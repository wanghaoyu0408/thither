"""Scoring an option for a group rather than for a person (spec sections 23-24).

Sits *on top of* the existing rankers rather than inside them. `score_flight`,
`score_hotel` and `score_place` already encode what makes an option good for one
traveller, they were expensive to get right, and none of them change here. This
module calls them once per traveller and combines the results.

That is the whole trick, and it buys two things. The per-person rule stays in
one place, and the group layer can never quietly disagree with the individual
one - a flight Ann is shown as loving is scored by the same function that says
so.

The combination is `0.6*mean + 0.4*worst`, and the per-traveller scores are
carried through untouched. A mean alone cannot distinguish four people who are
mildly pleased from three delighted and one miserable, and the second is the
case a group planner exists to catch.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.models.decision import FlightOptionData, HotelOptionData
from app.models.flight import AirportOption
from app.models.group import (
    WORST_WEIGHT,
    GroupScore,
    PreferenceConflict,
    TravelerPreferences,
)
from app.models.place import PlaceSummary
from app.services.flight_ranking import flies_avoided_airline, score_flight
from app.services.hotel_ranking import score_hotel
from app.services.ranking_service import RankingWeights, score_place
from app.services.scoring import damp_for_coverage


def build_group_score(
    per_traveler: dict[str, float],
    names: dict[str, str] | None = None,
    coverage: float = 1.0,
) -> GroupScore:
    """Summarize per-traveller scores without discarding them.

    Lives here rather than on the model because it is arithmetic, and because
    the damping rule it shares with `combine` belongs to the service layer.
    """
    if not per_traveler:
        return GroupScore(names=names or {})

    values = list(per_traveler.values())
    worst = min(values)
    best = max(values)
    mean = sum(values) / len(values)
    # Ties break on traveler_id so the named person is stable between runs.
    worst_id = min(per_traveler, key=lambda tid: (per_traveler[tid], tid))

    return GroupScore(
        per_traveler={tid: round(value, 4) for tid, value in per_traveler.items()},
        names=names or {},
        mean=round(mean, 4),
        worst=round(worst, 4),
        best=round(best, 4),
        worst_traveler_id=worst_id,
        spread=round(best - worst, 4),
        # The group's shared verdict gets the same treatment an individual score
        # does, against the worst-informed member's coverage. Otherwise a hotel
        # with three reviews leads the ranking because the one traveller who
        # only cares about price happens to know the one thing there is to know.
        # You cannot recommend what nobody has looked at.
        total=round(
            damp_for_coverage((1.0 - WORST_WEIGHT) * mean + WORST_WEIGHT * worst, coverage), 4
        ),
        coverage=round(coverage, 3),
    )


@dataclass
class GroupRanked[T]:
    """One option, scored per traveller and then for the group."""

    option: T
    group: GroupScore
    # The individual rankings behind the group score, keyed by traveler_id, so
    # "why is this bad for Dee?" is answerable without re-deriving anything.
    per_traveler: dict[str, Any] = field(default_factory=dict)
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.group.total


def score_for_group[T](
    options: Sequence[T],
    *,
    travelers: dict[str, TravelerPreferences],
    names: dict[str, str],
    score_one: Callable[[T, TravelerPreferences], Any],
    key: Callable[[T], str],
) -> list[GroupRanked[T]]:
    """Score every option for every traveller, then combine.

    `score_one` returns whatever the per-traveller ranker returns, as long as it
    carries a `.score.total`. Keeping the whole ranked object rather than just
    the number means the group layer can quote a traveller's own pros and cons
    back rather than inventing a reason they are unhappy.
    """
    ranked: list[GroupRanked[T]] = []

    for option in options:
        per_traveler: dict[str, Any] = {}
        totals: dict[str, float] = {}

        for traveler_id, preferences in travelers.items():
            scored = score_one(option, preferences)
            per_traveler[traveler_id] = scored
            totals[traveler_id] = scored.score.total

        # The thinnest evidence anyone's score rested on. Four people agreeing
        # about an option nobody has data for is not agreement.
        coverage = min((scored.score.coverage for scored in per_traveler.values()), default=1.0)
        group = build_group_score(totals, names, coverage=coverage)
        ranked.append(
            GroupRanked(
                option=option,
                group=group,
                per_traveler=per_traveler,
                pros=_group_pros(group, per_traveler),
                cons=_group_cons(group, per_traveler),
            )
        )

    # Ties break on the option key so the order is reproducible.
    ranked.sort(key=lambda item: (-item.group.total, key(item.option)))
    return ranked


def _group_pros(group: GroupScore, per_traveler: dict[str, Any]) -> list[str]:
    pros: list[str] = []
    if group.per_traveler and not group.is_split and not group.thinly_evidenced:
        pros.append(f"works for everyone ({group.worst:.2f} at worst)")

    happiest = max(group.per_traveler, key=lambda tid: (group.per_traveler[tid], tid), default=None)
    if happiest is not None and group.per_traveler[happiest] >= 0.75:
        reasons = getattr(per_traveler.get(happiest), "pros", [])
        if reasons:
            pros.append(f"{group.name_of(happiest)}: {reasons[0]}")
    return pros


def _group_cons(group: GroupScore, per_traveler: dict[str, Any]) -> list[str]:
    """The unhappiest traveller's own objection, in their ranker's words."""
    cons: list[str] = []
    if group.thinly_evidenced:
        cons.append(
            f"scored on only {group.coverage:.0%} of the usual evidence, so this ranking "
            f"says more about what is unknown than about the option"
        )

    worst_id = group.worst_traveler_id
    if worst_id is None:
        return cons

    if group.is_split:
        cons.append(
            f"{group.name_of(worst_id)} scores this {group.worst:.2f} while others reach "
            f"{group.best:.2f}"
        )

    objections = getattr(per_traveler.get(worst_id), "cons", [])
    for objection in objections[:2]:
        cons.append(f"{group.name_of(worst_id)}: {objection}")
    return cons


# --- per-kind entry points ---------------------------------------------------


def rank_flights_for_group(
    options: list[FlightOptionData],
    *,
    travelers: dict[str, TravelerPreferences],
    names: dict[str, str],
    airports: list[AirportOption] | None = None,
    limit: int | None = None,
) -> tuple[list[GroupRanked[FlightOptionData]], list[str]]:
    """Group flight ranking, with the avoided-airline rule applied group-wide.

    An airline *anyone* asked to avoid is filtered for the whole group: M5's
    rule was that a stated objection is not something eighty dollars may
    overrule, and that does not become less true because three other people do
    not mind. If the union of everyone's objections leaves nothing, the options
    come back with the objection stated instead of the group being told there
    are no flights.
    """
    warnings: list[str] = []
    if not options:
        return [], warnings

    acceptable = [
        option
        for option in options
        if not any(
            flies_avoided_airline(option, preferences.flight) for preferences in travelers.values()
        )
    ]
    if acceptable and len(acceptable) < len(options):
        removed = len(options) - len(acceptable)
        warnings.append(
            f"{removed} flight(s) fly an airline someone on this trip asked to avoid, and "
            f"were removed rather than scored down"
        )
        options = acceptable
    elif not acceptable:
        warnings.append(
            "every flight found flies an airline somebody asked to avoid; they are shown "
            "with that objection rather than withheld"
        )

    ground = {
        airport.iata: airport.ground_travel_minutes
        for airport in (airports or [])
        if airport.ground_travel_minutes is not None
    }
    prices = [(option.price_per_person or option.price).amount for option in options]
    durations = [option.duration_minutes for option in options if option.duration_minutes]

    ranked = score_for_group(
        options,
        travelers=travelers,
        names=names,
        score_one=lambda option, prefs: score_flight(
            option,
            cheapest=min(prices),
            dearest=max(prices),
            shortest=min(durations) if durations else None,
            ground_minutes=ground,
            preferences=prefs.flight,
        ),
        key=lambda option: option.offer_ref,
    )
    return (ranked[:limit] if limit else ranked), warnings


def rank_hotels_for_group(
    options: list[HotelOptionData],
    *,
    travelers: dict[str, TravelerPreferences],
    names: dict[str, str],
    limit: int | None = None,
    cheapest: float | None = None,
) -> list[GroupRanked[HotelOptionData]]:
    """Group hotel ranking.

    A stated `min_rating` is a filter for the traveller who stated it, but for
    the group it is a *conflict* rather than a veto - one person's four-star
    floor should not silently delete everything the other three liked. It is
    left to `conflict_service` to raise, and to the group to settle.
    """
    if not options:
        return []

    if cheapest is None:
        prices = [
            option.nightly_price.amount for option in options if option.nightly_price is not None
        ]
        cheapest = min(prices) if prices else None

    ranked = score_for_group(
        options,
        travelers=travelers,
        names=names,
        score_one=lambda option, prefs: score_hotel(
            option, cheapest=cheapest, preferences=prefs.hotel
        ),
        key=lambda option: option.offer_ref or option.name,
    )
    return ranked[:limit] if limit else ranked


def rank_places_for_group(
    places: list[PlaceSummary],
    *,
    travelers: dict[str, TravelerPreferences],
    names: dict[str, str],
    origin: tuple[float, float] | None = None,
    signals: dict | None = None,
    limit: int | None = None,
) -> list[GroupRanked[PlaceSummary]]:
    """Group place ranking.

    `score_place` takes weights rather than a preferences object, so each
    traveller's food and activity preferences are turned into weights first.
    The mapping is deliberately shallow - it shifts emphasis, it does not invent
    a new opinion about a restaurant nobody has been to.
    """
    if not places:
        return []

    by_place_id = signals or {}

    return score_for_group(
        places,
        travelers=travelers,
        names=names,
        score_one=lambda place, prefs: score_place(
            place,
            origin=origin,
            target_price_level=_target_price_level(prefs),
            weights=_weights_for(prefs),
            signal=by_place_id.get(place.place_id),
        ),
        key=lambda place: place.place_id,
    )[: limit or None]


def _weights_for(preferences: TravelerPreferences) -> RankingWeights:
    """One traveller's taste as ranking weights.

    The dimensions have to move in *different* directions to mean anything.
    `combine` renormalizes over whatever weights are present, so scaling them
    all by one factor produces an identical score - a preference that changes
    nothing, which is the failure this milestone keeps finding.

    So the claim made here is narrow and real: a cautious eater wants a place
    the crowd has already validated, and leans on review count; an adventurous
    one will take a sixty-review counter on somebody's word, and leans on what
    people actually said. The rating itself is left alone, because a good
    restaurant is good for both of them.
    """
    base = RankingWeights()
    adventurousness = preferences.food.adventurousness
    return RankingWeights(
        rating=base.rating,
        # Wanting proof and wanting discovery pull opposite ways.
        confidence=base.confidence * (1.0 + 0.8 * (0.5 - adventurousness)),
        proximity=base.proximity,
        price_fit=base.price_fit,
        community=base.community * (1.0 + 0.8 * (adventurousness - 0.5)),
    )


def _target_price_level(preferences: TravelerPreferences) -> int | None:
    """Fine-dining interest as a Google 0-4 price level, or nothing.

    Only expressed when the traveller actually leans one way. A default 0.5
    means "no view", and turning that into "price level 2" would be inventing a
    preference in order to have one.
    """
    interest = preferences.food.fine_dining_interest
    if 0.35 <= interest <= 0.65:
        return None
    return 3 if interest > 0.65 else 1


def conflicts_touching(
    conflicts: list[PreferenceConflict], marker: str
) -> list[PreferenceConflict]:
    """Conflicts that bear on one decision or item, for attaching to a shortlist."""
    return [conflict for conflict in conflicts if marker in conflict.affects]


def neutral_group(names: dict[str, str]) -> dict[str, TravelerPreferences]:
    """Everyone on neutral defaults - what scoring falls back to with no profiles."""
    return {traveler_id: TravelerPreferences() for traveler_id in names}
