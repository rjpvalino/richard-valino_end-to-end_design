"""Step 1 runner: generates every dataset in order, then validates it.

    py -3.12 tools/run_step1.py [--refresh]

`--refresh` re-downloads the raw Census files instead of using the .tmp cache.
Exits non-zero if any check fails.
"""

import sys

import fetch_geometry
import gen_county_data
import gen_hardware_data
import gen_network_data
import gen_spectrum_data
from common import (
    BAND_PLANS, DATA, EXPECTED_COUNTS, EXPECTED_TOTAL, LTE_LAYERS, NR_LAYERS,
    PCS_BLOCKS, all_blocks, band_blocks, blocks_for, read_json,
)
from edge_cases import assign, summarize

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(f"{label} {detail}")


def validate():
    print("\n=== Validation ===")
    counties = read_json(DATA / "counties.json")
    hardware = read_json(DATA / "hardware.json")
    spectrum = read_json(DATA / "spectrum.json")
    geo = read_json(DATA / "counties.geojson")

    # 1 - county counts
    per_state = {}
    for c in counties:
        per_state[c["state"]] = per_state.get(c["state"], 0) + 1
    check(f"257 counties (got {len(counties)})", len(counties) == EXPECTED_TOTAL)
    check(f"state split {per_state}", per_state == EXPECTED_COUNTS)

    # 2 - real Census fields populated
    bad_pop = [c["fips"] for c in counties
               if not c["population"] or c["land_area_sqmi"] <= 0]
    check("population and land area present for every county", not bad_pop,
          str(bad_pop[:5]))

    # 3 - tier counts
    tiers = {}
    for c in counties:
        tiers[c["tier"]] = tiers.get(c["tier"], 0) + 1
    check(f"tiers urban=20 suburban=55 rural=182 (got {tiers})",
          tiers == {"urban": 20, "suburban": 55, "rural": 182})

    # 4 - same FIPS set everywhere
    fips = {c["fips"] for c in counties}
    geo_fips = {f["properties"]["fips"] for f in geo["features"]}
    check("hardware.json keyed by the same 257 FIPS", set(hardware) == fips)
    check("spectrum.json keyed by the same 257 FIPS", set(spectrum) == fips)
    check(f"counties.geojson has 257 matching features "
          f"(got {len(geo['features'])})", geo_fips == fips)

    # 5 - 12 monthly records, chronological
    bad_months = [
        c["fips"] for c in counties
        if len(c["network"]["months"]) != 12
        or [m["month"] for m in c["network"]["months"]]
        != sorted(m["month"] for m in c["network"]["months"])
    ]
    check("every county has 12 monthly records in order", not bad_months,
          str(bad_months[:5]))

    # 6 - spectrum integrity
    valid_layers = set(LTE_LAYERS) | set(NR_LAYERS)
    layer_errs, budget_errs, block_errs, own_errs = [], [], [], []
    for f, s in spectrum.items():
        for layer in s["lte_layers"] + s["nr_layers"]:
            if layer not in valid_layers:
                layer_errs.append(f"{f}:{layer}")
        for b in s["bands"]:
            if b["mhz_lte"] + b["mhz_nr"] > b["mhz_owned"]:
                budget_errs.append(f"{f}:{b['band']}")
            if b["mhz_owned"] == 0 and (b["mhz_lte"] or b["mhz_nr"]):
                own_errs.append(f"{f}:{b['band']}")
        for cell in band_blocks(s, "1900"):
            if cell["block"] not in PCS_BLOCKS:
                block_errs.append(f"{f}:{cell['block']}")
        if [c["block"] for c in band_blocks(s, "1900")] != PCS_BLOCKS:
            block_errs.append(f"{f}:block-order")
    check("all layer names are in the allowed LTE/NR list", not layer_errs,
          str(layer_errs[:5]))
    check("per band, mhz_lte + mhz_nr <= mhz_owned", not budget_errs,
          str(budget_errs[:5]))
    check("no county deploys a band it does not own", not own_errs,
          str(own_errs[:5]))
    check("all 13 PCS blocks present in band order", not block_errs,
          str(block_errs[:5]))

    # Every banded band's blocks must agree with that band's MHz totals. This
    # is the invariant that makes the block strip trustworthy: the totals are
    # derived from the blocks, so a disagreement means one of them is a lie.
    blk_mismatch, width_errs, order_errs = [], [], []
    for f, s in spectrum.items():
        for band in BAND_PLANS:
            rec = next(b for b in s["bands"] if b["band"] == band)
            cells = band_blocks(s, band)
            lte = sum(c["mhz"] for c in cells if c["layer"] == "LTE")
            nr = sum(c["mhz"] for c in cells if c["layer"] == "NR")
            if (lte, nr) != (rec["mhz_lte"], rec["mhz_nr"]):
                blk_mismatch.append(f"{f}:{band}")
            if [c["block"] for c in cells] != blocks_for(band):
                order_errs.append(f"{f}:{band}")
            for c in cells:
                if c["width_mhz"] != dict(BAND_PLANS[band]["blocks"])[c["block"]]:
                    width_errs.append(f"{f}:{band}:{c['block']}")
    check("every band's blocks sum to that band's MHz totals", not blk_mismatch,
          str(blk_mismatch[:5]))
    check("every band lists its blocks in frequency order", not order_errs,
          str(order_errs[:5]))
    check("every block carries its real FCC width", not width_errs,
          str(width_errs[:5]))

    # 700 is the anchor: it has no NR layer, so adjacency and isolation are
    # meaningless there and must never be asserted.
    anchor_flag_errs = [
        f for f, s in spectrum.items()
        for c in band_blocks(s, "700")
        if c["adjacent_to_nr"] or c["isolated"]
    ]
    check("700 MHz blocks never claim NR adjacency or isolation",
          not anchor_flag_errs, str(anchor_flag_errs[:5]))

    # 7 - L700 is never refarmed: no NR on the 700 band, ever
    l700_errs = [
        f for f, s in spectrum.items()
        if next(b for b in s["bands"] if b["band"] == "700")["mhz_nr"] > 0
        or "N700" in s["nr_layers"]
    ]
    check("no county has NR on the 700 band (L700 is the anchor)", not l700_errs,
          str(l700_errs[:5]))

    # Hard-rule precondition: at least 5 MHz of LTE must remain possible
    thin = [f for f, s in spectrum.items() if s["totals"]["mhz_lte"] < 5]
    check("every county has >= 5 MHz of LTE on air", not thin, str(thin[:5]))

    # Hardware arithmetic
    hw_errs = [
        f for f, h in hardware.items()
        if h["nr_capable_sites"] + h["lte_only_sites"] != h["total_sites"]
        or h["total_sites"] <= 0
    ]
    check("hardware site counts add up", not hw_errs, str(hw_errs[:5]))

    # 8 - edge-case census: every Step 2 branch must have examples
    print("\n=== Edge-case census (each must be non-zero) ===")
    tag_map = assign(counties)
    for tag, n in sorted(summarize(tag_map).items()):
        print(f"  {tag:<20} {n}")

    latest = {c["fips"]: c["network"]["latest"] for c in counties}
    observed = {
        "LTE congestion > NR": sum(
            1 for v in latest.values()
            if v["lte_congestion_pct"] > v["nr_congestion_pct"]),
        "LTE users > NR users": sum(
            1 for v in latest.values() if v["lte_users"] > v["nr_users"]),
        "LTE-only devices > 30%": sum(
            1 for v in latest.values() if v["pct_lte_only_devices"] > 30.0),
        "LTE load rising, still below NR": sum(
            1 for c in counties
            if (sum(m["lte_congestion_pct"] for m in c["network"]["months"][-3:])
                > sum(m["lte_congestion_pct"] for m in c["network"]["months"][:3]) + 3)
            and c["network"]["latest"]["lte_congestion_pct"]
            < c["network"]["latest"]["nr_congestion_pct"]),
        "LTE-only sites remain": sum(
            1 for h in hardware.values() if h["lte_only_sites"] > 0),
        "isolated LTE PCS block": sum(
            1 for s in spectrum.values()
            if any(c["isolated"] for c in all_blocks(s))),
        "carveable PCS block": sum(
            1 for s in spectrum.values()
            if any(c["layer"] == "LTE" and c["adjacent_to_nr"]
                   for c in all_blocks(s))),
        "no N2500 layer": sum(
            1 for s in spectrum.values() if "N2500" not in s["nr_layers"]),
        "no L700 anchor": sum(
            1 for s in spectrum.values() if "L700" not in s["lte_layers"]),
    }
    print()
    for label, n in observed.items():
        check(f"{label}: {n} counties", n > 0)

    # Projected Step 2 split, so we know the mix before writing the engine.
    hold_gate = sum(
        1 for f, v in latest.items()
        if v["lte_congestion_pct"] > v["nr_congestion_pct"]
        or v["lte_users"] > v["nr_users"]
        or v["pct_lte_only_devices"] > 30.0
    )
    clean = len(counties) - hold_gate
    print(f"\n  projected Step 2 mix: {hold_gate} counties "
          f"({100 * hold_gate / len(counties):.0f}%) hit a Hold gate, "
          f"{clean} ({100 * clean / len(counties):.0f}%) are Carve/Review "
          f"candidates")

    # 10 - spot checks against known real Census values
    print("\n=== Spot checks vs real Census values ===")
    by_fips = {c["fips"]: c for c in counties}
    cook, kew = by_fips["17031"], by_fips["26083"]
    check(f"Cook County IL: pop {cook['population']:,}, "
          f"{cook['density_per_sqmi']}/sq mi, {cook['tier']}",
          cook["population"] == 5182617 and cook["tier"] == "urban")
    check(f"Keweenaw County MI: pop {kew['population']:,}, "
          f"{kew['density_per_sqmi']}/sq mi, {kew['tier']}",
          kew["density_per_sqmi"] == 4.0 and kew["tier"] == "rural")


def main():
    refresh = "--refresh" in sys.argv
    gen_county_data.build(refresh)
    gen_network_data.build()
    gen_hardware_data.build()
    gen_spectrum_data.build()
    fetch_geometry.build(refresh)
    validate()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("Step 1 complete. All checks passed.")
    print("Census data is real; all network, device, hardware, and spectrum "
          "data is synthetic for a fictional carrier.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
