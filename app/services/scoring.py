"""Scoring primitives shared by every ranker.

Two rules live here rather than in each ranker, because both were got wrong
independently once already and one implementation is one place to fix.

`price_score` in particular took three attempts. The version that normalizes
across the candidate range makes a ten-dollar gap score exactly like a
five-hundred-dollar one; the version with a floor collapses everything past the
tolerance to zero, so two very different prices tie and the winner falls to
whichever id sorts first. The decay below does neither.
"""

from app.models.decision import DecisionScore

# A price this far above the cheapest scores half. Roughly where "a bit more"
# becomes "a different budget".
PRICE_TOLERANCE = 0.30


def price_score(price: float, cheapest: float, *, tolerance: float = PRICE_TOLERANCE) -> float:
    """How much dearer than the cheapest, proportionally.

    Measured against the cheapest rather than spread across the range, so a
    small difference stays small. Decays smoothly and never reaches zero, so
    two dear options are still ordered by how dear they are.
    """
    if cheapest <= 0:
        return 1.0
    excess = max(0.0, (price - cheapest) / cheapest)
    return round(1.0 / (1.0 + excess / tolerance), 4)


# Below this share of the intended weight, a score is being computed from too
# little to stand on its own and is pulled back toward neutral. Half the
# evidence is the point at which "we know some things about this" stops being
# true.
COVERAGE_FLOOR = 0.5

# What an unknown is worth. Not zero, which would make missing data a bad
# review; not one, which would reward hiding.
NEUTRAL = 0.5


def damp_for_coverage(total: float, coverage: float) -> float:
    """Pull a score toward neutral in proportion to what was not known.

    One implementation, used both for a single score and for a group's shared
    verdict. Above the floor it does nothing; at zero coverage the result is
    entirely the prior. An option can still win on thin evidence, but it has to
    be genuinely better rather than merely unexamined.
    """
    if coverage <= 0.0 or coverage >= COVERAGE_FLOOR:
        return total
    trust = coverage / COVERAGE_FLOOR
    return trust * total + (1.0 - trust) * NEUTRAL


def combine(components: dict[str, tuple[float | None, float]]) -> DecisionScore:
    """Weighted mean over the dimensions that have data.

    `None` means "not known", and is dropped from both the numerator and the
    denominator rather than scored as zero - an unknown must not quietly become
    a bad review.

    But renormalizing alone lets an option win on ignorance. A hotel with three
    reviews has no usable guest rating, no star category and no measured travel
    time, so its *only* dimension is price - and being the cheapest then makes
    it a flawless 1.00 that beats a hotel we know five things about. That is not
    a better hotel, it is a hotel nobody has looked at.

    So a score covering less than `COVERAGE_FLOOR` of its intended weight is
    pulled toward neutral in proportion to what is missing. An option can still
    win on thin evidence, but it has to be genuinely better rather than merely
    unexamined, and `coverage` says how much was known either way.
    """
    dimensions = {
        name: round(value, 3) for name, (value, _weight) in components.items() if value is not None
    }
    available = sum(weight for value, weight in components.values() if value is not None)
    intended = sum(weight for _value, weight in components.values())
    weighted = sum(value * weight for value, weight in components.values() if value is not None)

    coverage = available / intended if intended else 0.0
    total = damp_for_coverage(weighted / available if available else 0.0, coverage)

    missing = sorted(name for name, (value, _weight) in components.items() if value is None)
    notes = f"no data for: {', '.join(missing)}" if missing else None
    if coverage < COVERAGE_FLOOR and missing:
        notes = f"{notes}; scored on {coverage:.0%} of the usual evidence"

    return DecisionScore(
        total=round(total, 4),
        dimensions=dimensions,
        coverage=round(coverage, 3),
        notes=notes,
    )
