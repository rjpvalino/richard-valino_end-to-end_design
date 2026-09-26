"""County map geometry for the Step 4 Leaflet prototype. REAL Census boundaries.

TIGERweb serves GeoJSON directly, and its `maxAllowableOffset` parameter does
the generalization server-side: all 257 counties come back at 92 KB instead of
14 MB, which is what makes a single static HTML file viable on GitHub Pages. No
client-side simplification needed.

Output: data/counties.geojson
"""

import json
import sys
import urllib.parse

from common import DATA, EXPECTED_TOTAL, TMP, download, load_counties, write_json

TIGERWEB = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "State_County/MapServer/1/query"
)
TIGERWEB_STATES = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "State_County/MapServer/0/query"
)
# States are background context on the map, so they can be much coarser than
# the counties drawn on top of them.
STATE_OFFSET = "0.03"
# Territories sit thousands of miles from the footprint and would wreck any
# fit-to-bounds; the 50 states plus DC are the map.
SKIP_STATES = {"PR", "VI", "GU", "AS", "MP"}
# Degrees. Larger = coarser and smaller. 0.005 deg is roughly 550 m.
MAX_ALLOWABLE_OFFSET = "0.005"
COORD_PRECISION = 4


def _round_coords(obj):
    if isinstance(obj, float):
        return round(obj, COORD_PRECISION)
    if isinstance(obj, list):
        return [_round_coords(x) for x in obj]
    return obj


def build(refresh=False):
    print("Step 1 (map) - county geometry (REAL Census boundaries)")
    params = {
        "where": "STATE IN ('17','55','26')",
        "outFields": "GEOID,NAME,STATE",
        "returnGeometry": "true",
        "f": "geojson",
        "outSR": "4326",
        "maxAllowableOffset": MAX_ALLOWABLE_OFFSET,
    }
    url = f"{TIGERWEB}?{urllib.parse.urlencode(params)}"
    raw = download(url, TMP / "tigerweb_counties.geojson", refresh)
    fc = json.loads(raw)

    if fc.get("exceededTransferLimit"):
        raise RuntimeError(
            "TIGERweb truncated the response; paginate with resultOffset."
        )

    expected = {c["fips"] for c in load_counties()}
    features = []
    for feat in fc["features"]:
        geoid = feat["properties"]["GEOID"]
        if geoid not in expected:
            continue
        features.append({
            "type": "Feature",
            "properties": {"fips": geoid},
            "geometry": {
                "type": feat["geometry"]["type"],
                "coordinates": _round_coords(feat["geometry"]["coordinates"]),
            },
        })

    got = {f["properties"]["fips"] for f in features}
    if got != expected:
        raise RuntimeError(
            f"geometry FIPS mismatch: missing {sorted(expected - got)}, "
            f"extra {sorted(got - expected)}"
        )
    if len(features) != EXPECTED_TOTAL:
        raise RuntimeError(f"expected {EXPECTED_TOTAL} features, got {len(features)}")

    features.sort(key=lambda f: f["properties"]["fips"])
    write_json(DATA / "counties.geojson", {
        "type": "FeatureCollection",
        "features": features,
    }, compact=True)
    print(f"  {len(features)} county polygons")

    build_states(refresh)
    return features


def build_states(refresh=False):
    """State outlines, used as map context around the three-state footprint."""
    params = {
        "where": "1=1",
        "outFields": "STUSAB,NAME",
        "returnGeometry": "true",
        "f": "geojson",
        "outSR": "4326",
        "maxAllowableOffset": STATE_OFFSET,
    }
    url = f"{TIGERWEB_STATES}?{urllib.parse.urlencode(params)}"
    raw = download(url, TMP / "tigerweb_states.geojson", refresh)
    fc = json.loads(raw)

    features = []
    for feat in fc["features"]:
        code = feat["properties"].get("STUSAB")
        if not code or code in SKIP_STATES:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "state": code,
                "name": feat["properties"].get("NAME"),
                "footprint": code in ("IL", "WI", "MI"),
            },
            "geometry": {
                "type": feat["geometry"]["type"],
                "coordinates": _round_coords(feat["geometry"]["coordinates"]),
            },
        })

    features.sort(key=lambda f: f["properties"]["state"])
    if len(features) != 51:
        raise RuntimeError(
            f"expected 50 states + DC, got {len(features)}: "
            f"{[f['properties']['state'] for f in features]}"
        )
    write_json(DATA / "us_states.geojson", {
        "type": "FeatureCollection",
        "features": features,
    }, compact=True)
    print(f"  {len(features)} state outlines (50 states + DC)")
    return features


if __name__ == "__main__":
    build(refresh="--refresh" in sys.argv)
