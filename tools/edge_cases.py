"""Deterministic edge-case assignment.

The Step 2 rule engine has several branches that only fire on unusual counties
(LTE hotter than NR, an isolated PCS block, a missing NR layer). Left purely to
the density correlations, some of those branches would have no examples at all
in a mostly-rural three-state footprint. So specific counties are deliberately
tagged here, and the 1b/1c/1d generators read these tags so they stay consistent
with each other.

Assignment is seeded from a fixed constant over the FIPS list sorted
ascending, so it is stable across runs and independent of dict ordering.

All of this describes synthetic data for a fictional carrier.
"""

import random

SELECTION_SEED = 20260926

# Tag -> (target count, which tiers are eligible)
TARGETS = {
    # LTE more congested than NR *and* carrying more users -> forces Hold.
    "lte_hotter": (26, {"urban", "suburban", "rural"}),
    # LTE-only device share pushed above the 30% "not ready yet" threshold.
    "lte_only_heavy": (18, {"suburban", "rural"}),
    # LTE load climbing across the 12-month window instead of falling, while
    # still sitting below NR. Population growth, fixed wireless, and LTE-only
    # IoT all do this. Direction of travel is a confidence factor in Step 2:
    # the level still permits a carve, but the trend argues for a second look.
    "lte_rising": (24, {"urban", "suburban", "rural"}),
    # Owns an LTE PCS block that is not adjacent to its NR blocks, so the
    # block cannot extend the NR carrier and must stay LTE.
    "isolated_pcs_lte": (22, {"urban", "suburban", "rural"}),
    # Rural counties with no N2500 layer deployed at all.
    "no_n2500": (45, {"rural"}),
    # Counties that never held 700 MHz, so the LTE anchor must come from
    # L600, L1900 or L2100 instead.
    "no_l700": (20, {"suburban", "rural"}),
    # Modernization incomplete: LTE-only sites still on air. Confidence
    # penalty plus a risk flag in Step 2.
    "lte_only_sites": (72, {"urban", "suburban", "rural"}),
}


def assign(counties):
    """Return {fips: sorted list of tags} for every county.

    `counties` is the list of dicts produced by Step 1a; each needs "fips"
    and "tier".
    """
    by_tier = {}
    for c in counties:
        by_tier.setdefault(c["tier"], []).append(c["fips"])
    for fips_list in by_tier.values():
        fips_list.sort()

    tags = {c["fips"]: set() for c in counties}

    for tag, (target, tiers) in sorted(TARGETS.items()):
        pool = sorted(f for t in tiers for f in by_tier.get(t, []))
        # Separate RNG per tag so adding a tag does not reshuffle the others.
        rnd = random.Random(f"{SELECTION_SEED}:{tag}")
        n = min(target, len(pool))
        for fips in rnd.sample(pool, n):
            tags[fips].add(tag)

    # A county cannot be both "LTE is hotter" and a clean carve candidate with
    # a heavy LTE-only base at once without the reasons reading as noise, but
    # both are legitimate Hold drivers, so overlap is allowed and intentional.
    return {fips: sorted(t) for fips, t in tags.items()}


def summarize(tag_map):
    """Count how many counties carry each tag, for the run summary."""
    counts = {tag: 0 for tag in TARGETS}
    for tags in tag_map.values():
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + 1
    counts["(any edge case)"] = sum(1 for t in tag_map.values() if t)
    return counts
