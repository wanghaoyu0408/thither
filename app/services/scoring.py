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


def combine(components: dict[str, tuple[float | None, float]]) -> DecisionScore:
    """Weighted mean over the dimensions that have data.

    `None` means "not known", and is dropped from both the numerator and the
    denominator rather than scored as zero - an unknown must not quietly become
    a bad review. What was missing is named in the notes, so a score built on
    half the dimensions says so instead of looking equally confident.
    """
    dimensions = {
        name: round(value, 3) for name, (value, _weight) in components.items() if value is not None
    }
    available = sum(weight for value, weight in components.values() if value is not None)
    weighted = sum(value * weight for value, weight in components.values() if value is not None)
    total = weighted / available if available else 0.0

    missing = sorted(name for name, (value, _weight) in components.items() if value is None)
    return DecisionScore(
        total=round(total, 4),
        dimensions=dimensions,
        notes=f"no data for: {', '.join(missing)}" if missing else None,
    )
