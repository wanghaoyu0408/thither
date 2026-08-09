"""Scoring primitives shared by every ranker.

These rules live here rather than in each ranker, because each was got wrong
independently once already and one implementation is one place to fix.

`price_score` in particular took three attempts. The version that normalizes
across the candidate range makes a ten-dollar gap score exactly like a
five-hundred-dollar one; the version with a floor collapses everything past the
tolerance to zero, so two very different prices tie and the winner falls to
whichever id sorts first. The decay below does neither.

The load-bearing separation (INVARIANTS.md section 2): **a score says what the
evidence says; `coverage` says how much evidence there is; and the two are
never folded into one stored number.** `DecisionScore.total` is the undamped
weighted mean over the dimensions that had data. Confidence enters exactly
once, at *ordering* time, through `ranking_value` - so a stored score never
understates what was measured, and a ranking never trusts what was not.
"""

from app.models.common import Attestation
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
    """Pull a value toward neutral in proportion to what was not known.

    Above the floor it does nothing; at zero coverage the result is entirely
    the prior. An option can still win on thin evidence, but it has to be
    genuinely better rather than merely unexamined.
    """
    if coverage <= 0.0 or coverage >= COVERAGE_FLOOR:
        return total
    trust = coverage / COVERAGE_FLOOR
    return trust * total + (1.0 - trust) * NEUTRAL


def ranking_value(score: DecisionScore) -> float:
    """What ordering sorts on: the evidence, discounted by how thin it is.

    This is the *only* place confidence and score meet, and the result is never
    stored. A hotel with three reviews had price as its sole dimension, and
    being cheapest made it a flawless 1.00 that outranked a hotel we knew five
    things about - that is not a better hotel, it is a hotel nobody has looked
    at. Every ranker sorts on this rather than on `total` so that cannot recur,
    while the stored score keeps saying exactly what was measured.
    """
    return damp_for_coverage(score.total, score.coverage)


def floor_check(
    value: float | None,
    review_count: int | None,
    floor: float,
    *,
    min_reviews: int,
) -> Attestation:
    """Whether a rating clears a stated floor, answered in the tri-state.

    A floor is a factual claim, and sparse data cannot settle a factual claim
    in either direction: a 5.0 from three reviews is too thin to *score* on,
    so letting it *pass* a stated 4.5 floor would make the same number
    untrustworthy and authoritative at once. Absent or under-evidenced ratings
    return "unknown" - which neither passes nor fails, and is left to the
    conflict layer to raise rather than silently resolved either way.
    """
    if value is None:
        return "unknown"
    if review_count is not None and review_count < min_reviews:
        return "unknown"
    return "confirmed_true" if value >= floor else "confirmed_false"


def combine(components: dict[str, tuple[float | None, float]]) -> DecisionScore:
    """Weighted mean over the dimensions that have data, plus how many there were.

    `None` means "not known", and is dropped from both the numerator and the
    denominator rather than scored as zero - an unknown must not quietly become
    a bad review.

    `total` is deliberately *not* damped here: it reports what the evidence
    says, `coverage` reports how much evidence there is, and blending them
    would leave one number doing two jobs - a total that understates thin-but-
    real evidence is as misleading as one that overstates it. Ordering applies
    the discount through `ranking_value`; nothing stored ever does.
    """
    dimensions = {
        name: round(value, 3) for name, (value, _weight) in components.items() if value is not None
    }
    available = sum(weight for value, weight in components.values() if value is not None)
    intended = sum(weight for _value, weight in components.values())
    weighted = sum(value * weight for value, weight in components.values() if value is not None)

    coverage = available / intended if intended else 0.0
    total = weighted / available if available else 0.0

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
