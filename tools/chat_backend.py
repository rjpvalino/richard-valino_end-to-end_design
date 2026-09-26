"""Step 3b - "Ask about this county" chat backend. LOCAL DEMO ONLY.

    py -3.12 tools/chat_backend.py
    # then open http://127.0.0.1:8000/docs

The Step 4 prototype calls this when it is running and shows "Available in
local demo." when it is not, so the static GitHub Pages build still works.

Same guardrails as 3a, plus one more: the model is given exactly one county's
data and must refuse anything outside it. That refusal is enforced by what the
model can see, not only by what it is told - a question about a different
county cannot be answered from a payload that does not contain it.
"""

import json
import sys
from contextlib import asynccontextmanager

import anthropic
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from llm_common import (
    MODEL, SYSTEM_PROMPT, build_payload, collect_jargon, collect_numbers,
    load_all, require_api_key, validate_numbers, validate_prose,
)

CHAT_SYSTEM = SYSTEM_PROMPT + """

You are now answering an engineer's question about ONE county. You have that \
county's data and nothing else.

Scope rules:

- Answer only from the county data provided in the user message.
- If the question is about a different county, a different state, a regional or \
national total, or anything not in this county's data, say you can only answer \
from this county's data and name what you would need. Do not guess, and do not \
reason from general knowledge about mobile networks to fill a gap in the data.
- If the question asks you to change, override, or re-score the recommendation, \
explain that the recommendation comes from the rule engine and that a human \
approves or rejects it - then explain the reasoning as it stands.
- Answer in at most 4 sentences. An engineer is reading this next to the data, \
not instead of it.\
"""

STATE = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    require_api_key()
    counties, hardware, spectrum, recs = load_all()
    STATE.update(
        counties=counties, hardware=hardware, spectrum=spectrum, recs=recs,
        client=anthropic.Anthropic(max_retries=3),
    )
    print(f"Loaded {len(counties)} counties. Model: {MODEL}")
    yield
    STATE.clear()


app = FastAPI(
    title="Spectrum Pivot Advisor - county chat (local demo)",
    description="Synthetic data, fictional carrier. The rule engine decides; "
                "this endpoint only explains.",
    version="1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local demo only - the prototype is opened from file://
    allow_methods=["*"],
    allow_headers=["*"],
)

class Question(BaseModel):
    fips: str = Field(description="5-digit county FIPS code")
    question: str = Field(min_length=1, max_length=1000)


class Answer(BaseModel):
    fips: str
    county: str
    answer: str
    validated: bool
    unverified_numbers: list[str]
    model: str
    note: str = ("AI-generated from this county's data only. Verify before "
                 "approving. Synthetic data, fictional carrier.")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "counties": len(STATE.get("counties", {})),
        "model": MODEL,
        "note": "Local demo. Synthetic data, fictional carrier.",
    }


@app.post("/ask", response_model=Answer)
def ask(q: Question):
    recs = STATE["recs"]
    if q.fips not in recs["counties"]:
        raise HTTPException(404, f"Unknown county FIPS: {q.fips}")

    payload = build_payload(
        q.fips, STATE["counties"], STATE["hardware"], STATE["spectrum"], recs
    )
    prompt = (
        f"County data:\n\n```json\n{json.dumps(payload, indent=2)}\n```\n\n"
        f"Engineer's question: {q.question}"
    )

    resp = STATE["client"].messages.create(
        model=MODEL,
        max_tokens=800,
        system=[{
            "type": "text",
            "text": CHAT_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")

    bad = validate_numbers(text, collect_numbers(payload))
    leaks = validate_prose(text, collect_jargon(payload))

    return Answer(
        fips=q.fips,
        county=f"{recs['counties'][q.fips]['name']}, "
               f"{recs['counties'][q.fips]['state']}",
        answer=text,
        validated=not bad and not leaks,
        unverified_numbers=bad + leaks,
        model=MODEL,
    )


if __name__ == "__main__":
    host = "127.0.0.1"
    port = 8000
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    print(f"Spectrum Pivot Advisor chat backend on http://{host}:{port}  (local demo)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
