# Workflow: Generate Spectrum Pivot Advisor datasets (Step 1)

## Objective

Produce the four datasets every later step reads: county base data (real Census), and
synthetic network, device, hardware, and spectrum data for the fictional carrier
"GLA - Mobile", covering all 257 counties in IL, WI, and MI.

## Inputs

None from the user. Three public Census sources are fetched and cached in `.tmp/`.

## Tools to use

Run the orchestrator, not the generators individually:

```sh
py -3.12 tools/run_step1.py             # generate + validate
py -3.12 tools/run_step1.py --refresh    # force re-download of raw Census files
```

| Tool | Purpose |
| --- | --- |
| `tools/common.py` | Shared constants, spectrum vocabulary, RNG seeding, cached download, JSON IO |
| `tools/edge_cases.py` | Deterministic edge-case tagging shared by 1b/1c/1d |
| `tools/gen_county_data.py` | 1a — real Census join, density, tier |
| `tools/gen_network_data.py` | 1b — 12-month network + device series |
| `tools/gen_hardware_data.py` | 1c — site inventory and NR-capable share |
| `tools/gen_spectrum_data.py` | 1d — block plans for 600/700/1900/2100, bulk MHz for 2500 |
| `tools/fetch_geometry.py` | County boundaries and US state outlines for the Step 4 map |
| `tools/run_step1.py` | Runs all of the above in order, then validates |

Order matters: 1b–1d read `data/counties.json`, so 1a must run first. 1b must run before
1c/1d only in the sense that it rewrites `counties.json`; the runner already sequences them
correctly.

## Expected outputs

- `data/counties.json` — 257 records, each with a `network.months` array of 12 entries
- `data/hardware.json` — keyed by FIPS
- `data/spectrum.json` — keyed by FIPS, with `bands`, each carrying its own `blocks`
- `data/counties.geojson` — 257 features, ~92 KB
- `data/us_states.geojson` — 51 features (50 states + DC), ~78 KB

Exit code 0 and "All checks passed" means the data is good. Non-zero lists the failures.

## Things learned the hard way

**The Census API needs a key now, and hides it.** `api.census.gov` returns **HTTP 200 with
an HTML "Missing Key" page**, not an error status, so naive code fails later at JSON
parsing with a confusing message. Do not use the API. The keyless static files under
`www2.census.gov` carry the same data. `common.download()` sniffs the first bytes and
raises immediately if it receives HTML.

**Gazetteer header fields have padding spaces.** The header row is `USPS   GEOID  ...`
with whitespace around names, so `row["GEOID"]` raises `KeyError` unless every key is
stripped first. The file is tab-delimited inside the zip and **latin-1**, not UTF-8 —
several county names fail to decode as UTF-8.

**Population estimates need `SUMLEV == '050'`.** The file mixes state and county rows;
without that filter the state totals are silently included as extra "counties".

**Don't hand-simplify geometry.** The raw TIGERweb GeoJSON for 257 counties is **14 MB**,
far too heavy for a static page. TIGERweb's `maxAllowableOffset=0.005` parameter does the
generalization server-side and returns **92 KB**. Writing the GeoJSON compact rather than
indented matters too: `indent=2` inflated it from 92 KB to 343 KB.

**Send a `User-Agent`.** `www2.census.gov` is unreliable with the default urllib agent.

**Seed per county, not globally.** A single global RNG means adding one county reshuffles
every later one, and diffs become unreadable. `common.rng_for(fips)` seeds from the FIPS so
each county is independent and reruns are byte-identical.

**Keep the PCS strip and the 1900 band totals in sync.** They are two views of one fact.
When the minimum-LTE guard moves MHz from NR back to LTE on the 1900 band, it must also
flip blocks in the strip, or validation fails on
"PCS block strip matches the 1900 band MHz totals".

**"Not adjacent to NR" is not the same as "isolated".** In a contiguous owned run with NR
at one end, only the innermost LTE block touches NR — the rest are "not adjacent" but are
still in a group that reaches the NR carrier. A first pass conflated the two and reported
187 isolated counties instead of the intended ~22. Isolation is a property of the **owned
group**: an LTE block is isolated only if its run of owned blocks contains no NR at all.

## Tuning the recommendation mix

The synthetic distributions are deliberately calibrated so every Step 2 branch has
examples. The knobs:

- `gen_network_data.TIER_PARAMS` — device share, congestion, and user split by tier. Rural
  `share_5g` is the strongest lever on how many counties trip the 30% LTE-only Hold gate.
- `edge_cases.TARGETS` — how many counties carry each seeded edge case.
- `gen_spectrum_data` `detached` probability (currently 0.15) — how often a county owns a
  non-contiguous PCS block, which is what creates isolated LTE blocks.

Current state: **41%** of counties trip a Hold gate, **59%** are Carve/Review candidates.
Target after Step 2 is roughly 40% Carve / 40% Hold / 20% Review. Re-measure once the
engine exists and adjust the knobs above rather than the engine's thresholds.

## Edge cases to keep non-empty

The runner prints an edge-case census and asserts each count is non-zero. If a change
drives any to zero, the Step 4 prototype will have a screen with nothing to show. The
conditions: LTE hotter than NR, LTE with more users, LTE-only devices > 30%, LTE-only sites
remaining, isolated PCS block, carveable PCS block, no N2500, no L700.

## Invariants that must never break

- Exactly 257 counties: IL 102, WI 72, MI 83
- All four files agree on the same FIPS set
- Layer names come only from `LTE_LAYERS` / `NR_LAYERS` in `common.py`
- **No county ever has NR on the 700 band** — L700 is the anchor and is never refarmed
- Per band, `mhz_lte + mhz_nr <= mhz_owned`, and nothing is deployed on an unowned band
- Every county retains at least 5 MHz of LTE, so the Step 2 hard rule is satisfiable


## Block plans (added after the first pass)

Four bands carry real FCC block designations at their **real paired widths**. Only 600 MHz
is a clean 5 MHz grid; 700 MHz came out of 6 MHz TV channels and AWS-1 mixes 5s and 10s.
Using a uniform 5 MHz grid everywhere would have been tidier and an RF engineer would have
spotted it immediately.

| Band | Blocks | Total |
| --- | --- | --- |
| 600 | A B C D E F G (5 each) | 35 MHz |
| 700 | Lower A 6, Lower B 6, Lower C 6, Upper C 11, Upper D 5 | 34 MHz |
| 1900 | the 13-block PCS plan (5 each) | 65 MHz |
| 2100 | A 10, B 10, C 5, D 5, E 5, F 10 | 45 MHz |

2500 stays bulk — BRS/EBS leasing has no clean block grid.

**Band MHz totals are derived from the blocks**, never stored separately, so the strip and
the totals cannot drift apart. A validation check asserts they agree for every band of every
county, along with block order and per-block width.

**Adding the real 700 MHz widths changed every county's LTE total**, because the old model
used uniform 5 MHz blocks and real 700 blocks are 6/11/5. That invalidated the Step 3a
explanations, which cite MHz figures — they had to be regenerated ($3.36). Any future change
to band widths carries the same cost; check before changing them.

**Don't compute adjacency or isolation on 700 MHz.** It has no NR layer anywhere in its
plan, so every 700 block reads as "isolated from the NR carrier" — technically true and
completely meaningless. The first run reported 238 counties with an isolated block; excluding
700 brought it to 74, which is the real number.

**Blocks live on their band, never in a flat duplicate list.** The first version wrote both,
which tripled `spectrum.json` to 5.4 MB and re-introduced exactly the two-copies-of-one-fact
problem the derived totals were meant to prevent. `common.band_blocks()` and
`common.all_blocks()` are the accessors.
