"""Step 1d - spectrum ownership + usage. SYNTHETIC, fictional carrier.

Four bands are modelled at **block level** using real FCC designations and
their real paired widths - 600 MHz (A-G), 700 MHz (Lower A/B/C, Upper C/D),
1900 MHz PCS (the 13-block plan), and 2100 MHz AWS-1 (A-F). 2500 is held as
bulk MHz, because BRS/EBS channel leasing does not map to a clean block grid.

Per-band MHz totals are DERIVED from the blocks a county owns, so the block
strip and the band totals cannot disagree.

Two rules of the domain are baked into the data, not just the Step 2 engine:
  - L700 has no NR counterpart, so no county ever has NR on the 700 band.
  - Only same-band refarm paths exist: L600->N600, L1900->N1900,
    L2100->N2100, L2500->N2500.

Output: data/spectrum.json
"""

from common import (
    BAND_PLANS, BLOCKED_BANDS, BULK_BANDS, REFARM_PATHS, block_mhz, blocks_for,
    DATA, load_counties, rng_for, write_json,
)
from edge_cases import assign

BAND_LABELS = {b: BAND_PLANS[b]["label"] for b in BAND_PLANS}
BAND_LABELS["2500"] = "2500 MHz"
BAND_ORDER = ["600", "700", "1900", "2100", "2500"]

# How many blocks a county owns in each banded band, by tier.
OWNED_BLOCKS = {
    "600":  {"urban": (3, 4), "suburban": (2, 3), "rural": (1, 3)},
    "700":  {"urban": (1, 2), "suburban": (1, 2), "rural": (1, 1)},
    "1900": {"urban": (7, 11), "suburban": (5, 8), "rural": (3, 6)},
    "2100": {"urban": (3, 4), "suburban": (2, 3), "rural": (1, 3)},
}

# Bulk 2500 holdings, MHz: (min, max, step).
OWNED_2500 = {"urban": (60, 120, 10), "suburban": (40, 90, 10), "rural": (0, 60, 10)}

# Fraction of a band already carrying NR, by tier.
NR_FRACTION = {"urban": (0.40, 0.80), "suburban": (0.30, 0.70), "rural": (0.00, 0.50)}

MIN_TOTAL_LTE_MHZ = 10


def _round_to(value, step):
    return int(round(value / step) * step)


def _pick_run(rnd, names, want, allow_detached):
    """Own a contiguous run of `want` blocks, sometimes with a detached extra.

    Contiguous holdings are the norm - a carrier buys adjacent blocks to build
    a wide carrier. The occasional detached block is what creates an isolated
    LTE block later, which is the interesting planning case.
    """
    n = len(names)
    want = max(1, min(want, n))
    detached = 1 if (allow_detached and want >= 3 and rnd.random() < 0.15) else 0
    run_len = want - detached
    start = rnd.randint(0, n - run_len)
    idx = list(range(start, start + run_len))
    if detached:
        far = [i for i in range(n) if i < start - 1 or i > start + run_len]
        if far:
            idx.append(rnd.choice(far))
    return sorted(idx)


def _assign_layers(rnd, names, owned_idx, force_all_lte=False):
    """Split owned blocks into LTE and NR.

    NR takes a contiguous slice at one end of the primary run, which is how a
    wide NR carrier is actually assembled; LTE keeps the rest.
    """
    if force_all_lte:
        return {names[i]: "LTE" for i in owned_idx}

    # The primary run is the longest stretch of consecutive owned indices.
    runs, cur = [], [owned_idx[0]]
    for i in owned_idx[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            runs.append(cur); cur = [i]
    runs.append(cur)
    primary = max(runs, key=len)

    nr_len = max(1, min(len(primary), round(len(primary) * rnd.uniform(0.45, 0.85))))
    nr_idx = set(primary[-nr_len:] if rnd.random() < 0.7 else primary[:nr_len])
    out = {names[i]: ("NR" if i in nr_idx else "LTE") for i in owned_idx}
    if "NR" not in out.values():
        out[names[owned_idx[-1]]] = "NR"
    return out


def _owned_groups(names, assigned):
    """Runs of consecutive owned blocks, as lists of indices."""
    groups, cur = [], []
    for i, b in enumerate(names):
        if b in assigned:
            cur.append(i)
        elif cur:
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    return groups


def _strip(band, assigned):
    """Every block in the band, in frequency order, with contiguity resolved.

    `adjacent_to_nr` - touches an owned NR block, so carving it widens the
        existing carrier. This is the test the Step 2 PCS carve rule uses.
    `isolated` - sits in a run of owned blocks with no NR in it at all, cut off
        from the NR carrier by spectrum the carrier does not own. Strictly a
        smaller set than "not adjacent".
    """
    names = blocks_for(band)
    widths = block_mhz(band)
    groups = _owned_groups(names, assigned)
    group_of = {i: gi for gi, g in enumerate(groups) for i in g}
    has_nr = [any(assigned.get(names[i]) == "NR" for i in g) for g in groups]

    # 700 is the LTE anchor and has no NR layer anywhere in the band plan, so
    # "adjacent to NR" and "isolated from the NR carrier" are not meaningful
    # there - every 700 block would otherwise read as isolated, which is noise,
    # not a finding.
    anchor_band = band == "700"

    out = []
    for i, b in enumerate(names):
        layer = assigned.get(b)
        neigh = [names[j] for j in (i - 1, i + 1) if 0 <= j < len(names)]
        out.append({
            "block": b,
            "width_mhz": widths[b],
            "owned": layer is not None,
            "layer": layer,
            "mhz": widths[b] if layer else 0,
            "adjacent_to_nr": (False if anchor_band else
                               (any(assigned.get(n) == "NR" for n in neigh)
                                if layer == "LTE" else False)),
            "isolated": (False if anchor_band else
                         (layer == "LTE" and not has_nr[group_of[i]])),
        })
    return out


def _band_record(band, owned, mhz_lte, mhz_nr, blocks=None):
    path = REFARM_PATHS.get(f"L{band}")
    return {
        "band": band,
        "label": BAND_LABELS[band],
        "blocked": band in BLOCKED_BANDS,
        "mhz_owned": owned,
        "mhz_lte": mhz_lte,
        "mhz_nr": mhz_nr,
        "mhz_unused": owned - mhz_lte - mhz_nr,
        "lte_layer": f"L{band}" if mhz_lte > 0 else None,
        "nr_layer": f"N{band}" if mhz_nr > 0 else None,
        "refarmable": path is not None,
        "refarm_path": f"L{band} -> {path}" if path else None,
        "blocks": blocks or [],
    }


def build():
    print("Step 1d - spectrum ownership + usage (SYNTHETIC, block-level)")
    counties = load_counties()
    tag_map = assign(counties)

    spectrum = {}
    for county in counties:
        fips, tier = county["fips"], county["tier"]
        tags = tag_map[fips]
        rnd = rng_for(fips)
        bands = {}

        for band in BLOCKED_BANDS:
            names = blocks_for(band)
            widths = block_mhz(band)

            # --- 700: the LTE anchor. Owned, never on NR, no exceptions. ---
            if band == "700":
                if "no_l700" in tags:
                    bands[band] = _band_record(band, 0, 0, 0, _strip(band, {}))
                    continue
                want = rnd.randint(*OWNED_BLOCKS[band][tier])
                idx = _pick_run(rnd, names, want, allow_detached=False)
                assigned = {names[i]: "LTE" for i in idx}

            # --- PCS: the deliberate isolated-block case lives here ---
            elif band == "1900" and "isolated_pcs_lte" in tags:
                n = len(names)
                nr_len = rnd.randint(3, 4)
                nr_start = n - nr_len - rnd.randint(0, 1)
                assigned = {names[i]: "NR" for i in range(nr_start, nr_start + nr_len)}
                lo = rnd.randint(0, max(0, nr_start - 3))
                assigned[names[lo]] = "LTE"
                if rnd.random() < 0.4 and lo + 1 < nr_start - 2:
                    assigned[names[lo + 1]] = "LTE"

            else:
                want = rnd.randint(*OWNED_BLOCKS[band][tier])
                idx = _pick_run(rnd, names, want, allow_detached=True)
                assigned = _assign_layers(rnd, names, idx)

            strip = _strip(band, assigned)
            lte = sum(c["mhz"] for c in strip if c["layer"] == "LTE")
            nr = sum(c["mhz"] for c in strip if c["layer"] == "NR")
            bands[band] = _band_record(band, lte + nr, lte, nr, strip)

        # --- 2500: bulk MHz, no block grid ---
        lo, hi, step = OWNED_2500[tier]
        owned = 0 if hi == 0 else rnd.randrange(lo, hi + 1, step)
        if owned == 0:
            bands["2500"] = _band_record("2500", 0, 0, 0)
        elif "no_n2500" in tags:
            # Owns 2500 but has never lit NR on it - the strongest refarm
            # candidate the tool can surface.
            bands["2500"] = _band_record("2500", owned, owned, 0)
        else:
            nr = _round_to(owned * rnd.uniform(*NR_FRACTION[tier]), 10)
            nr = max(0, min(owned, nr))
            unused = 10 if (owned - nr >= 20 and rnd.random() < 0.25) else 0
            bands["2500"] = _band_record("2500", owned, owned - nr - unused, nr)

        ordered = [bands[b] for b in BAND_ORDER]

        # Hard-rule precondition: leave enough LTE on air that Step 2's "keep at
        # least 5 MHz of LTE" rule is always satisfiable. Flip NR blocks back to
        # LTE on AWS, then PCS, if a county came out too thin.
        total_lte = sum(b["mhz_lte"] for b in ordered)
        for band in ("2100", "1900"):
            if total_lte >= MIN_TOTAL_LTE_MHZ:
                break
            rec = bands[band]
            for cell in reversed(rec["blocks"]):
                if total_lte >= MIN_TOTAL_LTE_MHZ:
                    break
                if cell["layer"] != "NR":
                    continue
                # Never strand the band with no NR at all.
                if sum(1 for c in rec["blocks"] if c["layer"] == "NR") <= 1:
                    break
                cell["layer"] = "LTE"
                total_lte += cell["mhz"]
            assigned = {c["block"]: c["layer"] for c in rec["blocks"] if c["owned"]}
            rec["blocks"] = _strip(band, assigned)
            rec["mhz_lte"] = sum(c["mhz"] for c in rec["blocks"] if c["layer"] == "LTE")
            rec["mhz_nr"] = sum(c["mhz"] for c in rec["blocks"] if c["layer"] == "NR")
            rec["lte_layer"] = f"L{band}" if rec["mhz_lte"] else None
            rec["nr_layer"] = f"N{band}" if rec["mhz_nr"] else None
        # Blocks live only on their band record - duplicating them into a
        # flat top-level array as well tripled the file size for no gain.
        spectrum[fips] = {
            "fips": fips,
            "name": county["name"],
            "state": county["state"],
            "tier": tier,
            "bands": ordered,
            "lte_layers": [b["lte_layer"] for b in ordered if b["lte_layer"]],
            "nr_layers": [b["nr_layer"] for b in ordered if b["nr_layer"]],
            "totals": {
                "mhz_owned": sum(b["mhz_owned"] for b in ordered),
                "mhz_lte": sum(b["mhz_lte"] for b in ordered),
                "mhz_nr": sum(b["mhz_nr"] for b in ordered),
            },
        }

    write_json(DATA / "spectrum.json", spectrum)

    def band_of(s, b):
        return next(x for x in s["bands"] if x["band"] == b)

    no_2500 = sum(1 for s in spectrum.values() if band_of(s, "2500")["mhz_owned"] == 0)
    all_lte_2500 = sum(1 for s in spectrum.values()
                       if band_of(s, "2500")["mhz_owned"] > 0
                       and "N2500" not in s["nr_layers"])
    no_l700 = sum(1 for s in spectrum.values() if "L700" not in s["lte_layers"])
    cells = lambda s: [c for b in s["bands"] for c in b["blocks"]]
    iso = sum(1 for s in spectrum.values() if any(c["isolated"] for c in cells(s)))
    carve = sum(1 for s in spectrum.values()
                if any(c["layer"] == "LTE" and c["adjacent_to_nr"] for c in cells(s)))
    print(f"  block-level bands: {', '.join(BLOCKED_BANDS)}   bulk: {', '.join(BULK_BANDS)}")
    print(f"  no 2500 holdings at all: {no_2500}")
    print(f"  owns 2500 but no N2500 layer yet: {all_lte_2500}")
    print(f"  counties with no L700: {no_l700}")
    print(f"  counties with an isolated LTE block: {iso}")
    print(f"  counties with a carveable block (adjacent to NR): {carve}")
    print(f"  total MHz on LTE: {sum(s['totals']['mhz_lte'] for s in spectrum.values()):,}")
    print(f"  total MHz on NR:  {sum(s['totals']['mhz_nr'] for s in spectrum.values()):,}")
    return spectrum


if __name__ == "__main__":
    build()
