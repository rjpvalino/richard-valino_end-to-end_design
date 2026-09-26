# Workflow: The prototype (Step 4)

## Objective

One self-contained HTML file that gives an RF engineer everything needed to decide a county
on a single screen — map, recommendation, the rules behind it, trends, spectrum before and
after, the block plans, the AI explanation, and the approve / adjust / reject actions.
No build step; host-ready for GitHub Pages.

## Running it

```sh
cd refarm_advisor
py -3.12 -m http.server 8080
# then http://localhost:8080/prototype.html
```

**It must be served over HTTP.** Opening `prototype.html` with `file://` blocks `fetch`, so
the five data files never load. The page detects this and prints the exact commands rather
than failing silently.

URL parameters, all genuinely useful rather than test hooks:

| Param | Effect |
| --- | --- |
| `?fips=17031` | Opens that county — makes a specific county shareable between an engineer and a manager. Cook County opens by default when none is given |
| `?tab=review` / `?tab=summary` | Opens a tab directly |
| — | The theme is pinned to dark on the `<html>` element; there is no toggle and no theme parameter |
| `?expand=1` | Renders the county panel at full height instead of scrolling inside its card — for screenshots and printing |

## Colour: computed, not chosen

The palette was run through the dataviz validator rather than picked by eye, and the first
two candidates failed:

| Candidate | Result |
| --- | --- |
| Status palette — good / warning / critical | **FAIL** — green↔red CVD ΔE **4.1** (deutan). The classic traffic-light trap. |
| Status palette — good / warning / serious | **FAIL** — ΔE 5.6 CVD, and 13.6 normal-vision, below the 15 floor |
| **Aqua / violet / yellow** | **PASS** both modes, worst all-pairs ΔE **9.1** light, **8.4** dark |

The map is a choropleth, so it is validated on the `--pairs all` list, not the easier
adjacent list. Final assignment:

| Role | Light | Dark | Why |
| --- | --- | --- | --- |
| Carve | `#1baf7a` | `#199e70` | reads positive |
| Hold | `#eda100` | `#c98500` | reads caution |
| Review | `#4a3aa7` | `#9085e9` | reads "needs a person" |
| LTE | `#2a78d6` | `#3987e5` | categorical slot 1 |
| NR | `#eb6834` | `#d95926` | categorical slot 2 |

LTE↔NR is the most repeated encoding in the tool — trend charts, spectrum bars, PCS strip,
device mix — so it takes the first two categorical slots, and the recommendation scale was
chosen to avoid colliding with them.

Light-mode aqua (2.74:1) and yellow (2.11:1) sit below 3:1 on the surface, which triggers
the **relief rule**: the recommendation word is always present as text — in the legend, the
map tooltip, the county header, the review queue, and the summary table. Colour never
carries the meaning alone.

## Chart rules followed

- **No dual-axis.** Congestion (%) and users (count) are two separate charts, never one
  plot with two scales.
- 2px lines, 8px markers with a 2px surface ring, 14px hit radius.
- Stacked device bars get a **2px surface-coloured border on the touching edge** — that is
  how you get the spacer in Chart.js; without it the two segments read as one block.
- Solid hairline gridlines, never dashed. No number on every point.
- Legend on every chart with 2+ series.
- **A table view exists** — "Show data table" swaps all three charts for the 12 monthly
  rows, so a tooltip is never the only way to read a value. The current values also appear
  as stat tiles above the charts.
- Filters sit in **one row above** the content, never inside a chart card.

## Things that went wrong

**Leaflet measured the container before layout settled.** The first render put all 257
counties in a small blob surrounded by empty space. Fixed by fitting inside
`requestAnimationFrame` and adding a `ResizeObserver`. The same bug came back with
`?expand=1`, which changes the map's height — so the expand class is now applied *before*
`initMap()` rather than after.

**Inline `<i>` swatches collapsed to nothing.** `.sw` sets width and height, but an `<i>`
is inline, so the legend keys under the spectrum bars and PCS strip silently rendered with
no colour at all. The map legend worked only because `.legend span` happened to be
`display:flex`. Both now share an explicit rule.

**Screenshots need absolute paths and a fresh profile.** Headless Edge exits 0 and writes
nothing when the `--screenshot` path is relative or the profile directory is reused. Run it
from PowerShell with absolute paths and a per-run `--user-data-dir`.

## Verification

Rendering it is not enough — the interactive parts were driven through the Edge DevTools
Protocol (`scripts/` in the scratchpad during development) and checked:

- Adjust slider: after-values update live, the spectrum bars and PCS strip re-render on
  drag, LTE never drops below 5 MHz at slider maximum (HR-01 mirrored in the UI so it
  cannot propose what the engine would reject)
- PCS block strip follows the slider — 5 MHz moves 1 block, 10 MHz moves 2
- Approve / Reject / Undo write and clear decisions, and the summary total follows
- Filters compose: WI = 72, WI + Review = 11, reset = 257
- Table view renders 12 rows
- Review tab lists every queued county, least confident first
- Chat: detects the backend, answers in scope, **refuses out of scope**

All 18 interaction checks plus 5 chat checks passed.

## One real bug the number validator could not catch

Asked "how much LTE is left and on which layer", the chat answered that LTE remained on
"L700, L1900, L2100, and the remaining **L2500** spectrum" for Cook County. Every number in
that sentence was correct, so Step 3's number validation passed it — but L2500 goes to
**zero** after that carve, and L600 was omitted entirely.

The lesson: number validation catches fabricated figures, not wrong reasoning about the
figures. The fix was to stop asking the model to infer it — `build_payload` now computes
`lte_layers_after`, `nr_layers_after`, and per-band `mhz_lte_after` / `mhz_nr_after`, and
the system prompt says to use them rather than subtract. The same question now answers:
"LTE totals 50 MHz, carried on L600, L700, L1900, and L2100. L2500 carries no LTE after
the carve (0 MHz)."

An audit of all 257 pre-generated explanations for this error class found **zero** — the
structured schema and retry loop in 3a kept them clean; the freeform chat path had neither.
Worth remembering when adding any new freeform surface.

## Known limitations

- Decisions live in `localStorage` — this is a prototype, not a system of record. The
  Summary tab says so and offers a clear-all.
- Adjust is disabled on a Hold, since there is nothing to move.
- The three data files total ~3.3 MB uncompressed; GitHub Pages gzips them, but a
  production version would trim the 12-month series server-side.


## The map drills down (added after review)

Three levels, one function. `fitTo(level, key)` is the only place the view moves, so the
levels cannot disagree:

| Level | Trigger | Max zoom |
| --- | --- | --- |
| US | default, "Zoom out to US", or clearing the state filter | 6 |
| State | picking a state in the filter | 8 |
| County | clicking a county, or a `?fips=` link | 9 |

Measured: zoom 3 at US, 6 at state, 9 at county.

**Cook County opens by default** so the first screen is a worked example, but the initial
selection deliberately does *not* zoom — `selectCounty(fips, zoom=false)` — otherwise the
default view would be a single county instead of the national context.

**Alaska and Hawaii are in the data but not drawn.** Fitting to all 50 states shrinks the
three-state footprint to a speck; fitting to the lower 48 while still drawing them crops
Alaska into the top-left corner, which reads as a rendering bug rather than a choice. The
contiguous 48 plus DC are the context frame.

**Dark only, by decision.** The prototype originally followed the visitor's OS setting and
had a light/dark toggle. Pinning it to dark removed a whole class of inconsistency: the
screenshots in the case study are now guaranteed to match what a reader sees when they open
the live tool. The light tokens stay in the stylesheet because the dark ones are written as
overrides of them.

**The state fills were invisible at first.** Both the map background and the state fill were
`--surface-2`, so the US outline simply did not appear. The map plane is now `--page` and
the states are `--surface-2`, which separates them in both themes.

## Block strips across four bands

One strip per banded band — 600, 700, 1900 PCS, 2100 AWS — plus a bulk row for 2500.
**Cells are sized in proportion to their real MHz width** via `flex: <width> 1 0`, because
the widths genuinely differ: Upper C at 11 MHz renders visibly wider than the 6 MHz Lower
blocks, and AWS A/B/F at 10 MHz wider than C/D/E at 5. Measured ratio for a 10 MHz block
against a 5 MHz block: **1.95x**.

Each cell shows the block name, its width in MHz, and a marker — `→NR` for blocks the
current recommendation moves (which follows the Adjust slider), `iso` for a block isolated
from the NR carrier. The strip footer states that 700 MHz is the anchor and is never
refarmed, since that is the question the 700 row invites.


## The demo GIF

`assets/demo.gif` is generated, not hand-recorded: a script drives the live prototype
through the DevTools Protocol, captures PNG frames, and Pillow assembles them. Re-run it
after any UI change so the demo never drifts from the tool.

Two things that matter for the output:

- **Collapse runs of identical frames.** Most of a walkthrough is a static screen being
  read. Folding those into a single frame with a longer duration took 235 captured frames
  to 34 stored ones — the difference between a usable file and a huge one.
- **One shared palette, not per-frame.** Per-frame palettes look marginally better and stop
  GIF reusing them, which costs far more than it gains.
- **No dithering, 128 colours.** The UI is flat dark panels with almost no gradients, so
  dithering only sprays noise that defeats run-length compression. Measured: **5.00 MB**
  dithered at 255 colours against **2.41 MB** undithered at 128, with no visible difference
  in the text. Check this rather than assume it — it was the single biggest saving.

Two mistakes worth not repeating: zooming the map to a small county left it as an
unreadable dark blob for most of the run (the script now pulls back to the state after
showing the zoom), and stepping the adjust slider 5 MHz at a time generated so many unique
frames that the review and summary screens were squeezed to the end.
