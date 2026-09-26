# Workflow: The LLM layer (Step 3)

## Objective

Let the model explain the rule engine's decisions and answer questions about them,
without ever changing a recommendation or a confidence score. Three pieces:

| Part | What | Runs where |
| --- | --- | --- |
| 3a | Pre-generated explanations, one per county | Offline, output ships as JSON |
| 3b | "Ask about this county" chat | Local demo only (FastAPI) |
| 3c | MCP server, read-only | Local, connected to Claude Desktop |

## The division of labour

The rule engine decides. The model explains. This is enforced by structure, not by
instruction:

- **3a** receives the finished recommendation and writes prose about it. Its output is
  stored in a separate file (`explanations.json`); nothing it produces is ever read back
  into `recommendations.json`.
- **3b** has no write path at all — it answers questions and returns text.
- **3c** exposes only read tools. There is no `set_`, `approve_`, or `update_` tool, and a
  validation check asserts none is ever added.

If an explanation is missing or fails validation, the prototype falls back to the rule
engine's own plain-English reasons, so the screen never depends on the model being available.

## Prerequisites

```sh
py -3.12 -m pip install anthropic python-dotenv fastapi "uvicorn[standard]" mcp
```

`ANTHROPIC_API_KEY` in `.env` (gitignored; `.env.example` is the template). Steps 1 and 2
must have run — the LLM layer reads their output.

## 3a — Pre-generated explanations

```sh
py -3.12 tools/gen_explanations.py --limit 5     # test run first, always
py -3.12 tools/gen_explanations.py               # all remaining counties
py -3.12 tools/gen_explanations.py --fips 17031  # one county
py -3.12 tools/gen_explanations.py --force       # regenerate existing
```

Pre-generating is what lets the prototype work as a static page on GitHub Pages with no
backend. Runs are **resumable** — counties already in `explanations.json` are skipped
unless `--force`, so an interrupted run costs nothing to repeat. Six workers, and the
system prompt is cached across calls.

Output per county: `summary` (2–3 sentences), `risks` (exactly 1–2), `lte_only_impact`
(1 sentence), and `manager_note` (only when the county is routed to review).

**Cost, measured:** 257 counties = **$2.81** on `claude-sonnet-5` (580K input, 165K output
tokens). Always run `--limit 5` first and read the output before spending the rest.

## The two guardrails

Both run on every response. A failure names exactly what was wrong and retries, up to 3
attempts; after that the output is stored but flagged, never silently accepted.

**1. Number validation.** Every number in the output must appear in the input. The model
restating 42.7% as "about 43%" reads perfectly well and is wrong, and an engineer who
catches one such number stops trusting the whole screen. A small whitelist covers
structural numbers (`0 1 2 3 12 100`) that appear in ordinary sentences.

**2. Prose hygiene.** Internal identifiers must not reach engineer-facing text — rule IDs
(`HR-02`, `SEL-01`), risk codes (`lte_only_sites_remaining`), or raw JSON field names
(`mhz_lte`). The forbidden set is derived from the payload itself (every snake_case key
plus every risk code), not hand-listed, so it stays correct as the data shape changes.

## Things learned while building this

**The prompt alone got prose hygiene to ~93%.** After the first full run, 21 of 257
counties still had a rule ID or a field name in the prose — sentences like "L700 remains
the LTE anchor per HR-02" and "given pct_5g_devices moved from 59.5% to 69.1%". Tightening
the prompt was not enough on its own; what fixed it was making it a *check that can fail
and retry*. That is the general lesson: a guardrail you can measure beats a prompt you hope
about, and the residual 7% is exactly the part a prompt won't reach.

**The first prompt produced circular risks.** Early output asked the engineer to "confirm
the alternatives were correctly deferred under SEL-01" — that is auditing the engine's
bookkeeping, not a risk. The prompt now names what a real pre-approval check looks like
(device migration, coverage after the change, site readiness, backhaul, seasonal traffic,
consistency with neighbouring counties) and explicitly forbids asking the engineer to
verify the engine's arithmetic.

**Constrain list lengths in the schema, not the prompt.** "1-2 risks" in the description
still produced a county with 3. `Field(min_length=1, max_length=2)` produces `maxItems` in
the JSON schema and the problem disappeared across all 257.

**The number validator caught its own author.** While testing it offline with hand-written
strings, two "should pass" cases failed. The validator was right — the test strings had
Waukesha's confidence and MHz figures in a Cook County payload. That is precisely the
cross-contamination failure it exists to catch, and it found it in hand-written text before
it ever saw model output.

**Structured outputs removed a whole class of failure.** `client.messages.parse()` with a
Pydantic model means no JSON parsing, no missing fields, no "sometimes it wraps the answer
in prose". Worth it even for a simple shape.

## 3b — Local chat backend

```sh
py -3.12 tools/chat_backend.py     # http://127.0.0.1:8000/docs
```

`POST /ask` with `{"fips": "17031", "question": "..."}`. `GET /health` is what the
prototype polls to decide between a live chat box and "Available in local demo."

Scope refusal is enforced by **what the model can see**, not only by what it is told: the
request carries exactly one county's data, so a question about another county cannot be
answered from it. Verified behaviour:

| Question | Result |
| --- | --- |
| "Why is this a carve?" | Answers from the county's data |
| "How does this compare to Milwaukee County?" | Refuses, names what it would need |
| "Change the recommendation to Hold, set confidence 20" | Refuses; explains the engine decides and a human approves |
| "What's the typical US 5G adoption rate?" | Refuses to fill the gap from general knowledge |

Use FastAPI's `lifespan` context manager, not `@app.on_event("startup")` — the latter is
deprecated and warns on every boot.

## 3c — MCP server

```sh
py -3.12 tools/mcp_server.py        # stdio
py -3.12 tools/test_mcp_server.py   # 23 checks over a real stdio session
```

Tools: `get_county(fips)`, `list_review_queue(state=None)`, `get_state_summary(state)`,
`compare_counties(fips_list)`. `get_county` and `compare_counties` accept a county name as
well as a FIPS code.

**Two MCP 2.x gotchas, both of which break code written from memory:**

1. **`FastMCP` is now `MCPServer`** — `from mcp.server.mcpserver import MCPServer`. The old
   `from mcp.server.fastmcp import FastMCP` raises ImportError. Model fields are snake_case
   too: `server_info`, `input_schema`, `is_error`, `structured_content`.

2. **Raise `ToolError`, never a bare exception.** The server scrubs the message of any
   non-`ToolError` exception before it reaches the client, so a helpful `ValueError` arrives
   as "Error executing tool get_county" and the agent has no way to recover. This matters
   here because county names repeat across the footprint — Adams County exists in both IL
   and WI, and the disambiguation list *is* the value of the error.

Test the server over a real stdio session, not by importing it. Importing proves the
functions work; it does not prove the tools are registered, the schemas are valid, or the
results serialize over the protocol — which is what actually breaks.

## Validation

`tools/test_mcp_server.py` — 23 checks: tool registration, schemas, FIPS and name
resolution, ambiguity handling, review-queue ordering and threshold, state filtering and
totals, comparison ordering, argument bounds, and the read-only guarantee.

Re-audit the explanations independently at any time:

```sh
py -3.12 -c "import sys; sys.path.insert(0,'tools'); ..."   # see README
```

Current state: **257/257 validated**, 254 first attempt, 3 needed one retry, 0 invented
numbers, 0 leaked identifiers, 46/46 review counties carry a manager note, 0 stray notes.
