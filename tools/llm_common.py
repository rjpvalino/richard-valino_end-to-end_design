"""Shared pieces for the Step 3 LLM layer: payload building, guardrails,
and number validation.

The division of labour matters here and is enforced by code, not by hope: the
rule engine decides, the model only explains. Nothing in this module can change
a recommendation or a confidence score - the model never sees a path to write
back into the rule output, and every number it produces is checked against the
numbers it was given.

All network, hardware, and spectrum figures are synthetic (fictional carrier).
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

from common import DATA, all_blocks, read_json

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# Sonnet 5 list price, used only to report what a run cost.
PRICE_IN_PER_MTOK = 2.00
PRICE_OUT_PER_MTOK = 10.00

SYSTEM_PROMPT = """\
You write short explanations of spectrum refarm recommendations for RF planning \
engineers at a mobile carrier.

A deterministic rule engine has already made the decision. Your job is to explain \
it clearly, not to evaluate it.

Rules you must follow:

1. Use ONLY the numbers in the data provided. Never introduce a number that is not \
in the input.
2. Quote numbers EXACTLY as given. Do not round, rescale, or restate them - if the \
input says 42.7%, write 42.7%, not 43% or "about 40%".
3. Do not change, question, or argue with the recommendation or the confidence \
score. If you think it looks wrong, explain the reasoning as given anyway.
4. If the data is insufficient to say something, say so plainly rather than \
filling the gap.
5. Write for an RF engineer: direct, technical, no marketing tone, no filler \
openers like "This county presents an interesting case".
6. Refer to layers and blocks by their real names (L2500, N2500, PCS block B5).

Never put these in your output:

- Rule IDs (HR-01, HR-05, JR-02, SEL-01, RT-01 and the like) or risk codes \
(lte_only_sites_remaining, high_lte_only_share). The engineer already sees the \
rules that fired in a separate audit panel on the same screen; repeating them \
here is noise.
- Raw field names from the JSON. Write "55 MHz on LTE", not "mhz_lte 55"; write \
"44.5% of devices are LTE-only", not "pct_lte_only_devices 44.5".
- Severity labels like "severity: medium".

The risks you list must be operational checks a planner would actually make \
before approving: device migration in this market, coverage or capacity after \
the change, site readiness, backhaul, seasonal or event traffic, consistency \
with neighbouring counties. Never ask the engineer to verify that the rule \
engine computed something correctly, or to confirm that a deferred alternative \
was deferred - that is bookkeeping, not risk, and it wastes the one thing the \
engineer will actually read.

Do not open every summary the same way. Lead with whatever matters most in this \
county - the load picture, the device base, the hardware gap, or the spectrum \
layout - rather than restating that a recommendation exists.

Vocabulary: LTE layers are L600, L700, L1900 (PCS), L2100 (AWS), L2500. NR layers \
are N600, N1900, N2100, N2500. Refarming moves spectrum from an LTE layer to the \
NR layer on the same band. L700 has no NR counterpart and is never refarmed - it \
is the LTE anchor. "Carve" means shift spectrum to NR now, "Hold" means do not, \
"Review" means a manager must decide.

When you describe what LTE still carries after the shift, use the \
`lte_layers_after` and `mhz_lte_after` values exactly as given. Do not work out \
which layers survive by subtracting - a layer the shift empties completely must \
not be listed as still carrying LTE.\
"""

# Numbers a sentence can legitimately contain that are structural rather than
# drawn from the data: counts of items, the length of the trend window, and the
# percent scale itself. Everything else must appear in the input.
STRUCTURAL_NUMBERS = {"0", "1", "2", "3", "12", "100"}

_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def normalize_number(token):
    """Normalize a numeric token so 1,234 / 1234 / 1234.0 all compare equal."""
    t = str(token).replace(",", "").strip().rstrip(".")
    if not t:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return str(int(v)) if v == int(v) else str(v)


def collect_numbers(obj, into=None):
    """Every numeric value anywhere in the payload, normalized.

    Strings are scanned too, because the rule engine's own reasons carry
    numbers ("42.7% vs 34.7%") that the model is entitled to reuse.
    """
    if into is None:
        into = set()
    if isinstance(obj, bool):
        return into
    if isinstance(obj, (int, float)):
        n = normalize_number(obj)
        if n:
            into.add(n)
    elif isinstance(obj, str):
        for tok in _NUMBER_RE.findall(obj):
            n = normalize_number(tok)
            if n:
                into.add(n)
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_numbers(v, into)
    elif isinstance(obj, list):
        for v in obj:
            collect_numbers(v, into)
    return into


def validate_numbers(text, allowed):
    """Return the numbers in `text` that do not appear in the input.

    This is the guardrail that catches the failure mode the prompt alone does
    not prevent: a model restating 42.7% as "about 43%" reads fine and is
    wrong, and an engineer who spots one such number stops trusting the rest of
    the screen.
    """
    bad = []
    for tok in _NUMBER_RE.findall(text or ""):
        n = normalize_number(tok)
        if n is None or n in STRUCTURAL_NUMBERS or n in allowed:
            continue
        bad.append(tok)
    return bad


_RULE_ID_RE = re.compile(r"\b(?:HR|JR|SEL|RT)-\d+\b")


def collect_jargon(payload):
    """Tokens from the payload that must never appear in engineer-facing prose.

    Every snake_case JSON key, plus the internal risk codes. Prompt wording
    alone got this to roughly 93% of counties; the rest need a check that can
    fail and retry, which is what makes it a guardrail rather than a hope.
    """
    bad = set()

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "_" in k:
                    bad.add(k)
                if k in ("code", "id") and isinstance(v, str):
                    bad.add(v)
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(payload)
    return bad


def validate_prose(text, jargon):
    """Return internal identifiers that leaked into the prose.

    A rule ID or a raw field name in the explanation card makes it read like a
    database dump. The rules that fired are shown in their own audit panel on
    the same screen, so repeating them here is noise, not transparency.
    """
    found = set(_RULE_ID_RE.findall(text or ""))
    for token in jargon:
        if re.search(rf"\b{re.escape(token)}\b", text or ""):
            found.add(token)
    return sorted(found)


def build_payload(fips, counties_by_fips, hardware, spectrum, recommendations):
    """Assemble exactly what the model is allowed to see for one county.

    The 12-month series is reduced to its endpoints: the model needs direction
    of travel, and sending 12 rows per county would triple the token cost
    without adding anything it can say.
    """
    county = counties_by_fips[fips]
    rec = recommendations["counties"][fips]
    months = county["network"]["months"]
    first, last = months[0], months[-1]
    spec = spectrum[fips]

    owned_blocks = [
        {"block": c["block"], "layer": c["layer"],
         "adjacent_to_nr": c["adjacent_to_nr"], "isolated": c["isolated"]}
        for c in all_blocks(spec) if c["owned"]
    ]

    # Compute the after-state here rather than leaving the model to derive it.
    # Asking it to work out which layers still carry LTE after the shift is an
    # inference the number validator cannot check - every figure can be right
    # while the layer list is wrong - so the answer is supplied instead.
    moved = {s["band"]: s["mhz"] for s in rec["shifts"]}
    bands_after = {}
    for b in spec["bands"]:
        mv = moved.get(b["band"], 0)
        bands_after[b["band"]] = (b["mhz_lte"] - mv, b["mhz_nr"] + mv)

    return {
        "county": {
            "name": county["name"],
            "state": county["state_name"],
            "fips": fips,
            "market_type": county["tier"],
            "population": county["population"],
            "density_per_sqmi": county["density_per_sqmi"],
        },
        "network_latest": {
            "month": last["month"],
            "lte_congestion_pct": last["lte_congestion_pct"],
            "nr_congestion_pct": last["nr_congestion_pct"],
            "lte_users": last["lte_users"],
            "nr_users": last["nr_users"],
            "pct_5g_devices": last["pct_5g_devices"],
            "pct_lte_only_devices": last["pct_lte_only_devices"],
        },
        "network_12_month_change": {
            "from_month": first["month"],
            "to_month": last["month"],
            "lte_congestion_pct": [first["lte_congestion_pct"],
                                   last["lte_congestion_pct"]],
            "nr_congestion_pct": [first["nr_congestion_pct"],
                                  last["nr_congestion_pct"]],
            "pct_5g_devices": [first["pct_5g_devices"], last["pct_5g_devices"]],
        },
        "hardware": {
            "total_sites": hardware[fips]["total_sites"],
            "nr_capable_sites": hardware[fips]["nr_capable_sites"],
            "lte_only_sites": hardware[fips]["lte_only_sites"],
            "pct_modernized": hardware[fips]["pct_modernized"],
        },
        "spectrum": {
            "bands": [
                dict({k: b[k] for k in
                      ("band", "mhz_owned", "mhz_lte", "mhz_nr", "lte_layer",
                       "nr_layer", "refarm_path")},
                     mhz_lte_after=bands_after[b["band"]][0],
                     mhz_nr_after=bands_after[b["band"]][1])
                for b in spec["bands"] if b["mhz_owned"] > 0
            ],
            "lte_layers": spec["lte_layers"],
            "nr_layers": spec["nr_layers"],
            # Which layers are still on air AFTER the shift - use these when
            # describing what LTE-only users are left with. Do not infer them.
            "lte_layers_after": [
                f"L{b}" for b, (l, _) in sorted(bands_after.items(), key=lambda kv: int(kv[0]))
                if l > 0
            ],
            "nr_layers_after": [
                f"N{b}" for b, (_, n) in sorted(bands_after.items(), key=lambda kv: int(kv[0]))
                if n > 0 and b != "700"
            ],
            "pcs_blocks_owned": owned_blocks,
            "totals": spec["totals"],
        },
        "rule_engine_output": {
            "recommendation": rec["recommendation"],
            "proposed_action": rec["proposed_action"],
            "confidence": rec["confidence"],
            "needs_manager_review": rec["needs_manager_review"],
            "shifts": rec["shifts"],
            "alternatives": rec["alternatives"],
            "total_mhz_shift": rec["total_mhz_shift"],
            "lte_mhz_before": rec["before"]["mhz_lte"],
            "lte_mhz_after": rec["after"]["mhz_lte"],
            "nr_mhz_before": rec["before"]["mhz_nr"],
            "nr_mhz_after": rec["after"]["mhz_nr"],
            "lte_anchor": rec["anchor"],
            "reasons": rec["reasons"],
            "rules_fired": rec["rules_fired"],
            "risk_flags": rec["risk_flags"],
        },
    }


def load_all():
    """Load the four Step 1/2 datasets the LLM layer reads."""
    counties = read_json(DATA / "counties.json")
    return (
        {c["fips"]: c for c in counties},
        read_json(DATA / "hardware.json"),
        read_json(DATA / "spectrum.json"),
        read_json(DATA / "recommendations.json"),
    )


def require_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or key.startswith("paste-your-key"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Put your key in refarm_advisor/.env:\n"
            "    ANTHROPIC_API_KEY=sk-ant-api03-...\n"
            "(.env is gitignored; see .env.example for the template.)"
        )
    return key


def usd(tokens_in, tokens_out):
    return (tokens_in / 1e6) * PRICE_IN_PER_MTOK + (tokens_out / 1e6) * PRICE_OUT_PER_MTOK
