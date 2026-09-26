"""Step 2 - rule-based, explainable recommendation engine.

The engine is deliberately deterministic and rule-based, not statistical. Every
recommendation carries the rules that fired, the confidence adjustments that
produced its score, and plain-English reasons, so an engineer can audit it. The
Step 3 LLM layer explains these outputs; it never changes them.

Rule IDs are stable and appear in the audit trail, so a recommendation can be
traced back to a specific planning rule.

  HR-01  Keep at least 5 MHz of LTE in every county
  HR-02  Preserve an LTE anchor layer
  HR-03  Only same-band refarm paths are valid
  HR-04  Freed spectrum must be reused for NR
  HR-05  Hold if LTE is more congested, or carries more users, than NR
  JR-01  Hold when LTE-only device share is too high
  JR-02  Keep a full LTE carrier where sites are not all NR-capable
  JR-03  Only carve PCS blocks adjacent to existing NR blocks
  JR-04  An isolated LTE block stays LTE

All network, hardware, and spectrum inputs are synthetic (fictional carrier).
"""

from common import PCS_BLOCK_MHZ, PCS_BLOCKS, REFARM_PATHS, band_blocks

ENGINE_VERSION = "2.0"

# Thresholds. These are the knobs a planner would argue about, so they are
# named, surfaced in the output, and easy to find.
MIN_LTE_MHZ = 5                 # HR-01 hard floor
LTE_CARRIER_MHZ = 10            # JR-02 "keep an LTE carrier"
LTE_ONLY_HOLD_PCT = 30.0        # JR-01 "not ready yet"
LTE_ONLY_CAUTION_PCT = 20.0     # close enough to the threshold to cost confidence
REVIEW_BELOW_CONFIDENCE = 60    # route to Manager Review
SPARSE_USERS = 4000             # below this, monthly metrics are statistically thin
SPARSE_SITES = 10               # ditto for site count

LOW_BANDS = ("600", "700")
# Carve priority: highest capacity gain per MHz moved goes first. Un-carving
# (when the LTE floor needs protecting) walks this list in reverse, so low-band
# LTE coverage is the last thing given up.
CARVE_PRIORITY = ["2500", "2100", "1900", "600"]


def _band(spectrum, band):
    return next(b for b in spectrum["bands"] if b["band"] == band)


def _pick_anchor(spectrum):
    """HR-02. Return (layer, band, why).

    Low band carries LTE coverage, so it anchors when present. L700 wins over
    L600 because L700 has no NR path at all - it can never be refarmed, which
    makes it the natural anchor and frees L600 to move to N600.
    """
    lte_mhz = {b["band"]: b["mhz_lte"] for b in spectrum["bands"]}
    has = lambda band: lte_mhz.get(band, 0) > 0

    if has("700") and has("600"):
        return "L700", "700", (
            "county holds both L700 and L600, so L700 anchors LTE and L600 is "
            "free to move to N600"
        )
    if has("700"):
        return "L700", "700", "L700 is the LTE low-band anchor and has no NR path"
    if has("600"):
        return "L600", "600", (
            "L600 is the only LTE low band, so it stays as the coverage anchor"
        )
    for band in ("1900", "2100"):
        if has(band):
            return f"L{band}", band, (
                f"no LTE low band in this county, so L{band} is held back as the "
                f"LTE anchor"
            )
    for band in ("2500",):
        if has(band):
            return f"L{band}", band, (
                "only L2500 carries LTE here, so it must stay as the anchor"
            )
    return None, None, "county has no LTE layer on air"


def _carve_pcs(strip, retain_mhz):
    """JR-03 / JR-04. Grow the NR carrier outward, one adjacent block at a time.

    Only blocks touching an owned NR block can be carved, because only those
    widen the existing NR carrier. Adjacency is recomputed after each carve, so
    the carrier grows contiguously rather than in scattered pieces.

    Returns (carved block names in carve order, final layer map).
    """
    layers = {c["block"]: c["layer"] for c in strip if c["owned"]}
    carved = []
    while True:
        lte_mhz = sum(PCS_BLOCK_MHZ for v in layers.values() if v == "LTE")
        if lte_mhz <= retain_mhz:
            break
        candidates = []
        for i, block in enumerate(PCS_BLOCKS):
            if layers.get(block) != "LTE":
                continue
            neighbours = [
                PCS_BLOCKS[j] for j in (i - 1, i + 1) if 0 <= j < len(PCS_BLOCKS)
            ]
            if any(layers.get(n) == "NR" for n in neighbours):
                candidates.append(block)
        if not candidates:
            break  # everything left is isolated - JR-04 keeps it on LTE
        layers[candidates[0]] = "NR"
        carved.append(candidates[0])
    return carved, layers


def evaluate(county, hardware, spectrum):
    """Run the rules for one county. Returns the recommendation record."""
    fips = county["fips"]
    latest = county["network"]["latest"]
    rules = []
    reasons = []
    risks = []
    factors = []

    def fire(rule_id, name, outcome):
        rules.append({"id": rule_id, "name": name, "outcome": outcome})

    lte_cong = latest["lte_congestion_pct"]
    nr_cong = latest["nr_congestion_pct"]
    lte_users = latest["lte_users"]
    nr_users = latest["nr_users"]
    lte_only = latest["pct_lte_only_devices"]
    share_5g = latest["pct_5g_devices"]

    lte_only_sites = hardware["lte_only_sites"]
    pct_modernized = hardware["pct_modernized"]
    fully_ready = hardware["fully_nr_capable"]

    before_lte = spectrum["totals"]["mhz_lte"]
    before_nr = spectrum["totals"]["mhz_nr"]

    base = {
        "fips": fips,
        "name": county["name"],
        "state": county["state"],
        "state_name": county["state_name"],
        "tier": county["tier"],
        "before": {"mhz_lte": before_lte, "mhz_nr": before_nr},
    }

    # ---------------------------------------------------------------- HR-05
    # LTE busier or hotter than NR means the network is telling us not to move
    # capacity off LTE yet. This is a hard stop, whatever else looks good.
    hotter = lte_cong > nr_cong
    busier = lte_users > nr_users
    if hotter or busier:
        bits = []
        if hotter:
            bits.append(
                f"LTE is running hotter than NR ({lte_cong:.1f}% vs "
                f"{nr_cong:.1f}% PRB utilization)"
            )
        if busier:
            bits.append(
                f"LTE still carries more users than NR ({lte_users:,} vs "
                f"{nr_users:,})"
            )
        fire("HR-05", "Hold if LTE is more congested or busier than NR",
             "; ".join(bits) + " - refarming would move capacity off the "
             "busier layer")
        reasons.extend(bits)
        reasons.append(
            "Planning rule: LTE has to be the quieter layer before any of its "
            "spectrum moves to NR."
        )
        # Say what would have to change for this county to become a candidate.
        if lte_only > LTE_ONLY_HOLD_PCT:
            reasons.append(
                f"{lte_only:.1f}% of devices are LTE-only here, so the device "
                f"base has to migrate before the load will shift on its own."
            )
        else:
            reasons.append(
                f"Revisit once NR load overtakes LTE - {share_5g:.1f}% of devices "
                f"are already 5G-capable, so the crossover should come without "
                f"intervention."
            )
        # Confident Hold: the margin is the evidence.
        margin = max(
            lte_cong - nr_cong if hotter else 0,
            10.0 * (lte_users - nr_users) / max(1, nr_users) if busier else 0,
        )
        confidence = int(min(96, 84 + margin / 2.0))
        factors.append({"label": "Hard rule HR-05 blocks any carve",
                        "delta": None, "note": "base 84 for a hard-rule Hold"})
        if lte_only > LTE_ONLY_HOLD_PCT:
            risks.append({
                "code": "high_lte_only_share",
                "label": f"{lte_only:.1f}% of devices are LTE-only",
                "severity": "high",
            })
        if lte_only_sites:
            risks.append({
                "code": "lte_only_sites_remaining",
                "label": f"{lte_only_sites} of {hardware['total_sites']} sites "
                         f"are still LTE-only",
                "severity": "medium",
            })
        return _finish(base, "Hold", confidence, [], reasons, rules, risks,
                       factors, None, before_lte, before_nr)

    # ---------------------------------------------------------------- JR-01
    # Too much of the device base cannot use NR at all yet.
    if lte_only > LTE_ONLY_HOLD_PCT:
        fire("JR-01", "Hold when LTE-only device share is too high",
             f"{lte_only:.1f}% LTE-only devices exceeds the "
             f"{LTE_ONLY_HOLD_PCT:.0f}% threshold - not ready yet")
        reasons.append(
            f"{lte_only:.1f}% of devices in this county are LTE-only, above the "
            f"{LTE_ONLY_HOLD_PCT:.0f}% threshold, so LTE still has to carry them."
        )
        reasons.append(
            f"Only {share_5g:.1f}% of devices can use 5G, so moving spectrum to "
            f"NR would strand real traffic."
        )
        reasons.append(
            f"NR has headroom ({nr_cong:.1f}% utilization) but the constraint "
            f"here is devices, not capacity."
        )
        confidence = int(min(94, 78 + (lte_only - LTE_ONLY_HOLD_PCT)))
        factors.append({"label": "Judgment rule JR-01 holds the county",
                        "delta": None, "note": "base 78 for a device-readiness Hold"})
        risks.append({
            "code": "high_lte_only_share",
            "label": f"{lte_only:.1f}% of devices are LTE-only",
            "severity": "high",
        })
        if lte_only_sites:
            risks.append({
                "code": "lte_only_sites_remaining",
                "label": f"{lte_only_sites} of {hardware['total_sites']} sites "
                         f"are still LTE-only",
                "severity": "medium",
            })
        return _finish(base, "Hold", confidence, [], reasons, rules, risks,
                       factors, None, before_lte, before_nr)

    # ---------------------------------------------------------------- HR-02
    anchor_layer, anchor_band, anchor_why = _pick_anchor(spectrum)
    if anchor_layer is None:
        fire("HR-02", "Preserve an LTE anchor layer",
             "county has no LTE layer on air - nothing to refarm")
        reasons.append("This county has no LTE spectrum on air, so there is "
                       "nothing to refarm.")
        return _finish(base, "Hold", 70, [], reasons, rules, risks, factors,
                       None, before_lte, before_nr)
    fire("HR-02", "Preserve an LTE anchor layer", anchor_why)

    # ---------------------------------------------------------------- JR-02
    min_lte = MIN_LTE_MHZ
    if not fully_ready:
        min_lte = LTE_CARRIER_MHZ
        fire("JR-02", "Keep a full LTE carrier where sites are not all NR-capable",
             f"{lte_only_sites} of {hardware['total_sites']} sites are LTE-only, "
             f"so at least {LTE_CARRIER_MHZ} MHz of LTE is held back and "
             f"confidence is reduced")
        risks.append({
            "code": "lte_only_sites_remaining",
            "label": f"{lte_only_sites} of {hardware['total_sites']} sites are "
                     f"still LTE-only ({pct_modernized:.1f}% modernized)",
            "severity": "high" if pct_modernized < 80 else "medium",
        })

    # ---------------------------------------------------------------- HR-03
    # Build the candidate shifts. Only same-band paths exist, and L700 has none.
    fire("HR-03", "Only same-band refarm paths are valid",
         "evaluated L600->N600, L1900->N1900, L2100->N2100, L2500->N2500; "
         "L700 has no NR layer and is never refarmed")

    pcs_strip = band_blocks(spectrum, "1900")
    isolated_blocks = [c["block"] for c in pcs_strip if c["isolated"]]

    candidates = []
    for band in CARVE_PRIORITY:
        b = _band(spectrum, band)
        if b["mhz_lte"] <= 0 or f"L{band}" not in REFARM_PATHS:
            continue
        retain = min_lte if band == anchor_band else 0

        if band == "1900":
            carved, _ = _carve_pcs(pcs_strip, retain)
            if not carved:
                continue
            candidates.append({
                "band": band,
                "from": f"L{band}",
                "to": REFARM_PATHS[f"L{band}"],
                "mhz": len(carved) * PCS_BLOCK_MHZ,
                "blocks": [blk for blk in PCS_BLOCKS if blk in carved],
                "creates_new_nr_layer": b["mhz_nr"] == 0,
            })
        else:
            mhz = b["mhz_lte"] - retain
            if mhz <= 0:
                continue
            candidates.append({
                "band": band,
                "from": f"L{band}",
                "to": REFARM_PATHS[f"L{band}"],
                "mhz": mhz,
                "blocks": None,
                "creates_new_nr_layer": b["mhz_nr"] == 0,
            })

    # Recommend ONE move, not every eligible move at once. A refarm plan is
    # executed a layer at a time, and a recommendation that stripped LTE down to
    # its floor everywhere would be rejected on sight - the 5 MHz floor is the
    # fallback for constrained counties, not the target. The rest stay on the
    # record as alternatives, which is what the Adjust flow offers the engineer.
    #
    # Widening an existing NR carrier beats standing up a brand-new layer, then
    # the biggest capacity win, then band priority as the tie-break.
    candidates.sort(key=lambda c: (
        c["creates_new_nr_layer"], -c["mhz"], CARVE_PRIORITY.index(c["band"]),
    ))

    # HR-01 is applied during selection, not as a correction afterwards: take the
    # best candidate that fits under the county-wide LTE floor, trimming it if
    # only part of it fits, and falling through to the next candidate if none of
    # it does.
    allowed = before_lte - min_lte
    shifts, alternatives = [], list(candidates)
    for i, cand in enumerate(candidates):
        if allowed <= 0:
            break
        fitted = cand
        if cand["mhz"] > allowed:
            if cand["blocks"]:
                n = allowed // PCS_BLOCK_MHZ
                if n <= 0:
                    continue
                fitted = dict(cand, blocks=cand["blocks"][:n],
                              mhz=n * PCS_BLOCK_MHZ)
            else:
                trimmed = (allowed // 5) * 5
                if trimmed <= 0:
                    continue
                fitted = dict(cand, mhz=trimmed)
        shifts = [fitted]
        alternatives = [c for j, c in enumerate(candidates) if j != i]
        break

    # JR-03 / JR-04 audit trail for PCS.
    carveable = [c["block"] for c in pcs_strip
                 if c["layer"] == "LTE" and c["adjacent_to_nr"]]
    if carveable or isolated_blocks:
        pcs_shift = next((s for s in shifts if s["band"] == "1900"), None)
        moved = pcs_shift["blocks"] if pcs_shift else []
        fire("JR-03", "Only carve PCS blocks adjacent to existing NR blocks",
             f"carveable blocks adjacent to NR: {', '.join(carveable) or 'none'}; "
             f"moving {', '.join(moved) or 'none'}")
    if isolated_blocks:
        only_lte = len(spectrum["lte_layers"]) == 1
        fire("JR-04", "An isolated LTE block stays LTE",
             f"{', '.join(isolated_blocks)} not adjacent to any owned NR block"
             + (" and this is the county's only LTE layer" if only_lte else "")
             + " - left on LTE")
        risks.append({
            "code": "isolated_lte_block",
            "label": f"PCS block{'s' if len(isolated_blocks) > 1 else ''} "
                     f"{', '.join(isolated_blocks)} "
                     f"{'are' if len(isolated_blocks) > 1 else 'is'} isolated "
                     f"from the NR carrier and stay{'' if len(isolated_blocks) > 1 else 's'} on LTE",
            "severity": "low",
        })

    # ---------------------------------------------------------------- HR-01
    after_lte = before_lte - sum(s["mhz"] for s in shifts)
    fire("HR-01", f"Keep at least {MIN_LTE_MHZ} MHz of LTE in every county",
         f"{before_lte} MHz of LTE before, {after_lte} MHz after - floor is "
         f"{min_lte} MHz"
         + (" (raised by JR-02)" if min_lte > MIN_LTE_MHZ else ""))

    if not shifts:
        fire("JR-03", "No eligible spectrum to carve",
             "every LTE layer is either the anchor, has no NR path, or is not "
             "adjacent to an existing NR block")
        reasons.append(
            "There is no eligible spectrum to move here: every LTE layer is "
            "either the anchor, has no matching NR layer, or is not adjacent to "
            "an existing NR block."
        )
        reasons.append(
            f"NR is at {nr_cong:.1f}% utilization against LTE at "
            f"{lte_cong:.1f}%, so the network could take more NR - the "
            f"constraint is the spectrum layout."
        )
        if isolated_blocks:
            reasons.append(
                f"PCS block{'s' if len(isolated_blocks) > 1 else ''} "
                f"{', '.join(isolated_blocks)} would have to stay on LTE "
                f"regardless, being cut off from the NR carrier."
            )
        return _finish(base, "Hold", 66, [], reasons, rules, risks, factors,
                       {"layer": anchor_layer, "band": anchor_band,
                        "why": anchor_why},
                       before_lte, before_nr)

    # ---------------------------------------------------------------- HR-04
    fire("HR-04", "Freed spectrum must be reused for NR",
         "; ".join(f"{s['mhz']} MHz {s['from']} -> {s['to']}" for s in shifts)
         + " - no LTE spectrum is shut down without an NR reuse")
    if alternatives:
        fire("SEL-01", "Recommend one layer at a time",
             f"also eligible: "
             + "; ".join(f"{a['mhz']} MHz {a['from']} -> {a['to']}"
                         for a in alternatives)
             + " - held back as alternatives rather than carved in the same pass")

    # ------------------------------------------------------- confidence
    score = 62.0
    factors.append({"label": "Base confidence for a rule-based carve",
                    "delta": 62, "note": None})

    margin = nr_cong - lte_cong
    delta = min(12.0, margin / 3.0)
    score += delta
    factors.append({
        "label": f"NR is {margin:.1f} points busier than LTE "
                 f"({nr_cong:.1f}% vs {lte_cong:.1f}%)",
        "delta": round(delta, 1), "note": None,
    })

    if share_5g >= 85:
        delta = 10.0
    elif share_5g >= 75:
        delta = 6.0
    elif share_5g >= 65:
        delta = 2.0
    else:
        delta = 0.0
    if delta:
        score += delta
        factors.append({"label": f"{share_5g:.1f}% of devices are 5G-capable",
                        "delta": delta, "note": None})

    if lte_only > LTE_ONLY_CAUTION_PCT:
        delta = -6.0 - (lte_only - LTE_ONLY_CAUTION_PCT) * 0.6
        score += delta
        factors.append({
            "label": f"{lte_only:.1f}% LTE-only devices is close to the "
                     f"{LTE_ONLY_HOLD_PCT:.0f}% hold threshold",
            "delta": round(delta, 1), "note": None,
        })

    if fully_ready:
        score += 8.0
        factors.append({"label": "All sites are NR-capable", "delta": 8.0,
                        "note": None})
    else:
        delta = -(8.0 + 16.0 * (lte_only_sites / hardware["total_sites"]))
        delta = max(delta, -24.0)
        score += delta
        factors.append({
            "label": f"Only {pct_modernized:.1f}% of sites are NR-capable "
                     f"({lte_only_sites} LTE-only)",
            "delta": round(delta, 1), "note": "JR-02",
        })

    total_users = lte_users + nr_users
    if total_users and nr_users / total_users >= 0.70:
        score += 6.0
        factors.append({
            "label": f"{100.0 * nr_users / total_users:.0f}% of users are "
                     f"already on NR",
            "delta": 6.0, "note": None,
        })

    if isolated_blocks:
        score -= 5.0
        factors.append({
            "label": f"Isolated PCS block{'s' if len(isolated_blocks) > 1 else ''} "
                     f"({', '.join(isolated_blocks)}) complicate the band plan",
            "delta": -5.0, "note": "JR-04",
        })

    # A thin market is a real reason to be less sure. A few hundred users across
    # a handful of sites means monthly PRB and device figures swing on very
    # little traffic, so the same rules deserve less confidence than they do in
    # a market with statistical weight behind the numbers.
    if total_users < SPARSE_USERS or hardware["total_sites"] < SPARSE_SITES:
        score -= 11.0
        factors.append({
            "label": f"Thin market ({total_users:,} users across "
                     f"{hardware['total_sites']} sites) - monthly metrics move "
                     f"on very little traffic",
            "delta": -11.0, "note": None,
        })

    # Direction of travel matters as much as the current level. LTE load that is
    # climbing rather than falling undercuts the premise of a refarm, even while
    # it still sits below NR.
    months = county["network"]["months"]
    early = sum(m["lte_congestion_pct"] for m in months[:3]) / 3.0
    late = sum(m["lte_congestion_pct"] for m in months[-3:]) / 3.0
    if late > early + 1.0:
        score -= 9.0
        factors.append({
            "label": f"LTE congestion is trending up across the window "
                     f"({early:.1f}% -> {late:.1f}%), not down",
            "delta": -9.0, "note": None,
        })
        risks.append({
            "code": "lte_load_rising",
            "label": f"LTE utilization rose from {early:.1f}% to {late:.1f}% over "
                     f"12 months - confirm the trend before removing capacity",
            "severity": "medium",
        })

    new_layers = [s for s in shifts if s["creates_new_nr_layer"]]
    if new_layers:
        delta = max(-12.0, -7.0 * len(new_layers))
        score += delta
        names = ", ".join(s["to"] for s in new_layers)
        factors.append({
            "label": f"Stands up a brand-new NR layer ({names}) rather than "
                     f"widening an existing carrier",
            "delta": delta, "note": None,
        })
        risks.append({
            "code": "new_nr_layer",
            "label": f"{names} would be a new NR layer in this county, not an "
                     f"expansion of an existing carrier",
            "severity": "medium",
        })

    if anchor_band not in LOW_BANDS:
        score -= 6.0
        factors.append({
            "label": f"No LTE low band; {anchor_layer} is carrying the anchor",
            "delta": -6.0, "note": "HR-02",
        })
        risks.append({
            "code": "no_low_band_anchor",
            "label": f"No LTE low band in this county - {anchor_layer} is the "
                     f"anchor, so LTE coverage depends on mid-band",
            "severity": "medium",
        })

    if after_lte <= min_lte:
        score -= 5.0
        factors.append({
            "label": f"LTE left at the {min_lte} MHz floor with no margin",
            "delta": -5.0, "note": "HR-01",
        })
        risks.append({
            "code": "thin_lte_margin",
            "label": f"LTE drops to {after_lte} MHz, the minimum this county "
                     f"can run",
            "severity": "medium",
        })

    confidence = int(max(0, min(100, round(score))))

    # ------------------------------------------------------- reasons
    # Don't call a half-point gap "the busier layer" - that overstatement is
    # exactly what makes an engineer distrust the whole recommendation.
    if margin >= 5.0:
        reasons.append(
            f"NR is the busier layer ({nr_cong:.1f}% vs {lte_cong:.1f}% PRB "
            f"utilization) and carries {nr_users:,} of {total_users:,} users, so "
            f"it is where the capacity is needed."
        )
    else:
        reasons.append(
            f"NR is only marginally busier than LTE ({nr_cong:.1f}% vs "
            f"{lte_cong:.1f}% PRB utilization), though it already carries "
            f"{nr_users:,} of {total_users:,} users - the case rests on the user "
            f"split more than on load."
        )
    shift_desc = ", ".join(
        f"{s['mhz']} MHz {s['from']} -> {s['to']}"
        + (f" (blocks {', '.join(s['blocks'])})" if s["blocks"] else "")
        for s in shifts
    )
    plan = (
        f"Shift {shift_desc}, leaving {after_lte} MHz of LTE on air anchored on "
        f"{anchor_layer}."
    )
    reasons.append(plan)
    reasons.append(
        f"{share_5g:.1f}% of devices are 5G-capable and only {lte_only:.1f}% are "
        f"LTE-only, so the device base can follow the capacity."
    )
    if any(s["blocks"] for s in shifts):
        pcs = next(s for s in shifts if s["blocks"])
        reasons.append(
            f"PCS blocks {', '.join(pcs['blocks'])} sit next to the existing NR "
            f"blocks, so moving them widens the NR carrier instead of "
            f"fragmenting the band."
        )
    if alternatives:
        reasons.append(
            f"{alternatives[0]['from']} is also eligible "
            f"({alternatives[0]['mhz']} MHz) but is left for a later pass rather "
            f"than moving two layers at once."
        )

    # For a county heading to Manager Review, the three things a reviewer needs
    # are what is proposed and why it is shaky - not the supporting rationale.
    # So when confidence is low, the largest confidence penalties are promoted
    # ahead of the positive case. The full list stays in `all_reasons`.
    concerns = [
        f["label"].rstrip(".") + "."
        for f in sorted(
            (f for f in factors if isinstance(f.get("delta"), (int, float))
             and f["delta"] < 0),
            key=lambda f: f["delta"],
        )
    ]
    if confidence < REVIEW_BELOW_CONFIDENCE and concerns:
        reasons = [plan] + concerns[:2] + reasons + concerns[2:]

    return _finish(base, "Carve", confidence, shifts, reasons, rules, risks,
                   factors,
                   {"layer": anchor_layer, "band": anchor_band, "why": anchor_why},
                   before_lte, before_nr, alternatives)


def _finish(base, action, confidence, shifts, reasons, rules, risks, factors,
            anchor, before_lte, before_nr, alternatives=None):
    """Assemble the record and apply the Manager Review routing."""
    total_shift = sum(s["mhz"] for s in shifts)
    needs_review = confidence < REVIEW_BELOW_CONFIDENCE
    recommendation = "Review" if needs_review else action

    if needs_review:
        rules.append({
            "id": "RT-01",
            "name": f"Route to Manager Review below {REVIEW_BELOW_CONFIDENCE}% "
                    f"confidence",
            "outcome": f"confidence {confidence} is below "
                       f"{REVIEW_BELOW_CONFIDENCE}, so the proposed "
                       f"{action} goes to a manager instead of straight through",
        })

    record = dict(base)
    record.update({
        "recommendation": recommendation,
        "proposed_action": action,
        "confidence": confidence,
        "needs_manager_review": needs_review,
        "shifts": shifts,
        "alternatives": alternatives or [],
        "total_mhz_shift": total_shift,
        "after": {
            "mhz_lte": before_lte - total_shift,
            "mhz_nr": before_nr + total_shift,
        },
        "anchor": anchor,
        "reasons": reasons[:3],
        "all_reasons": reasons,
        "rules_fired": rules,
        "risk_flags": risks,
        "confidence_factors": factors,
    })
    return record
