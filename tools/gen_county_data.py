"""Step 1a - county base data. This is the only REAL data in the project.

Sources (both keyless static Census files; the Census API now requires a key):
  - 2024 Gazetteer            -> land area, internal point lat/long
  - Vintage 2024 popest CSV   -> population

Output: data/counties.json
"""

import csv
import io
import sys
import zipfile

from common import (
    DATA, EXPECTED_COUNTS, EXPECTED_TOTAL, STATE_NAMES, STATES, TMP,
    download, tier_for, write_json,
)

GAZ_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_counties_national.zip"
)
POP_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/"
    "2020-2024/counties/totals/co-est2024-alldata.csv"
)
POP_FIELD = "POPESTIMATE2024"


def load_gazetteer(refresh=False):
    """Land area and centroid, keyed by 5-digit FIPS.

    The Gazetteer file is tab-delimited inside the zip, latin-1 encoded, and
    its header row has padding spaces around the field names.
    """
    raw = download(GAZ_URL, TMP / "gaz_counties_2024.zip", refresh)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        text = zf.read(zf.namelist()[0]).decode("latin-1")

    out = {}
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for row in reader:
        row = {k.strip(): (v.strip() if v else v) for k, v in row.items()}
        if row["USPS"] not in STATE_NAMES:
            continue
        out[row["GEOID"]] = {
            "state": row["USPS"],
            "name": row["NAME"],
            "land_area_sqmi": float(row["ALAND_SQMI"]),
            "lat": float(row["INTPTLAT"]),
            "lon": float(row["INTPTLONG"]),
        }
    return out


def load_population(refresh=False):
    """Population by 5-digit FIPS. SUMLEV 050 is the county level."""
    raw = download(POP_URL, TMP / "co-est2024-alldata.csv", refresh)
    text = raw.decode("latin-1")

    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row["SUMLEV"] != "050" or row["STATE"] not in STATES:
            continue
        out[row["STATE"] + row["COUNTY"]] = int(row[POP_FIELD])
    return out


def build(refresh=False):
    print("Step 1a - county base data (REAL Census)")
    gaz = load_gazetteer(refresh)
    pop = load_population(refresh)

    only_gaz = sorted(set(gaz) - set(pop))
    only_pop = sorted(set(pop) - set(gaz))
    if only_gaz or only_pop:
        raise RuntimeError(
            f"FIPS mismatch between Census files. "
            f"Gazetteer-only: {only_gaz}  Popest-only: {only_pop}"
        )

    counties = []
    for fips in sorted(gaz):
        g = gaz[fips]
        population = pop[fips]
        if g["land_area_sqmi"] <= 0:
            raise RuntimeError(f"{fips} {g['name']} has non-positive land area")
        density = population / g["land_area_sqmi"]
        counties.append({
            "fips": fips,
            "name": g["name"],
            "state": g["state"],
            "state_name": STATE_NAMES[g["state"]],
            "population": population,
            "land_area_sqmi": round(g["land_area_sqmi"], 3),
            "density_per_sqmi": round(density, 1),
            "tier": tier_for(density),
            "lat": g["lat"],
            "lon": g["lon"],
        })

    if len(counties) != EXPECTED_TOTAL:
        raise RuntimeError(f"expected {EXPECTED_TOTAL} counties, got {len(counties)}")
    per_state = {}
    for c in counties:
        per_state[c["state"]] = per_state.get(c["state"], 0) + 1
    if per_state != EXPECTED_COUNTS:
        raise RuntimeError(f"state counts {per_state} != {EXPECTED_COUNTS}")

    write_json(DATA / "counties.json", counties)

    tiers = {}
    for c in counties:
        tiers[c["tier"]] = tiers.get(c["tier"], 0) + 1
    print(f"  {len(counties)} counties  {per_state}")
    print(f"  tiers: urban={tiers.get('urban', 0)} "
          f"suburban={tiers.get('suburban', 0)} rural={tiers.get('rural', 0)}")
    return counties


if __name__ == "__main__":
    build(refresh="--refresh" in sys.argv)
