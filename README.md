# Spectrum Pivot Advisor

An AI-assisted tool that helps RF engineers decide when to move LTE spectrum to 5G NR,
county by county, across Illinois, Wisconsin, and Michigan — 257 counties
(IL 102, WI 72, MI 83).

> **Census data is real; all network, device, hardware, and spectrum data is synthetic for a
> fictional carrier.**

"GLA - Mobile" is a **fictional carrier**. No real carrier's network performance, device
mix, site inventory, or spectrum holdings appear anywhere in this project. County names,
FIPS codes, population, land area, and boundaries come from the real US Census; everything
else is generated.

## Status

| Step | What it is | State |
| --- | --- | --- |
| 1 | Data generation (Census + synthetic network/hardware/spectrum) | **Done** |
| 2 | Rule-based recommendation engine | **Done** |
| 3 | LLM explanation layer, local chat backend, MCP server | **Done** |
| 4 | Single-file HTML prototype (Leaflet + Chart.js) | **Done** |
| 5 | Case study page | **Done** |

## Requirements

**Python 3.12.** Invoke it as `py -3.12` — the default `python` on the development machine
is 3.7, which is too old for the Step 3 dependencies (`anthropic`, `fastapi`, `mcp`).

Steps 1 and 2 use **only the standard library**. Step 3 needs:

```sh
py -3.12 -m pip install anthropic python-dotenv fastapi "uvicorn[standard]" mcp
```

and an Anthropic API key in `.env` (copy `.env.example`). `.env` is gitignored.

## Running it

```sh
cd refarm_advisor
py -3.12 tools/run_step1.py             # Step 1: generate data, then validate
py -3.12 tools/run_step1.py --refresh   # also re-download the raw Census files
py -3.12 tools/gen_recommendations.py   # Step 2: run the rule engine, then validate

py -3.12 tools/gen_explanations.py --limit 5   # Step 3a: test run, 5 counties
py -3.12 tools/gen_explanations.py             # Step 3a: all remaining counties
py -3.12 tools/chat_backend.py                 # Step 3b: local chat, port 8000
py -3.12 tools/mcp_server.py                   # Step 3c: MCP server (stdio)
py -3.12 tools/test_mcp_server.py              # Step 3c: 23 checks
```

Raw Census downloads are cached in `.tmp/`, so only the first run needs network access.
Individual generators can be run on their own (`py -3.12 tools/gen_spectrum_data.py`), but
1b–1d read `data/counties.json`, so 1a has to run first.

The runner finishes with 24 assertions covering county counts, cross-file FIPS agreement,
spectrum arithmetic, the L700 anchor rule, and an edge-case census. It exits non-zero on
any failure.

## Outputs

| File | Contents |
| --- | --- |
| `data/counties.json` | 257 counties: real Census identity/population/area, density tier, and a 12-month synthetic network + device series |
| `data/hardware.json` | Synthetic site inventory and NR-capable share per county |
| `data/spectrum.json` | Synthetic MHz owned / on LTE / on NR per band, plus block plans for 600 / 700 / 1900 / 2100 |
| `data/counties.geojson` | Real Census county boundaries, generalized for web use (92 KB) |
| `data/us_states.geojson` | Real Census state outlines, map context (78 KB) |
| `data/recommendations.json` | Step 2: per-county Carve/Hold/Review, confidence, reasons, rules fired, risk flags |
| `data/explanations.json` | Step 3a: AI-generated summary, risks, LTE-only impact, manager note |

## Data sources (real)

The Census **API** now requires a key and fails in a way that is easy to miss: it returns
HTTP 200 with an HTML "Missing Key" page instead of JSON. This project avoids it entirely
and uses keyless static files instead.

1. **Land area + centroid** — [2024 Gazetteer county file](https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_counties_national.zip)
   (`ALAND_SQMI`, `INTPTLAT`, `INTPTLONG`)
2. **Population** — [Vintage 2024 county population estimates](https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/counties/totals/co-est2024-alldata.csv)
   (`POPESTIMATE2024`)
3. **Boundaries** — TIGERweb `State_County` MapServer, GeoJSON, with
   `maxAllowableOffset=0.005` for server-side generalization

Both tabular sources yield exactly 257 counties for IL/WI/MI and join cleanly on 5-digit
FIPS with zero unmatched records.

## Synthetic data model

**Density tiers** — `population / land_area_sqmi`; urban ≥ 500, suburban 100–500,
rural < 100. Result: 20 urban, 55 suburban, 182 rural. This footprint is genuinely rural
(median density 51/sq mi), which is why Hold is a common recommendation.

**Determinism** — every county seeds its own RNG from its FIPS, so runs are byte-identical
and editing one county never shifts another.

**Spectrum vocabulary** — these are the only valid layers:

- LTE: `L600`, `L700`, `L1900` (PCS), `L2100` (AWS), `L2500`
- NR: `N600`, `N1900` (PCS), `N2100` (AWS), `N2500`

Refarm paths are same-band only: `L600→N600`, `L1900→N1900`, `L2100→N2100`, `L2500→N2500`.
**`L700` has no NR counterpart and is never refarmed — it is the LTE anchor.** The
validation suite asserts no county ever carries NR on the 700 band.

**Block plans** — four bands are modelled at block level using **real FCC designations and
their real paired widths**. The widths are deliberately not uniform, because they aren't in
reality — 700 MHz came out of 6 MHz TV channels and AWS-1 mixes 5s and 10s. Modelling them
all as 5 MHz would have been tidier and wrong.

| Band | Blocks (frequency order) | Total |
| --- | --- | --- |
| 600 (n71) | A B C D E F G — 5 MHz each | 35 MHz |
| 700 | Lower A 6 · Lower B 6 · Lower C 6 · Upper C 11 · Upper D 5 | 34 MHz |
| 1900 (PCS) | A3 A4 A5 D B3 B4 B5 E F C3 C4 C5 G — 5 MHz each | 65 MHz |
| 2100 (AWS-1) | A 10 · B 10 · C 5 · D 5 · E 5 · F 10 | 45 MHz |

**2500 is held as bulk MHz with no block grid** — BRS/EBS channel leasing doesn't map to
one. Per-band MHz totals are *derived* from the blocks a county owns, so a block strip and
its band totals cannot disagree.

700 MHz is the LTE anchor and has no NR layer anywhere in its plan, so adjacency and
isolation are not computed there — every 700 block would otherwise read as "isolated",
which is noise rather than a finding.

Each owned block carries two contiguity facts, because they drive different decisions:

- `adjacent_to_nr` — the block touches an owned NR block, so carving it would widen the
  existing NR carrier. This is what the Step 2 carve rule tests.
- `isolated` — the block sits in a run of owned blocks containing no NR at all, cut off
  from the NR carrier by spectrum the carrier does not own. Carving it would strand a
  narrow NR carrier, so it stays LTE.

## Deliberate edge cases

A mostly-rural footprint would leave several Step 2 branches with no examples, so specific
counties are tagged in `tools/edge_cases.py` and the generators honour those tags:

| Condition | Counties |
| --- | --- |
| LTE more congested than NR | 48 |
| LTE carrying more users than NR | 47 |
| LTE-only device share > 30% | 65 |
| LTE-only sites still on air | 72 |
| Isolated LTE block (any band) | 74 |
| Carveable block (adjacent to NR) | 255 |
| No N2500 layer deployed | 104 |
| No L700, so the anchor must come from another band | 20 |

41% of counties currently trip at least one Hold condition; 59% are Carve/Review
candidates before hardware and spectrum constraints are applied in Step 2.

## Step 2: the recommendation engine

Rule-based, deterministic, and explainable. Every county gets a recommendation, a confidence
score, the top three reasons in plain English, the rules that fired, and risk flags.
Confidence below 60 routes to Manager Review. See
[workflows/recommend_refarm.md](workflows/recommend_refarm.md) for the full rule set and the
reasoning behind each design decision.

**Hard rules (never broken):** HR-01 keep ≥ 5 MHz LTE · HR-02 preserve an LTE anchor ·
HR-03 same-band paths only · HR-04 freed spectrum must be reused for NR · HR-05 Hold if LTE
is hotter or busier than NR.

**Judgment rules:** JR-01 Hold above 30% LTE-only devices · JR-02 keep a full LTE carrier
where sites are not all NR-capable · JR-03 only carve PCS blocks adjacent to NR · JR-04 an
isolated LTE block stays LTE · SEL-01 one layer at a time · RT-01 review below 60%.

| | Counties | Share |
| --- | --- | --- |
| Carve | 107 | 42% |
| Hold | 105 | 41% |
| Review | 45 | 18% |

Total refarmed: **3,540 MHz** across 257 counties.

The engine recommends **one layer at a time** and records other eligible layers as
`alternatives`. An earlier version carved every eligible layer at once: it passed every hard
rule but moved 52% of all LTE spectrum and left 29% of counties sitting on the 5 MHz floor,
which is not a plan an engineer would sign. `tools/rules_engine.py` is importable, and Step 3
should call it rather than reimplementing any of this.

`gen_recommendations.py` re-validates all five hard rules against the engine's own output
rather than trusting it — 14 checks, all of which must pass.

## Step 3: the LLM layer

**The rule engine decides; the model only explains.** This is enforced structurally, not by
instruction: explanations are written to a separate file and never read back into
`recommendations.json`, the chat endpoint has no write path, and the MCP server exposes only
read tools (a test asserts no `set_`/`approve_`/`update_` tool is ever added). If an
explanation is missing or fails validation, the prototype falls back to the rule engine's
own reasons — the screen never depends on the model being available.

See [workflows/llm_layer.md](workflows/llm_layer.md) for the full design and what went wrong
along the way.

### Two guardrails, both enforced on every response

**Number validation** — every number in the output must appear in the input. A model
restating 42.7% as "about 43%" reads perfectly well and is wrong; an engineer who catches
one such number stops trusting the whole screen.

**Prose hygiene** — internal identifiers must never reach engineer-facing text: rule IDs
(`HR-02`), risk codes (`lte_only_sites_remaining`), raw field names (`mhz_lte`). The
forbidden set is derived from the payload itself, so it stays correct as the data changes.

A failure of either names exactly what was wrong and retries (up to 3 attempts), then flags
rather than silently accepting. Prompt wording alone got prose hygiene to ~93% of counties;
the retry loop closed the remaining 7%.

**Current state: 257/257 validated** — 225 on the first attempt, 32 needed one retry, 0
invented numbers, 0 leaked identifiers, 46/46 review counties carry a manager note, 0 stray
notes. Full run cost **$3.36** on `claude-sonnet-5`.

### MCP server — connecting to Claude Desktop

Add this to `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`, macOS:
`~/Library/Application Support/Claude/`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "spectrum-pivot-advisor": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["C:\\MyProject\\refarm_advisor\\tools\\mcp_server.py"]
    }
  }
}
```

Use the **full path to `python.exe`**, not `py` — Claude Desktop does not reliably resolve
the launcher from PATH. Both paths must be absolute; the server resolves its data directory
from its own location, so the working directory does not matter.

Four read-only tools: `get_county(fips)`, `list_review_queue(state=None)`,
`get_state_summary(state)`, `compare_counties(fips_list)`. The first and last accept a
county name as well as a FIPS code.

### Example prompts

> "Which Michigan counties are waiting for manager review and why?"

> "Compare Cook County, Milwaukee County, and Keweenaw County — why does the recommendation
> differ across them?"

> "Give me the Wisconsin summary. Which three counties would free the most spectrum, and
> what's holding back the rest?"

## Step 5: the case study page

`index.html` is the case study and the entry point; it links to the prototype. Serve the
folder and open `http://localhost:8080/` — GitHub Pages will serve `index.html` at the root
automatically.

Twelve sections per the brief: hero, problem, research, personas, current-state journey map,
service blueprint, design principles, the solution walkthrough, designing the AI layer,
future-state journey map, outcomes, and honest limitations. The **journey maps and the
service blueprint are HTML and inline SVG**, not images, so they stay legible and
selectable at any size.

The page is **final** — no `[PLACEHOLDER]` or `[DRAFT]` markers remain. Nothing in the
research section was invented: the quotes and insight cards are reproduced as given, with
the four attributions supplied by the author.

`assets/demo.gif` is a 26-second silent walkthrough (900px, 2.4 MB, lazy-loaded) recorded by
scripting the live prototype through the DevTools Protocol and assembling the frames with
Pillow. Runs of identical frames are collapsed into one frame with a longer duration, which
is what keeps 235 captured frames down to 34 stored ones.

Screenshots in `assets/` are captured from the running prototype by clipping real elements
via the DevTools Protocol, so they re-generate correctly if the layout changes.

## Step 4: the prototype

```sh
py -3.12 -m http.server 8080      # then http://localhost:8080/prototype.html
```

**Serve it over HTTP** — `file://` blocks `fetch`, so the data never loads. The page detects
this and prints the commands rather than failing silently.

One self-contained file, no build step: Leaflet map of all 257 counties coloured by
recommendation, filters in a single row, and a county detail panel carrying the
recommendation, confidence, top three reasons, the full rules-fired audit trail, risk flags,
the AI explanation, 12-month congestion / users / device-mix charts, hardware readiness,
spectrum before-and-after per band, the 13-block PCS strip, and approve / adjust / reject.
Plus a Manager Review queue and a Summary tab.

**Cook County opens by default**, so the screen lands on a worked example rather than an
empty panel. A **← Case study** button in the header returns to `index.html`.
Shareable URLs: `?fips=17031`, `?tab=review`, `?expand=1`.

The prototype is **dark only** — the theme is pinned on the `<html>` element rather than
following the visitor's OS, so the case-study screenshots always match the live tool.

### The map drills down

The default view is the **contiguous US** with the three-state footprint outlined and its
counties coloured, so it's clear where the carrier operates. Picking a state zooms to that
state; picking a county zooms to the county; a "Zoom out to US" button returns. All 50
states plus DC are in the data — Alaska and Hawaii aren't drawn, because fitting to all 50
shrinks the footprint to a speck and fitting to the lower 48 while drawing them crops Alaska
into the corner.

### Colour was computed, not chosen

The palette went through the dataviz validator. The obvious choice failed:

| Candidate | Result |
| --- | --- |
| Traffic light — green / amber / red | **FAIL** — green↔red ΔE **4.1** for deuteranopia |
| **Aqua / violet / yellow** | **PASS** both modes, worst all-pairs ΔE 9.1 light, 8.4 dark |

Validated on the all-pairs list because a choropleth needs it. LTE (blue) and NR (orange)
take the first two categorical slots since that pair recurs in every chart. Light-mode aqua
and yellow fall below 3:1 contrast, so the **recommendation word is always shown as text** —
colour never carries the meaning alone.

### Verified by driving it, not just rendering it

The interactive paths were exercised through the Edge DevTools Protocol: **18 interaction
checks and 5 chat checks, all passing**. Most importantly, the Adjust slider mirrors HR-01 —
it cannot be dragged to a point that would leave a county below 5 MHz of LTE, so the UI
can't propose what the engine would reject. The PCS strip follows the slider block by block.

Full details and what broke along the way: [workflows/prototype.md](workflows/prototype.md).

## Layout

```
data/            generated datasets (committed - the prototype reads them directly)
tools/           Python generators and the validating runner
workflows/       SOPs describing what to run and what to watch out for
.tmp/            cached raw Census downloads (gitignored, regenerable)
```

## Publishing to GitHub Pages

The repo is the site: `index.html` is the case study, `prototype.html` is the tool, and
both read `data/` and `assets/` by **relative path**, so it works whether it is served from
a user page or a project page under a subdirectory.

1. `git init && git add -A && git commit -m "Spectrum Pivot Advisor"`
2. Push to GitHub.
3. Settings → Pages → Source: deploy from branch, root folder.

`.env` and `.tmp/` are gitignored — verified by staging the whole tree in a scratch repo and
confirming neither appears. `.nojekyll` is committed so Pages serves the files as-is.

Nothing on the published site calls the Anthropic API. The explanations were generated
offline and ship as `data/explanations.json`; the chat backend and MCP server are local-only
and the page only looks for the backend when it is itself served from localhost.

## License

Two parts, because this repository holds two different kinds of work — see
[LICENSE](LICENSE) for the full text.

| What | Licence |
| --- | --- |
| Source code — `tools/*.py`, `index.html`, `prototype.html` | **MIT** — use it, learn from it, build on it |
| Case study, README, `workflows/`, `assets/`, `data/` | **CC BY-NC-ND 4.0** — credit required, non-commercial, no derivatives |

Presenting this case study or its research as your own work is not permitted.

US Census data (county names, FIPS, population, land area, boundaries) is a US
Government work in the public domain. Leaflet and Chart.js load from a CDN and
are not redistributed here.
