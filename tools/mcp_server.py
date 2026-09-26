"""Step 3c - MCP server exposing the Spectrum Pivot Advisor data as read-only tools.

Lets an agent (Claude Desktop, or any MCP client) query exactly the same data
the engineer sees on screen, so a conversation about the plan and the plan
itself cannot drift apart.

Read-only by design: there is no tool here that writes, approves, rejects, or
re-scores anything. An agent can interrogate the recommendations; a human still
makes the decision.

    py -3.12 tools/mcp_server.py

Built on mcp 2.x, where FastMCP was renamed MCPServer.
Synthetic data, fictional carrier.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from common import DATA, all_blocks, band_blocks, read_json

SYNTHETIC_NOTE = (
    "Census county identity, population, land area and boundaries are real. "
    "All network, device, hardware, and spectrum data is synthetic for a "
    "fictional carrier, GLA - Mobile."
)

mcp = MCPServer(
    name="spectrum-pivot-advisor",
    title="Spectrum Pivot Advisor",
    version="1.0.0",
    instructions=(
        "Query LTE-to-5G spectrum refarm recommendations for 257 counties in "
        "Illinois, Wisconsin, and Michigan.\n\n"
        "The recommendations come from a deterministic rule engine, not from a "
        "model. These tools are read-only: you can inspect recommendations but "
        "not change them, and approval is a human decision made in the tool "
        "itself. Report the confidence score and the rules that fired when "
        "explaining any recommendation, and never restate a number differently "
        "from how these tools return it.\n\n" + SYNTHETIC_NOTE
    ),
)


def _load():
    counties = read_json(DATA / "counties.json")
    return {
        "counties": {c["fips"]: c for c in counties},
        "hardware": read_json(DATA / "hardware.json"),
        "spectrum": read_json(DATA / "spectrum.json"),
        "recs": read_json(DATA / "recommendations.json"),
        "explanations": (
            read_json(DATA / "explanations.json")["counties"]
            if (DATA / "explanations.json").exists() else {}
        ),
    }


D = _load()


def _resolve(fips):
    """Accept a FIPS code or a county name, so an agent can pass either.

    Failures raise ToolError, not ValueError: the server scrubs the message of
    any other exception before it reaches the client, which would leave an
    agent with "Error executing tool" and no way to recover. County names
    repeat across these three states - 'Adams' exists in all of them - so the
    disambiguation list is the whole value of the error.
    """
    fips = str(fips).strip()
    if fips in D["recs"]["counties"]:
        return fips
    needle = fips.lower().replace(" county", "").strip()
    matches = [
        f for f, r in D["recs"]["counties"].items()
        if r["name"].lower().replace(" county", "") == needle
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(
            f"{D['recs']['counties'][f]['name']}, {D['recs']['counties'][f]['state']} "
            f"({f})" for f in sorted(matches)
        )
        raise ToolError(
            f"'{fips}' matches more than one county. Call get_county again "
            f"with the FIPS code for the one you want: {names}"
        )
    raise ToolError(
        f"No county named '{fips}' in this dataset. Pass a 5-digit FIPS code, "
        f"or a county name from Illinois, Wisconsin, or Michigan - those are "
        f"the only three states covered."
    )


@mcp.tool(
    description="Full detail for one county: recommendation, confidence, the "
                "rules that fired, risk flags, network and device metrics, "
                "hardware readiness, spectrum by band, and the PCS block plan. "
                "Accepts a 5-digit FIPS code or a county name."
)
def get_county(fips: str) -> dict:
    f = _resolve(fips)
    county, rec, spec = D["counties"][f], D["recs"]["counties"][f], D["spectrum"][f]
    expl = D["explanations"].get(f)
    return {
        "fips": f,
        "name": county["name"],
        "state": county["state_name"],
        "market_type": county["tier"],
        "population": county["population"],
        "density_per_sqmi": county["density_per_sqmi"],
        "recommendation": rec["recommendation"],
        "proposed_action": rec["proposed_action"],
        "confidence": rec["confidence"],
        "needs_manager_review": rec["needs_manager_review"],
        "shifts": rec["shifts"],
        "alternatives": rec["alternatives"],
        "total_mhz_shift": rec["total_mhz_shift"],
        "lte_mhz_before_after": [rec["before"]["mhz_lte"], rec["after"]["mhz_lte"]],
        "nr_mhz_before_after": [rec["before"]["mhz_nr"], rec["after"]["mhz_nr"]],
        "lte_anchor": rec["anchor"],
        "reasons": rec["reasons"],
        "rules_fired": rec["rules_fired"],
        "risk_flags": rec["risk_flags"],
        "confidence_factors": rec["confidence_factors"],
        "network_latest": county["network"]["latest"],
        "network_latest_month": county["network"]["latest_month"],
        "hardware": D["hardware"][f],
        "spectrum_bands": spec["bands"],
        "pcs_blocks": band_blocks(spec, "1900"),
        "blocks_by_band": {b["band"]: b["blocks"] for b in spec["bands"] if b["blocks"]},
        "lte_layers": spec["lte_layers"],
        "nr_layers": spec["nr_layers"],
        "ai_explanation": expl,
        "data_note": SYNTHETIC_NOTE,
    }


@mcp.tool(
    description="Counties routed to Manager Review because rule-engine "
                "confidence fell below 60. Returns why each one is uncertain, "
                "sorted by confidence ascending so the least certain come "
                "first. Optionally filter by state (IL, WI, or MI)."
)
def list_review_queue(state: str | None = None) -> dict:
    rows = []
    for f in D["recs"]["review_queue"]:
        rec = D["recs"]["counties"][f]
        if state and rec["state"].upper() != state.strip().upper():
            continue
        expl = D["explanations"].get(f) or {}
        rows.append({
            "fips": f,
            "name": rec["name"],
            "state": rec["state"],
            "market_type": rec["tier"],
            "confidence": rec["confidence"],
            "proposed_action": rec["proposed_action"],
            "shifts": rec["shifts"],
            "reasons": rec["reasons"],
            "risk_flags": [r["label"] for r in rec["risk_flags"]],
            "confidence_penalties": [
                {"factor": c["label"], "points": c["delta"]}
                for c in rec["confidence_factors"]
                if isinstance(c.get("delta"), (int, float)) and c["delta"] < 0
            ],
            "manager_note": expl.get("manager_note"),
        })
    return {
        "count": len(rows),
        "threshold": D["recs"]["thresholds"]["review_below_confidence"],
        "counties": rows,
        "data_note": SYNTHETIC_NOTE,
    }


@mcp.tool(
    description="Rollup for one state (IL, WI, or MI): counts by "
                "recommendation, total MHz to be refarmed, review queue size, "
                "breakdown by market type, and the largest single refarm "
                "opportunities."
)
def get_state_summary(state: str) -> dict:
    code = state.strip().upper()
    alias = {"ILLINOIS": "IL", "WISCONSIN": "WI", "MICHIGAN": "MI"}
    code = alias.get(code, code)
    if code not in ("IL", "WI", "MI"):
        raise ToolError(
            f"'{state}' is not in this dataset. Only Illinois (IL), Wisconsin "
            f"(WI), and Michigan (MI) are covered."
        )

    rows = [r for r in D["recs"]["counties"].values() if r["state"] == code]
    by_rec, by_tier = {}, {}
    for r in rows:
        by_rec[r["recommendation"]] = by_rec.get(r["recommendation"], 0) + 1
        t = by_tier.setdefault(r["tier"], {"counties": 0, "mhz": 0})
        t["counties"] += 1
        t["mhz"] += r["total_mhz_shift"]

    carves = sorted(
        (r for r in rows if r["total_mhz_shift"] > 0),
        key=lambda r: -r["total_mhz_shift"],
    )[:5]
    return {
        "state": code,
        "counties": len(rows),
        "by_recommendation": dict(sorted(by_rec.items())),
        "by_market_type": by_tier,
        "total_mhz_refarmed": sum(r["total_mhz_shift"] for r in rows),
        "review_queue_size": sum(1 for r in rows if r["needs_manager_review"]),
        "mean_confidence": round(
            sum(r["confidence"] for r in rows) / max(1, len(rows)), 1
        ),
        "largest_opportunities": [
            {
                "fips": r["fips"], "name": r["name"],
                "mhz": r["total_mhz_shift"],
                "shift": (f"{r['shifts'][0]['from']} -> {r['shifts'][0]['to']}"
                          if r["shifts"] else None),
                "confidence": r["confidence"],
                "recommendation": r["recommendation"],
            }
            for r in carves
        ],
        "data_note": SYNTHETIC_NOTE,
    }


@mcp.tool(
    description="Compare 2-10 counties side by side on recommendation, "
                "confidence, the spectrum move, device mix, hardware "
                "readiness, and congestion. Accepts FIPS codes or county names."
)
def compare_counties(fips_list: list[str]) -> dict:
    if not 2 <= len(fips_list) <= 10:
        raise ToolError(
            f"compare_counties needs between 2 and 10 counties, got "
            f"{len(fips_list)}. For a single county use get_county instead."
        )
    resolved = [_resolve(x) for x in fips_list]
    rows = []
    for f in resolved:
        rec, county, hw = D["recs"]["counties"][f], D["counties"][f], D["hardware"][f]
        latest = county["network"]["latest"]
        rows.append({
            "fips": f,
            "name": rec["name"],
            "state": rec["state"],
            "market_type": rec["tier"],
            "recommendation": rec["recommendation"],
            "confidence": rec["confidence"],
            "mhz_to_shift": rec["total_mhz_shift"],
            "shift": (f"{rec['shifts'][0]['from']} -> {rec['shifts'][0]['to']}"
                      if rec["shifts"] else None),
            "lte_congestion_pct": latest["lte_congestion_pct"],
            "nr_congestion_pct": latest["nr_congestion_pct"],
            "pct_5g_devices": latest["pct_5g_devices"],
            "pct_lte_only_devices": latest["pct_lte_only_devices"],
            "pct_sites_nr_capable": hw["pct_modernized"],
            "lte_anchor": rec["anchor"]["layer"] if rec["anchor"] else None,
            "top_reason": rec["reasons"][0] if rec["reasons"] else None,
        })
    return {
        "counties": rows,
        "total_mhz_to_shift": sum(r["mhz_to_shift"] for r in rows),
        "recommendations": sorted({r["recommendation"] for r in rows}),
        "data_note": SYNTHETIC_NOTE,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
