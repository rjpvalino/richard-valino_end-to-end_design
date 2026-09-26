"""Exercise the MCP server the way a client does: over a real stdio session.

    py -3.12 tools/test_mcp_server.py

Importing the module and calling the functions directly would not prove the
tools are registered, that their schemas are valid, or that the results
serialize over the protocol - which is what actually breaks.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


def payload(result):
    """Pull the structured result out of a CallToolResult."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise AssertionError("no usable content in tool result")


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=[str(HERE / "mcp_server.py")]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"Connected to: {info.server_info.name} "
                  f"v{info.server_info.version}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"\n=== Tools exposed ===\n  {names}\n")
            check("all four tools are registered",
                  names == ["compare_counties", "get_county",
                            "get_state_summary", "list_review_queue"],
                  str(names))
            check("every tool has a description and input schema",
                  all(t.description and t.input_schema for t in tools.tools))

            # --- get_county by FIPS ---
            r = payload(await session.call_tool("get_county", {"fips": "17031"}))
            print(f"get_county(17031) -> {r['name']}, {r['state']}: "
                  f"{r['recommendation']} @ {r['confidence']}")
            check("get_county returns Cook County by FIPS",
                  r["name"] == "Cook County" and r["state"] == "Illinois")
            check("get_county includes the rules-fired audit trail",
                  len(r["rules_fired"]) > 0)
            check("get_county includes the 13-block PCS strip",
                  len(r["pcs_blocks"]) == 13 and len(r["blocks_by_band"]) == 4)
            check("get_county carries the AI explanation",
                  r["ai_explanation"] and r["ai_explanation"]["summary"])
            check("get_county states the data is synthetic",
                  "synthetic" in r["data_note"].lower())

            # --- get_county by name ---
            r2 = payload(await session.call_tool("get_county",
                                                 {"fips": "Milwaukee"}))
            print(f"get_county('Milwaukee') -> {r2['name']}, {r2['state']}")
            check("get_county resolves a bare county name",
                  r2["fips"] == "55079")

            # --- ambiguous name is an error, not a wrong answer ---
            amb = await session.call_tool("get_county", {"fips": "Adams"})
            print(f"get_county('Adams') -> is_error={amb.is_error}")
            check("an ambiguous county name errors instead of guessing",
                  amb.is_error)

            # --- review queue ---
            q = payload(await session.call_tool("list_review_queue", {}))
            print(f"\nlist_review_queue() -> {q['count']} counties, "
                  f"threshold {q['threshold']}")
            check("review queue is non-empty", q["count"] > 0)
            confs = [c["confidence"] for c in q["counties"]]
            check("review queue is sorted least-confident first",
                  confs == sorted(confs), str(confs[:6]))
            check("every queued county is below the threshold",
                  all(c < q["threshold"] for c in confs))
            check("queued counties carry a manager note",
                  all(c["manager_note"] for c in q["counties"]))
            check("queued counties explain what cost them confidence",
                  all(c["confidence_penalties"] for c in q["counties"]))

            mi = payload(await session.call_tool("list_review_queue",
                                                 {"state": "MI"}))
            print(f"list_review_queue('MI') -> {mi['count']} counties")
            check("review queue filters by state",
                  0 < mi["count"] < q["count"]
                  and all(c["state"] == "MI" for c in mi["counties"]))

            # --- state summary ---
            s = payload(await session.call_tool("get_state_summary",
                                                {"state": "WI"}))
            print(f"\nget_state_summary('WI') -> {s['counties']} counties, "
                  f"{s['by_recommendation']}, {s['total_mhz_refarmed']} MHz")
            check("Wisconsin has 72 counties", s["counties"] == 72)
            check("state summary totals MHz and lists opportunities",
                  s["total_mhz_refarmed"] > 0
                  and len(s["largest_opportunities"]) == 5)
            s2 = payload(await session.call_tool("get_state_summary",
                                                 {"state": "Michigan"}))
            check("state summary accepts a full state name",
                  s2["state"] == "MI" and s2["counties"] == 83)
            bad = await session.call_tool("get_state_summary", {"state": "CA"})
            check("an out-of-footprint state errors", bad.is_error)

            # --- compare ---
            c = payload(await session.call_tool(
                "compare_counties",
                {"fips_list": ["17031", "55079", "26083"]}))
            print(f"\ncompare_counties(3) -> "
                  f"{[x['name'] for x in c['counties']]}")
            check("compare returns one row per county",
                  len(c["counties"]) == 3)
            check("compare preserves the requested order",
                  [x["fips"] for x in c["counties"]]
                  == ["17031", "55079", "26083"])
            one = await session.call_tool("compare_counties",
                                          {"fips_list": ["17031"]})
            check("comparing a single county errors", one.is_error)

            # --- read-only guarantee ---
            check("no tool can write, approve, reject, or re-score",
                  not any(
                      w in t.name.lower()
                      for t in tools.tools
                      for w in ("set", "update", "approve", "reject",
                                "write", "delete", "score")
                  ))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("MCP server: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
