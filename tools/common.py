"""Shared helpers for Spectrum Pivot Advisor data generation.

Census data is real; all network, device, hardware, and spectrum data is
synthetic for a fictional carrier ("GLA - Mobile").

Requires Python 3.12 (`py -3.12`). The default `python` on this machine is 3.7
and is too old for the Step 3 dependencies.
"""

import json
import random
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TMP = ROOT / ".tmp"

STATES = {"17": "IL", "55": "WI", "26": "MI"}
STATE_NAMES = {"IL": "Illinois", "WI": "Wisconsin", "MI": "Michigan"}
EXPECTED_COUNTS = {"IL": 102, "WI": 72, "MI": 83}
EXPECTED_TOTAL = 257

# Density tiers (people per square mile of land area).
URBAN_MIN = 500.0
SUBURBAN_MIN = 100.0

# --- Spectrum vocabulary. These are the ONLY valid layer names. ---
LTE_LAYERS = ["L600", "L700", "L1900", "L2100", "L2500"]
NR_LAYERS = ["N600", "N1900", "N2100", "N2500"]

# Same-band LTE -> NR refarm paths. L700 is absent on purpose: it has no NR
# layer and is never refarmed (it is the LTE anchor).
REFARM_PATHS = {"L600": "N600", "L1900": "N1900", "L2100": "N2100", "L2500": "N2500"}

BANDS = ["600", "700", "1900", "2100", "2500"]

# Full PCS band plan, in frequency order. 13 blocks x 5 MHz = 65 MHz, which
# matches the real PCS band size (A/B/C are 15 MHz blocks, each expressed as
# three 5 MHz pieces).
PCS_BLOCKS = [
    "A3", "A4", "A5", "D", "B3", "B4", "B5", "E", "F", "C3", "C4", "C5", "G",
]
PCS_BLOCK_MHZ = 5

# --- Block plans for the other refarmable bands ------------------------------
# Real FCC block designations, in frequency order, with their real paired
# widths. The widths are NOT uniform: 600 MHz is a clean 5 MHz grid, but 700 MHz
# blocks came out of 6 MHz TV channels and AWS-1 mixes 5s and 10s. Modelling
# them as uniform 5s would have been tidier and wrong, and an RF engineer would
# spot it immediately.
#
# 2500 is deliberately left unblocked - BRS/EBS channel leasing does not map to
# a clean block grid, and it is held as bulk MHz here.
BAND_PLANS = {
    "600": {                      # Band 71, broadcast incentive auction
        "label": "600 MHz",
        "blocks": [("A", 5), ("B", 5), ("C", 5), ("D", 5),
                   ("E", 5), ("F", 5), ("G", 5)],
    },
    "700": {                      # Lower/Upper 700, from 6 MHz TV channels
        "label": "700 MHz",
        "blocks": [("Lower A", 6), ("Lower B", 6), ("Lower C", 6),
                   ("Upper C", 11), ("Upper D", 5)],
    },
    "1900": {                     # PCS
        "label": "1900 MHz (PCS)",
        "blocks": [(b, PCS_BLOCK_MHZ) for b in PCS_BLOCKS],
    },
    "2100": {                     # AWS-1
        "label": "2100 MHz (AWS)",
        "blocks": [("A", 10), ("B", 10), ("C", 5),
                   ("D", 5), ("E", 5), ("F", 10)],
    },
}
BLOCKED_BANDS = list(BAND_PLANS)          # bands modelled at block level
BULK_BANDS = ["2500"]                     # held as bulk MHz, no block grid


def blocks_for(band):
    """Block names in frequency order for a banded band."""
    return [b for b, _ in BAND_PLANS[band]["blocks"]]


def band_blocks(spec, band):
    """Blocks for one band of a county's spectrum record.

    Blocks are stored only on their band, never duplicated into a flat list -
    two copies of the same fact are how a block strip ends up disagreeing with
    the band totals it is supposed to describe.
    """
    rec = next((b for b in spec["bands"] if b["band"] == band), None)
    return rec["blocks"] if rec else []


def all_blocks(spec):
    """Every block across every banded band, in band then frequency order."""
    return [c for b in spec["bands"] for c in b["blocks"]]


def block_mhz(band):
    """{block name: paired MHz} for a banded band."""
    return dict(BAND_PLANS[band]["blocks"])

USER_AGENT = "Mozilla/5.0 (RefarmAdvisor portfolio project; contact via GitHub)"


def rng_for(fips):
    """Per-county RNG seeded from FIPS.

    Each county is independent, so output is reproducible and editing one
    county never shifts another.
    """
    return random.Random(int(fips))


def tier_for(density):
    if density >= URBAN_MIN:
        return "urban"
    if density >= SUBURBAN_MIN:
        return "suburban"
    return "rural"


def download(url, dest, refresh=False):
    """Fetch `url` to `dest`, reusing the cached copy unless `refresh`.

    Guards against the Census API failure mode where a missing key returns
    HTTP 200 with an HTML error page instead of data.
    """
    dest = Path(dest)
    if dest.exists() and not refresh:
        print(f"  cached  {dest.name} ({dest.stat().st_size:,} bytes)")
        return dest.read_bytes()

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetch   {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read()

    head = body[:200].lstrip().lower()
    if head.startswith(b"<html") or b"<!doctype html" in head:
        raise RuntimeError(
            f"{url} returned an HTML page, not data. The Census API now "
            f"requires a key and fails this way. First bytes: {body[:120]!r}"
        )
    if not body:
        raise RuntimeError(f"{url} returned an empty body.")

    dest.write_bytes(body)
    print(f"  saved   {dest.name} ({len(body):,} bytes)")
    return body


def write_json(path, obj, compact=False):
    """Write JSON with stable key order.

    `compact` drops indentation - worth it for geometry, where pretty-printing
    more than triples the file the browser has to download.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        if compact:
            json.dump(obj, fh, separators=(",", ":"), sort_keys=False)
        else:
            json.dump(obj, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(f"  wrote   {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


def read_json(path):
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def load_counties():
    """Load counties.json, failing loudly if Step 1a has not run."""
    path = DATA / "counties.json"
    if not path.exists():
        raise SystemExit(
            "data/counties.json missing. Run tools/gen_county_data.py first "
            "(or just run tools/run_step1.py)."
        )
    return read_json(path)
