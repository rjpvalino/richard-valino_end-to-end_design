"""Step 1c - hardware readiness. SYNTHETIC, fictional carrier.

Site counts are driven by both population and land area: dense counties need
capacity sites, sparse counties still need coverage sites along roads, so a
purely population-based count would give the smallest rural counties
implausibly close to zero sites.

Modernization: counties tagged "lte_only_sites" still have LTE-only sites on
air (Step 2 lowers confidence and raises a risk flag for these). Every other
county is fully NR-capable, which keeps the Step 2 hardware rule a crisp
yes/no rather than a fuzzy threshold.

Output: data/hardware.json
"""

from common import DATA, load_counties, rng_for, write_json
from edge_cases import assign

# Site count model.
POP_PER_SITE = 2600
SQMI_PER_SITE = 150
MIN_SITES = 3

# How far behind modernization is, for counties that still have LTE-only sites.
MODERNIZED_RANGE = {
    "urban": (0.78, 0.96),
    "suburban": (0.70, 0.94),
    "rural": (0.55, 0.90),
}


def build():
    print("Step 1c - hardware readiness (SYNTHETIC)")
    counties = load_counties()
    tag_map = assign(counties)

    hardware = {}
    for county in counties:
        fips = county["fips"]
        rnd = rng_for(fips)

        total_sites = max(
            MIN_SITES,
            round(county["population"] / POP_PER_SITE
                  + county["land_area_sqmi"] / SQMI_PER_SITE),
        )
        # Small site-count jitter so counties of similar size are not identical.
        total_sites = max(MIN_SITES, total_sites + rnd.randint(-2, 2))

        if "lte_only_sites" in tag_map[fips]:
            frac = rnd.uniform(*MODERNIZED_RANGE[county["tier"]])
            nr_capable = round(total_sites * frac)
            # Guarantee the edge case is actually visible in the data.
            nr_capable = max(1, min(total_sites - 1, nr_capable))
        else:
            nr_capable = total_sites

        lte_only = total_sites - nr_capable
        hardware[fips] = {
            "fips": fips,
            "name": county["name"],
            "state": county["state"],
            "total_sites": total_sites,
            "nr_capable_sites": nr_capable,
            "lte_only_sites": lte_only,
            "pct_modernized": round(100.0 * nr_capable / total_sites, 1),
            "fully_nr_capable": lte_only == 0,
        }

    write_json(DATA / "hardware.json", hardware)

    with_lte_only = [h for h in hardware.values() if h["lte_only_sites"] > 0]
    total = sum(h["total_sites"] for h in hardware.values())
    nr_cap = sum(h["nr_capable_sites"] for h in hardware.values())
    print(f"  {total:,} sites total, {nr_cap:,} NR-capable "
          f"({100.0 * nr_cap / total:.1f}%)")
    print(f"  counties with LTE-only sites remaining: {len(with_lte_only)}")
    print(f"  fully NR-capable counties: {len(hardware) - len(with_lte_only)}")
    return hardware


if __name__ == "__main__":
    build()
