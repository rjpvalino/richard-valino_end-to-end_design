"""Step 3a - pre-generate one explanation per county with the Claude API.

Pre-generating means the prototype works as a static page on GitHub Pages with
no backend: the explanations ship as a JSON file alongside the data.

    py -3.12 tools/gen_explanations.py --limit 5     # test run, 5 counties
    py -3.12 tools/gen_explanations.py               # all remaining counties
    py -3.12 tools/gen_explanations.py --fips 17031  # one specific county
    py -3.12 tools/gen_explanations.py --force       # regenerate existing ones

Runs are resumable: counties already present in data/explanations.json are
skipped unless --force is given, so an interrupted run costs nothing to repeat.

Output: data/explanations.json
"""

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
from pydantic import BaseModel, Field

from common import DATA, read_json, write_json
from llm_common import (
    MODEL, SYSTEM_PROMPT, build_payload, collect_jargon, collect_numbers,
    load_all, require_api_key, usd, validate_numbers, validate_prose,
)

MAX_WORKERS = 6
MAX_ATTEMPTS = 3


class Explanation(BaseModel):
    """The shape the model must return. Enforced by structured outputs."""

    summary: str = Field(
        description="2-3 sentences explaining the recommendation to an RF "
                    "engineer, using only numbers from the input."
    )
    risks: list[str] = Field(
        min_length=1,
        max_length=2,
        description="Exactly 1 or 2 operational checks to make before "
                    "approving. Each one a short sentence. Never more than 2 - "
                    "a longer list is a list nobody reads."
    )
    lte_only_impact: str = Field(
        description="One sentence on what this means for LTE-only users in "
                    "this county."
    )
    manager_note: str | None = Field(
        default=None,
        description="One sentence addressed to the approving manager. Provide "
                    "this ONLY when needs_manager_review is true; otherwise null."
    )


_lock = threading.Lock()
_usage = {"in": 0, "out": 0, "calls": 0}


def _record_usage(resp):
    with _lock:
        _usage["in"] += resp.usage.input_tokens
        _usage["out"] += resp.usage.output_tokens
        _usage["calls"] += 1


def explain_one(client, payload, allowed, jargon):
    """One county, with both guardrails.

    A failure of either check - an invented number, or an internal identifier
    leaking into the prose - triggers a retry that names exactly what was
    wrong. After MAX_ATTEMPTS the output is stored but flagged, never silently
    accepted.
    """
    needs_note = payload["rule_engine_output"]["needs_manager_review"]
    ask = (
        "Explain this refarm recommendation.\n\n"
        + ("A manager_note IS required - this county is routed to Manager "
           "Review.\n\n" if needs_note else
           "Set manager_note to null - this county is not routed to review.\n\n")
        + "```json\n" + json.dumps(payload, indent=2) + "\n```"
    )
    messages = [{"role": "user", "content": ask}]
    last_bad, last_leaks = [], []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # 2000 was not quite enough: one county in 257 ran long and the JSON
        # came back truncated mid-string, which surfaces as a Pydantic
        # ValidationError rather than anything the number check can see.
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=3000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
            output_format=Explanation,
        )
        _record_usage(resp)
        out = resp.parsed_output

        checked = " ".join(
            [out.summary, out.lte_only_impact, out.manager_note or ""]
            + list(out.risks)
        )
        bad = validate_numbers(checked, allowed)
        leaks = validate_prose(checked, jargon)
        if not bad and not leaks:
            return out, attempt, [], []

        last_bad, last_leaks = bad, leaks
        if attempt == MAX_ATTEMPTS:
            break

        problems = []
        if bad:
            problems.append(
                f"These numbers do not appear in the input data: "
                f"{', '.join(sorted(set(bad)))}. Use only numbers present in "
                f"the JSON above, quoted exactly as given. Do not round."
            )
        if leaks:
            problems.append(
                f"These are internal identifiers and must not appear in "
                f"engineer-facing prose: {', '.join(leaks)}. Say the same "
                f"thing in plain English - for example write \"5G device "
                f"share rose from 59.5% to 69.1%\" rather than naming the "
                f"field, and describe what a rule did rather than citing its "
                f"ID."
            )
        messages = messages + [
            {"role": "assistant", "content": out.model_dump_json()},
            {"role": "user", "content": " ".join(problems)
                + " Rewrite accordingly, keeping the same meaning and the "
                  "same recommendation."},
        ]

    return out, MAX_ATTEMPTS, last_bad, last_leaks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only process N counties")
    ap.add_argument("--fips", action="append", help="specific county FIPS")
    ap.add_argument("--force", action="store_true",
                    help="regenerate counties that already have explanations")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = ap.parse_args()

    require_api_key()
    counties_by_fips, hardware, spectrum, recs = load_all()

    out_path = DATA / "explanations.json"
    existing = read_json(out_path) if out_path.exists() else {}
    store = existing.get("counties", {}) if isinstance(existing, dict) else {}

    targets = args.fips or sorted(recs["counties"])
    unknown = [f for f in targets if f not in recs["counties"]]
    if unknown:
        raise SystemExit(f"unknown FIPS: {unknown}")
    if not args.force:
        targets = [f for f in targets if f not in store]
    if args.limit:
        targets = targets[:args.limit]

    if not targets:
        print("Nothing to do - every requested county already has an "
              "explanation. Use --force to regenerate.")
        return 0

    print(f"Step 3a - generating explanations with {MODEL}")
    print(f"  {len(targets)} counties, {args.workers} workers, "
          f"{len(store)} already stored")

    client = anthropic.Anthropic(max_retries=4)
    failures, flagged = [], []

    def work(fips):
        payload = build_payload(fips, counties_by_fips, hardware, spectrum, recs)
        allowed = collect_numbers(payload)
        jargon = collect_jargon(payload)
        # A truncated or malformed response raises before any of our own checks
        # run, so it needs its own retry - one bad draw should not cost a county
        # its explanation.
        last = None
        for _ in range(2):
            try:
                out, attempts, bad, leaks = explain_one(
                    client, payload, allowed, jargon)
                return fips, payload, out, attempts, bad, leaks
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise last

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, f): f for f in targets}
        for fut in as_completed(futures):
            fips = futures[fut]
            try:
                fips, payload, out, attempts, bad, leaks = fut.result()
            except Exception as exc:  # noqa: BLE001 - report, don't abort the run
                failures.append((fips, f"{type(exc).__name__}: {exc}"))
                print(f"  FAIL  {fips}  {type(exc).__name__}: {exc}")
                continue

            rec = recs["counties"][fips]
            store[fips] = {
                "fips": fips,
                "name": rec["name"],
                "state": rec["state"],
                "model": MODEL,
                "summary": out.summary,
                "risks": out.risks,
                "lte_only_impact": out.lte_only_impact,
                "manager_note": out.manager_note,
                "attempts": attempts,
                "validated": not bad and not leaks,
                "unverified_numbers": bad,
                "leaked_identifiers": leaks,
            }
            if bad or leaks:
                flagged.append((fips, bad + leaks))
            done += 1
            status = "ok " if not (bad or leaks) else "FLAG"
            retry = "" if attempts == 1 else f" (retries: {attempts - 1})"
            print(f"  [{done}/{len(targets)}] {status} {fips} "
                  f"{rec['name']}, {rec['state']}{retry}")

    write_json(out_path, {
        "model": MODEL,
        "note": "AI-generated explanations of a rule-based recommendation. The "
                "model never changes the recommendation or the confidence "
                "score. Every number is validated against the source data.",
        "counties": dict(sorted(store.items())),
    })

    cost = usd(_usage["in"], _usage["out"])
    print(f"\n  {_usage['calls']} API calls, "
          f"{_usage['in']:,} in / {_usage['out']:,} out tokens")
    print(f"  cost this run: ${cost:.2f}  "
          f"(projected for all 257: ${cost / max(1, done) * 257:.2f})")
    print(f"  stored: {len(store)}/257 counties")

    if flagged:
        print(f"\n  {len(flagged)} county(s) still contain unverified numbers:")
        for fips, bad in flagged:
            print(f"    {fips}: {', '.join(sorted(set(bad)))}")
    if failures:
        print(f"\n  {len(failures)} county(s) failed outright:")
        for fips, err in failures:
            print(f"    {fips}: {err}")
    if not flagged and not failures:
        print("\n  All explanations validated: every number traces to the input.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
