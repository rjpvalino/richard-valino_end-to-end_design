"""Step 2 runner - apply the rule engine to all 257 counties and validate.

    py -3.12 tools/gen_recommendations.py

Output: data/recommendations.json

The validation here is the important part: it re-checks the hard rules against
the engine's own output. A hard rule that the engine believes it honoured but
actually broke is the failure mode that would destroy an engineer's trust, so it
is asserted independently rather than assumed.
"""

import sys
from collections import Counter

import rules_engine
from common import (
    DATA, PCS_BLOCK_MHZ, REFARM_PATHS, band_blocks, load_counties, read_json,
    write_json,
)
from rules_engine import (
    ENGINE_VERSION, LTE_CARRIER_MHZ, LTE_ONLY_HOLD_PCT, MIN_LTE_MHZ,
    REVIEW_BELOW_CONFIDENCE, evaluate,
)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(f"{label} {detail}")


def build():
    print("Step 2 - recommendation engine (rule-based)")
    counties = load_counties()
    hardware = read_json(DATA / "hardware.json")
    spectrum = read_json(DATA / "spectrum.json")

    recs = {}
    for county in counties:
        fips = county["fips"]
        recs[fips] = evaluate(county, hardware[fips], spectrum[fips])

    by_rec = Counter(r["recommendation"] for r in recs.values())
    by_state = {}
    for r in recs.values():
        by_state.setdefault(r["state"], Counter())[r["recommendation"]] += 1

    total_mhz = sum(r["total_mhz_shift"] for r in recs.values())
    review_queue = sorted(
        (r["fips"] for r in recs.values() if r["needs_manager_review"]),
        key=lambda f: recs[f]["confidence"],
    )

    payload = {
        "engine_version": ENGINE_VERSION,
        "note": "Rule-based and deterministic. Census data is real; all "
                "network, device, hardware, and spectrum data is synthetic for "
                "a fictional carrier.",
        "thresholds": {
            "min_lte_mhz": MIN_LTE_MHZ,
            "lte_carrier_mhz": LTE_CARRIER_MHZ,
            "lte_only_hold_pct": LTE_ONLY_HOLD_PCT,
            "review_below_confidence": REVIEW_BELOW_CONFIDENCE,
        },
        "summary": {
            "counties": len(recs),
            "by_recommendation": dict(sorted(by_rec.items())),
            "by_state": {s: dict(sorted(c.items())) for s, c in sorted(by_state.items())},
            "total_mhz_refarmed": total_mhz,
            "review_queue_size": len(review_queue),
        },
        "review_queue": review_queue,
        "counties": recs,
    }
    write_json(DATA / "recommendations.json", payload)

    print(f"  {len(recs)} counties evaluated")
    for rec, n in sorted(by_rec.items()):
        print(f"    {rec:<8} {n:>4}  ({100.0 * n / len(recs):.0f}%)")
    print(f"  total MHz refarmed: {total_mhz:,}")
    print(f"  manager review queue: {len(review_queue)}")
    return payload, counties, hardware, spectrum


def validate(payload, counties, hardware, spectrum):
    print("\n=== Validation: hard rules re-checked against engine output ===")
    recs = payload["counties"]
    by_fips = {c["fips"]: c for c in counties}

    check(f"257 counties evaluated (got {len(recs)})", len(recs) == 257)

    # HR-01 - the LTE floor, re-derived from the shifts rather than trusted.
    floor_errs = []
    for f, r in recs.items():
        floor = (LTE_CARRIER_MHZ if not hardware[f]["fully_nr_capable"]
                 else MIN_LTE_MHZ)
        if r["after"]["mhz_lte"] < MIN_LTE_MHZ:
            floor_errs.append(f"{f}:{r['after']['mhz_lte']}MHz")
        elif r["shifts"] and r["after"]["mhz_lte"] < floor:
            floor_errs.append(f"{f}:{r['after']['mhz_lte']}<JR-02 floor {floor}")
    check(f"HR-01 every county keeps >= {MIN_LTE_MHZ} MHz LTE "
          f"(and the JR-02 floor where sites are not all NR-capable)",
          not floor_errs, str(floor_errs[:5]))

    # HR-02 - the anchor must survive with LTE still on it.
    anchor_errs = []
    for f, r in recs.items():
        if not r["shifts"]:
            continue
        anchor = r["anchor"]
        if not anchor:
            anchor_errs.append(f"{f}:no-anchor")
            continue
        band = anchor["band"]
        sb = next(b for b in spectrum[f]["bands"] if b["band"] == band)
        moved = sum(s["mhz"] for s in r["shifts"] if s["band"] == band)
        if sb["mhz_lte"] - moved < MIN_LTE_MHZ:
            anchor_errs.append(f"{f}:{anchor['layer']} left "
                               f"{sb['mhz_lte'] - moved}MHz")
    check("HR-02 the LTE anchor layer always retains LTE", not anchor_errs,
          str(anchor_errs[:5]))

    # HR-02 - L700 preferred over L600 wherever both exist.
    pref_errs = [
        f for f, r in recs.items()
        if r["anchor"] and "L700" in spectrum[f]["lte_layers"]
        and r["anchor"]["layer"] != "L700"
    ]
    check("HR-02 L700 anchors wherever the county holds it", not pref_errs,
          str(pref_errs[:5]))

    # HR-03 - only the four same-band paths, and never L700.
    path_errs = []
    for f, r in recs.items():
        for s in r["shifts"]:
            if REFARM_PATHS.get(s["from"]) != s["to"]:
                path_errs.append(f"{f}:{s['from']}->{s['to']}")
            if s["from"] == "L700":
                path_errs.append(f"{f}:L700-refarmed")
    check("HR-03 every shift is a valid same-band path, and L700 is never "
          "refarmed", not path_errs, str(path_errs[:5]))

    # HR-04 - freed MHz always lands on NR; the before/after totals must balance.
    reuse_errs = []
    for f, r in recs.items():
        moved = r["total_mhz_shift"]
        if r["after"]["mhz_nr"] != r["before"]["mhz_nr"] + moved:
            reuse_errs.append(f"{f}:nr")
        if r["after"]["mhz_lte"] != r["before"]["mhz_lte"] - moved:
            reuse_errs.append(f"{f}:lte")
        if moved and not r["shifts"]:
            reuse_errs.append(f"{f}:orphan")
    check("HR-04 every freed MHz is reused for NR (before/after balance)",
          not reuse_errs, str(reuse_errs[:5]))

    # HR-05 - no Carve where LTE is hotter or busier.
    hr5_errs = []
    for f, r in recs.items():
        latest = by_fips[f]["network"]["latest"]
        if r["shifts"] and (
            latest["lte_congestion_pct"] > latest["nr_congestion_pct"]
            or latest["lte_users"] > latest["nr_users"]
        ):
            hr5_errs.append(f)
    check("HR-05 nothing is carved where LTE is hotter or busier than NR",
          not hr5_errs, str(hr5_errs[:5]))

    # JR-01 - no Carve above the LTE-only threshold.
    jr1_errs = [
        f for f, r in recs.items()
        if r["shifts"]
        and by_fips[f]["network"]["latest"]["pct_lte_only_devices"]
        > LTE_ONLY_HOLD_PCT
    ]
    check(f"JR-01 nothing is carved above {LTE_ONLY_HOLD_PCT:.0f}% LTE-only "
          f"devices", not jr1_errs, str(jr1_errs[:5]))

    # JR-03 / JR-04 - only adjacent PCS blocks move; isolated blocks never do.
    pcs_errs = []
    for f, r in recs.items():
        pcs = next((s for s in r["shifts"] if s["band"] == "1900"), None)
        if not pcs:
            continue
        cells = band_blocks(spectrum[f], "1900")
        strip = {c["block"]: c for c in cells}
        order = {c["block"]: i for i, c in enumerate(cells)}
        for blk in pcs["blocks"]:
            cell = strip.get(blk)
            if cell is None or cell["layer"] != "LTE":
                pcs_errs.append(f"{f}:{blk}:not-LTE")
            elif cell["isolated"]:
                pcs_errs.append(f"{f}:{blk}:isolated")
        # The first block carved must have been adjacent to NR at the start.
        first = pcs["blocks"][0] if pcs["blocks"] else None
        if first and not strip[first]["adjacent_to_nr"]:
            # Legal only if an earlier carve in this same band made it adjacent.
            carved = set(pcs["blocks"])
            idx = order[first]
            neigh = {b for b, i in order.items() if abs(i - idx) == 1}
            if not (neigh & carved):
                pcs_errs.append(f"{f}:{first}:not-adjacent")
        if pcs["mhz"] != len(pcs["blocks"]) * PCS_BLOCK_MHZ:
            pcs_errs.append(f"{f}:mhz-mismatch")
    check("JR-03/JR-04 only PCS blocks adjacent to NR move; isolated blocks "
          "stay on LTE", not pcs_errs, str(pcs_errs[:5]))

    # Shifts must never exceed the LTE actually on that band.
    over_errs = []
    for f, r in recs.items():
        for s in r["shifts"]:
            sb = next(b for b in spectrum[f]["bands"] if b["band"] == s["band"])
            if s["mhz"] > sb["mhz_lte"]:
                over_errs.append(f"{f}:{s['band']}:{s['mhz']}>{sb['mhz_lte']}")
    check("no shift exceeds the LTE MHz actually on that band", not over_errs,
          str(over_errs[:5]))

    # One move per county, and alternatives must be genuinely different bands
    # that were not also recommended.
    alt_errs = []
    for f, r in recs.items():
        if len(r["shifts"]) > 1:
            alt_errs.append(f"{f}:{len(r['shifts'])}-shifts")
        moved = {s["band"] for s in r["shifts"]}
        for a in r["alternatives"]:
            if a["band"] in moved:
                alt_errs.append(f"{f}:{a['band']}-both")
            if REFARM_PATHS.get(a["from"]) != a["to"]:
                alt_errs.append(f"{f}:{a['from']}-bad-path")
        if r["alternatives"] and not r["shifts"]:
            alt_errs.append(f"{f}:alts-without-shift")
    check("one refarm move per county, with remaining options recorded as "
          "distinct alternatives", not alt_errs, str(alt_errs[:5]))

    # Output contract the prototype and LLM layer depend on.
    shape_errs = []
    for f, r in recs.items():
        if r["recommendation"] not in ("Carve", "Hold", "Review"):
            shape_errs.append(f"{f}:rec")
        if not 0 <= r["confidence"] <= 100:
            shape_errs.append(f"{f}:conf")
        if not 1 <= len(r["reasons"]) <= 3:
            shape_errs.append(f"{f}:reasons={len(r['reasons'])}")
        if not r["rules_fired"]:
            shape_errs.append(f"{f}:no-rules")
        if r["recommendation"] == "Carve" and not r["shifts"]:
            shape_errs.append(f"{f}:carve-without-shift")
        if r["recommendation"] == "Hold" and r["shifts"]:
            shape_errs.append(f"{f}:hold-with-shift")
    check("output contract: valid recommendation, 0-100 confidence, 1-3 "
          "reasons, rules always present", not shape_errs, str(shape_errs[:5]))

    # Review routing.
    route_errs = [
        f for f, r in recs.items()
        if (r["confidence"] < REVIEW_BELOW_CONFIDENCE)
        != (r["recommendation"] == "Review")
    ]
    check(f"every county below {REVIEW_BELOW_CONFIDENCE}% confidence is routed "
          f"to Review, and only those", not route_errs, str(route_errs[:5]))

    # Every Carve needs a reason a human can read.
    thin = [f for f, r in recs.items() if any(len(x) < 20 for x in r["reasons"])]
    check("every recommendation has substantive plain-English reasons", not thin,
          str(thin[:5]))


def main():
    payload, counties, hardware, spectrum = build()
    validate(payload, counties, hardware, spectrum)

    recs = payload["counties"]
    print("\n=== Rule firing frequency ===")
    fired = Counter(
        rule["id"] for r in recs.values() for rule in r["rules_fired"]
    )
    names = {}
    for r in recs.values():
        for rule in r["rules_fired"]:
            names.setdefault(rule["id"], rule["name"])
    for rid, n in sorted(fired.items()):
        print(f"  {rid}  {n:>4}  {names[rid]}")

    print("\n=== Risk flags ===")
    for code, n in sorted(Counter(
        fl["code"] for r in recs.values() for fl in r["risk_flags"]
    ).items()):
        print(f"  {code:<28} {n:>4}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("Step 2 complete. All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
