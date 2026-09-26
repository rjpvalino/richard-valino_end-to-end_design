"""Step 1b - network + device metrics. SYNTHETIC, fictional carrier.

12 monthly records per county (2025-10 .. 2026-09), correlated with population
density. Over the window NR load and 5G device share trend up while LTE trends
down, so the Step 4 trend chart shows real movement.

Written into data/counties.json as a "network" block on each county, plus a
"latest" convenience copy of the most recent month.
"""

import sys

from common import DATA, load_counties, rng_for, write_json
from edge_cases import assign, summarize

MONTHS = [
    "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03",
    "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09",
]

# Tuning knobs. Ranges are for the FINAL month; earlier months are walked back
# along the trend. Adjust these to shift the Step 2 recommendation mix.
TIER_PARAMS = {
    "urban": {
        "share_5g": (84.0, 93.0),     # % of devices that are 5G-capable
        "nr_congestion": (62.0, 80.0),  # NR PRB utilization %
        "lte_congestion": (32.0, 52.0),  # LTE PRB utilization %
        "nr_coupling": (0.80, 0.90),  # fraction of 5G devices actually on NR
        "penetration": (0.62, 0.74),  # mobile lines per capita
        "market_share": (0.30, 0.38),  # GLA - Mobile share of that market
    },
    "suburban": {
        "share_5g": (76.0, 88.0),
        "nr_congestion": (52.0, 70.0),
        "lte_congestion": (30.0, 50.0),
        "nr_coupling": (0.76, 0.88),
        "penetration": (0.58, 0.70),
        "market_share": (0.27, 0.35),
    },
    "rural": {
        "share_5g": (66.0, 84.0),
        "nr_congestion": (34.0, 58.0),
        "lte_congestion": (24.0, 46.0),
        "nr_coupling": (0.70, 0.86),
        "penetration": (0.52, 0.66),
        "market_share": (0.24, 0.33),
    },
}

# 12-month movement, in percentage points.
SHARE_5G_RISE = (6.0, 11.0)
NR_CONGESTION_RISE = (5.0, 14.0)
LTE_CONGESTION_FALL = (3.0, 10.0)
# For the "lte_rising" edge case, how much LTE load climbs instead of falling.
LTE_CONGESTION_RISE = (5.0, 11.0)


def build():
    print("Step 1b - network + device metrics (SYNTHETIC)")
    counties = load_counties()
    tag_map = assign(counties)

    for county in counties:
        fips = county["fips"]
        tags = tag_map[fips]
        rnd = rng_for(fips)
        p = TIER_PARAMS[county["tier"]]

        share_5g_final = rnd.uniform(*p["share_5g"])
        nr_cong_final = rnd.uniform(*p["nr_congestion"])
        lte_cong_final = rnd.uniform(*p["lte_congestion"])
        coupling = rnd.uniform(*p["nr_coupling"])

        # Edge case: LTE-only base too heavy to refarm yet (>30% LTE-only).
        if "lte_only_heavy" in tags:
            share_5g_final = rnd.uniform(55.0, 66.0)

        # Edge case: LTE is the hotter, busier layer. Forces a Hold in Step 2.
        if "lte_hotter" in tags:
            lte_cong_final = nr_cong_final + rnd.uniform(8.0, 20.0)
            coupling = rnd.uniform(0.30, 0.45)

        share_5g_rise = rnd.uniform(*SHARE_5G_RISE)
        nr_cong_rise = rnd.uniform(*NR_CONGESTION_RISE)
        lte_cong_fall = rnd.uniform(*LTE_CONGESTION_FALL)

        # Edge case: LTE load climbing rather than falling, but still below NR.
        # A negative "fall" makes the earlier months lower than the latest one.
        # Kept clear of NR so this lands as a confidence penalty in Step 2, not
        # as a hard-rule Hold, which is the situation worth showing.
        if "lte_rising" in tags and "lte_hotter" not in tags:
            lte_cong_final = max(8.0, nr_cong_final - rnd.uniform(5.0, 13.0))
            lte_cong_fall = -rnd.uniform(*LTE_CONGESTION_RISE)

        total_users = int(
            county["population"] * rnd.uniform(*p["penetration"])
            * rnd.uniform(*p["market_share"])
        )
        # Subscriber base creeps up slightly across the year.
        total_start = total_users / rnd.uniform(1.02, 1.06)

        months = []
        for i, month in enumerate(MONTHS):
            t = i / (len(MONTHS) - 1)  # 0.0 at the oldest month, 1.0 at newest

            share_5g = share_5g_final - share_5g_rise * (1 - t)
            share_5g += rnd.uniform(-0.5, 0.5)  # month-to-month noise
            share_5g = max(5.0, min(99.0, share_5g))
            lte_only = 100.0 - share_5g

            nr_cong = nr_cong_final - nr_cong_rise * (1 - t) + rnd.uniform(-1.5, 1.5)
            lte_cong = lte_cong_final + lte_cong_fall * (1 - t) + rnd.uniform(-1.5, 1.5)
            nr_cong = max(2.0, min(99.0, nr_cong))
            lte_cong = max(2.0, min(99.0, lte_cong))

            total = total_start + (total_users - total_start) * t
            nr_users = int(total * (share_5g / 100.0) * coupling)
            lte_users = int(total) - nr_users

            months.append({
                "month": month,
                "lte_congestion_pct": round(lte_cong, 1),
                "nr_congestion_pct": round(nr_cong, 1),
                "lte_users": lte_users,
                "nr_users": nr_users,
                "pct_5g_devices": round(share_5g, 1),
                "pct_lte_only_devices": round(lte_only, 1),
            })

        latest = dict(months[-1])
        latest.pop("month")
        county["network"] = {
            "months": months,
            "latest_month": MONTHS[-1],
            "latest": latest,
        }

    write_json(DATA / "counties.json", counties)

    hotter = sum(
        1 for c in counties
        if c["network"]["latest"]["lte_congestion_pct"]
        > c["network"]["latest"]["nr_congestion_pct"]
    )
    more_lte_users = sum(
        1 for c in counties
        if c["network"]["latest"]["lte_users"] > c["network"]["latest"]["nr_users"]
    )
    heavy = sum(
        1 for c in counties
        if c["network"]["latest"]["pct_lte_only_devices"] > 30.0
    )
    print(f"  12 months x {len(counties)} counties")
    print(f"  LTE congestion > NR: {hotter} counties")
    print(f"  LTE users > NR users: {more_lte_users} counties")
    print(f"  LTE-only device share > 30%: {heavy} counties")
    print(f"  edge-case tags: {summarize(tag_map)}")
    return counties


if __name__ == "__main__":
    build()
    sys.exit(0)
