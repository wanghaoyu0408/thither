"""Rough, deterministic ranking of place candidates.

Spec section 23: hard-filter, compute objective metrics, normalize, then hand a
short list to the LLM for the qualitative trade-off. Handing a model 30 raw
rows and asking it to compare them is exactly what this exists to avoid.

Scope note: this is the *rough* pass that turns ~20 candidates into 3-5. Per-
traveler preference weighting and group fairness are spec sections 23-24 and
land in M3/M7; the dimensions dict is already the extension point.

The total is never presented as objective truth - `DecisionScore.dimensions`
keeps every component so a recommendation can be explained rather than asserted.
"""

import math
from dataclasses import dataclass, field

from app.models.decision import DecisionScore
from app.models.evidence import CommunitySignal
from app.models.place import PlaceSummary
from app.services.geo import haversine_km
from app.services.scoring import combine, ranking_value

# Reviews needed before a rating is treated as fully trustworthy. A 5.0 from
# seven people should not outrank a 4.4 from three thousand.
CONFIDENCE_SATURATION = 500


@dataclass(frozen=True)
class RankingWeights:
    rating: float = 0.45
    confidence: float = 0.25
    proximity: float = 0.20
    price_fit: float = 0.10
    # What the community said. Modest on purpose: it reorders places Google has
    # already vouched for, and can never rescue one that failed a hard filter.
    community: float = 0.15


@dataclass
class RankedPlace:
    place: PlaceSummary
    score: DecisionScore
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)


def _rating_score(place: PlaceSummary) -> float | None:
    return None if place.rating is None else max(0.0, min(1.0, (place.rating - 3.0) / 2.0))


def _confidence_score(place: PlaceSummary) -> float | None:
    if place.rating_count is None:
        return None
    return min(1.0, math.log10(place.rating_count + 1) / math.log10(CONFIDENCE_SATURATION + 1))


def _proximity_score(place: PlaceSummary, origin: tuple[float, float] | None) -> float | None:
    if origin is None or place.lat is None or place.lng is None:
        return None
    km = haversine_km(origin[0], origin[1], place.lat, place.lng)
    # 1.0 next door, decaying to 0 by about 3km - walking distance in a city.
    return max(0.0, 1.0 - km / 3.0)


def _price_fit_score(place: PlaceSummary, target: int | None) -> float | None:
    if target is None or place.price_level is None:
        return None
    return max(0.0, 1.0 - abs(place.price_level - target) / 4.0)


def _community_score(signal: CommunitySignal | None) -> float | None:
    """How much the community liked it, from counts rather than a judgement call.

    Independent sources count for more than repeat mentions of the same one, and
    negative sentiment pulls down rather than merely failing to lift.
    """
    if signal is None or signal.mention_count == 0:
        return None

    breadth = min(1.0, signal.source_count / 3.0)
    depth = min(1.0, math.log10(signal.mention_count + 1) / math.log10(6))
    base = 0.6 * breadth + 0.4 * depth

    if signal.sentiment == "negative":
        base *= 0.2
    elif signal.sentiment == "mixed":
        base *= 0.7
    elif signal.sentiment == "unclear":
        base *= 0.8

    if signal.has_editorial_backing:
        base = min(1.0, base * 1.15)

    return round(base, 4)


def score_place(
    place: PlaceSummary,
    *,
    origin: tuple[float, float] | None = None,
    target_price_level: int | None = None,
    weights: RankingWeights | None = None,
    signal: CommunitySignal | None = None,
) -> RankedPlace:
    weights = weights or RankingWeights()

    components: dict[str, tuple[float | None, float]] = {
        "rating": (_rating_score(place), weights.rating),
        "confidence": (_confidence_score(place), weights.confidence),
        "proximity": (_proximity_score(place, origin), weights.proximity),
        "price_fit": (_price_fit_score(place, target_price_level), weights.price_fit),
        "community": (_community_score(signal), weights.community),
    }

    # Renormalization, coverage and the missing-dimension note all come from the
    # shared primitive rather than a second implementation of the same three
    # rules - which is also how places gained a real `coverage` figure.
    score = combine(components)

    pros: list[str] = []
    cons: list[str] = []
    if place.rating is not None and place.rating_count is not None:
        phrase = f"{place.rating} from {place.rating_count:,} reviews"
        (pros if place.rating >= 4.2 and place.rating_count >= 200 else cons).append(phrase)
    if place.rating_count is not None and place.rating_count < 50:
        cons.append(f"only {place.rating_count} reviews, so the rating is not yet reliable")
    if origin is not None and place.lat is not None and place.lng is not None:
        km = haversine_km(origin[0], origin[1], place.lat, place.lng)
        (pros if km <= 1.0 else cons).append(f"{km:.1f} km from the area centre (straight line)")

    if signal is not None and signal.mention_count:
        sources = ", ".join(signal.source_types)
        phrase = (
            f"mentioned by {signal.source_count} source(s) ({sources}), "
            f"sentiment {signal.sentiment}"
        )
        (cons if signal.sentiment == "negative" else pros).append(phrase)
        if signal.themes:
            pros.append("community notes: " + "; ".join(signal.themes[:3]))

    return RankedPlace(place=place, score=score, pros=pros, cons=cons)


def rank_places(
    places: list[PlaceSummary],
    *,
    origin: tuple[float, float] | None = None,
    target_price_level: int | None = None,
    weights: RankingWeights | None = None,
    min_rating: float | None = None,
    min_rating_count: int | None = None,
    limit: int | None = None,
    signals: dict[str, CommunitySignal] | None = None,
) -> list[RankedPlace]:
    """Hard-filter, then score, then sort. Filters disqualify; they never discount.

    Note the order: filtering happens before community signal is looked at, so
    being fashionable cannot rescue a place that is shut permanently or below the
    rating floor. Buzz reorders what Google has already vouched for.
    """
    survivors = [
        place
        for place in places
        if place.is_operational()
        and not (min_rating is not None and (place.rating is None or place.rating < min_rating))
        and not (
            min_rating_count is not None
            and (place.rating_count is None or place.rating_count < min_rating_count)
        )
    ]

    by_place_id = signals or {}
    ranked = [
        score_place(
            place,
            origin=origin,
            target_price_level=target_price_level,
            weights=weights,
            signal=by_place_id.get(place.place_id),
        )
        for place in survivors
    ]
    # ranking_value discounts thin evidence at ordering time; place_id breaks
    # ties so the order is stable across runs.
    ranked.sort(key=lambda item: (-ranking_value(item.score), item.place.place_id))

    return ranked[:limit] if limit else ranked
