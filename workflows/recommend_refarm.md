# Workflow: Run the refarm recommendation engine (Step 2)

## Objective

Turn the Step 1 datasets into an auditable per-county recommendation — Carve, Hold, or
Review — with a confidence score, plain-English reasons, the rules that fired, and risk
flags. The engine is deterministic and rule-based. No model is involved at this stage.

## Inputs

`data/counties.json`, `data/hardware.json`, `data/spectrum.json` — Step 1 must have run.

## Tools to use

```sh
py -3.12 tools/gen_recommendations.py
```

| Tool | Purpose |
| --- | --- |
| `tools/rules_engine.py` | The rules. `evaluate(county, hardware, spectrum)` returns one record. Import this from Step 3's chat backend and MCP server — do not reimplement the logic there. |
| `tools/gen_recommendations.py` | Runs the engine over all 257 counties, writes `data/recommendations.json`, then re-validates the hard rules against the output |

Output: `data/recommendations.json` — summary, review queue, and a record per county.

## The rules, and their IDs

Rule IDs are stable and appear in each county's `rules_fired` audit trail.

**Hard rules — never broken:**

| ID | Rule |
| --- | --- |
| HR-01 | Keep at least 5 MHz of LTE in every county |
| HR-02 | Preserve an LTE anchor layer |
| HR-03 | Only same-band refarm paths are valid |
| HR-04 | Freed spectrum must be reused for NR |
| HR-05 | Hold if LTE is more congested, or carries more users, than NR |

**Judgment rules — affect recommendation and confidence:**

| ID | Rule |
| --- | --- |
| JR-01 | Hold when LTE-only device share exceeds 30% |
| JR-02 | Keep a full LTE carrier (10 MHz) where sites are not all NR-capable, and lower confidence |
| JR-03 | Only carve PCS blocks adjacent to existing NR blocks |
| JR-04 | An isolated LTE block stays LTE |
| SEL-01 | Recommend one layer at a time; other eligible layers become `alternatives` |
| RT-01 | Route to Manager Review below 60% confidence |

## Anchor selection (HR-02)

In priority order:

1. Holds both L700 and L600 → **L700 anchors**, L600 is free to move to N600
2. Holds L700 → L700 anchors (it has no NR path, so it can never be refarmed anyway)
3. Holds L600 only → L600 anchors, since it is the only LTE low band
4. No LTE low band → L1900, else L2100, holds the anchor and costs 6 confidence points
5. Only L2500 → it anchors

The anchor band retains the floor (5 MHz, or 10 MHz under JR-02); anything above that is
carveable. This makes HR-01 and HR-02 satisfy each other rather than competing.

## Decisions made while building this

**Recommend one layer at a time (SEL-01).** The first version carved every eligible layer
in one pass. It passed every hard rule, but it moved 52% of all LTE spectrum and left 75
counties (29%) sitting exactly on the 5 MHz floor — and the `thin_lte_margin` flag fired on
all of them. An engineer seeing "drop LTE to 5 MHz" across a third of the state would reject
the tool, not the plan; the interviews describe 5 MHz as the constrained fallback, not the
target. Recommending the single best move dropped the total to 3,445 MHz (22%), eliminated
the thin-margin flag entirely, and cut `new_nr_layer` from 73 counties to 3. The remaining
eligible layers are kept in `alternatives`, which is what the Step 4 Adjust flow offers.

**Prefer widening an existing NR carrier over standing up a new layer.** Candidate ranking
is `(creates_new_nr_layer, -mhz, band_priority)`. This is what collapsed `new_nr_layer` from
73 to 3 — those 3 are counties where a new layer is the only option.

**Apply the LTE floor during selection, not as a correction.** An earlier version picked a
candidate then clawed MHz back to protect the floor, which could empty the shift entirely
while other eligible candidates sat unused in `alternatives`. Now the floor is a filter: take
the best candidate that fits, trimming it if only part fits, and fall through to the next if
none of it does.

**Low-confidence counties lead with concerns, not rationale.** For a county going to Manager
Review, the three things a reviewer needs are what is proposed and why it is shaky. When
confidence is below 60, the largest confidence penalties are promoted into the top three
reasons ahead of the supporting case. `all_reasons` keeps the full list either way.

**Don't overstate a thin margin.** A 0.5-point gap was being described as "NR is the busier
layer". That kind of overstatement is what makes an engineer distrust everything else on the
screen, so below a 5-point margin the wording changes to "only marginally busier … the case
rests on the user split more than on load".

**A hard-rule Hold should say what must change.** A Hold that only lists what is wrong gives
the engineer nothing to act on. Each Hold now closes with either the device-migration
blocker or "revisit once NR load overtakes LTE".

**Two confidence factors that were missing.** `lte_load_rising` did nothing at first because
Step 1 trended LTE congestion down in every county — the rule was live but unreachable. Rather
than delete it, Step 1 gained an `lte_rising` edge case (24 counties: load climbing but still
below NR), because that is a real pattern and direction of travel matters as much as level.
`Thin market` (under 4,000 users or 10 sites) was added because monthly PRB and device
figures in a tiny county swing on very little traffic, which is a genuine reason for a human
to confirm.

## Confidence model

Carve starts at a base of 62 and every adjustment is recorded in `confidence_factors` with
its own label and delta, so a score can be reconstructed from the output.

| Factor | Delta |
| --- | --- |
| NR busier than LTE | up to +12 (margin ÷ 3) |
| 5G device share ≥ 85 / ≥ 75 / ≥ 65 | +10 / +6 / +2 |
| All sites NR-capable | +8 |
| ≥ 70% of users already on NR | +6 |
| LTE-only share above the 20% caution line | −6, steepening toward the 30% threshold |
| Sites not all NR-capable (JR-02) | −8 to −24, scaled by the LTE-only site share |
| Thin market (< 4,000 users or < 10 sites) | −11 |
| LTE congestion trending up | −9 |
| No LTE low band for the anchor | −6 |
| Isolated PCS block | −5 |
| Stands up a new NR layer | −7 |

Holds are scored separately and high, because a hard-rule Hold is a confident call: base 84
for HR-05 (rising with the margin), base 78 for JR-01 (rising with how far above 30% the
LTE-only share sits).

## Current output

| | Counties | Share |
| --- | --- | --- |
| Carve | 106 | 41% |
| Hold | 105 | 41% |
| Review | 46 | 18% |

Total refarmed: **3,445 MHz**. Review queue: **46**. Confidence spreads across 28–98 with no
clustering. This lands on the 40/40/20 target set in Step 1.

If the mix drifts, tune the Step 1 generators (`gen_network_data.TIER_PARAMS`,
`edge_cases.TARGETS`) rather than these thresholds — the thresholds represent planning rules
the team is supposed to trust, and quietly moving them to make the demo look better would
defeat the point of the engine.

## Validation

`gen_recommendations.py` re-checks every hard rule against the engine's own output rather
than trusting that the engine honoured it. A hard rule the engine believes it kept but
actually broke is the failure that would destroy an engineer's trust, so each is asserted
independently: the LTE floor, anchor survival, L700 preference, valid same-band paths, the
before/after MHz balance, no carve where LTE is hotter or busier, no carve above 30% LTE-only,
PCS adjacency and isolation, shifts never exceeding the LTE on that band, one move per
county, the output contract, and the Review routing threshold.

Determinism: the engine is pure over its inputs, so `recommendation.json` is byte-identical
across runs. Verified by diffing two consecutive runs.
