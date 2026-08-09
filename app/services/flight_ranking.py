"""Ranking flights, and saying why (spec sections 23 and 30).

The dimensions are the ones the spec names: price, nonstop, total duration,
departure and arrival convenience, airport convenience, airline preference.
Weights come from `FlightPreferences`, which has carried
`nonstop_importance`, `price_importance`, `schedule_importance`,
`avoid_red_eye` and the airline lists since M1 and goes unused until now.

`explain_choice` is the part the acceptance is really asking for. "Why is the
dearer flight better?" has to be answered with figures that came from a tool -
a price gap, minutes saved, a real driving time - never with prose the model
made up. So it returns structure, and the model turns structure into a sentence.
"""

from dataclasses import dataclass, field

from app.models.decision import DecisionScore, FlightOptionData
from app.models.flight import AirportOption
from app.models.traveler import FlightPreferences
from app.services.scoring import PRICE_TOLERANCE, combine, price_score, ranking_value

__all__ = [
    "PRICE_TOLERANCE",
    "RankedFlight",
    "TradeOff",
    "cheapest_of",
    "explain_choice",
    "flies_avoided_airline",
    "rank_flights",
    "score_flight",
]

# Beyond this, a longer flight is simply "long"; further hours stop changing the
# decision much.
DURATION_CEILING_MINUTES = 24 * 60
# A drive this long makes an airport genuinely inconvenient.
GROUND_CEILING_MINUTES = 120.0

RED_EYE_HOURS = range(0, 6)


@dataclass
class RankedFlight:
    option: FlightOptionData
    score: DecisionScore
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)

    @property
    def per_person(self) -> float:
        money = self.option.price_per_person or self.option.price
        return money.amount

    @property
    def currency(self) -> str:
        money = self.option.price_per_person or self.option.price
        return money.currency


def _price_score(option: FlightOptionData, cheapest: float, dearest: float) -> float | None:
    """Delegates to the shared rule; hotels price the same way."""
    return price_score((option.price_per_person or option.price).amount, cheapest)


def _duration_score(option: FlightOptionData, shortest: int | None) -> float | None:
    if option.duration_minutes is None or shortest is None:
        return None
    excess = option.duration_minutes - shortest
    return max(0.0, 1.0 - excess / DURATION_CEILING_MINUTES)


def _nonstop_score(option: FlightOptionData) -> float | None:
    if option.stops is None:
        return None
    return {0: 1.0, 1: 0.5, 2: 0.2}.get(option.stops, 0.0)


def _departure_score(option: FlightOptionData, avoid_red_eye: bool) -> float | None:
    if option.departure_at is None:
        return None
    hour = option.departure_at.hour
    if hour in RED_EYE_HOURS:
        return 0.1 if avoid_red_eye else 0.6
    if 6 <= hour < 9:
        return 0.7
    if 9 <= hour < 18:
        return 1.0
    return 0.8


def _arrival_score(option: FlightOptionData) -> float | None:
    if option.arrival_at is None:
        return None
    hour = option.arrival_at.hour
    if hour in RED_EYE_HOURS:
        return 0.3
    if 6 <= hour < 22:
        return 1.0
    return 0.7


def _airport_score(option: FlightOptionData, ground: dict[str, float]) -> float | None:
    minutes = ground.get(option.origin)
    if minutes is None:
        return None
    return max(0.0, 1.0 - minutes / GROUND_CEILING_MINUTES)


def _airline_score(option: FlightOptionData, preferences: FlightPreferences) -> float | None:
    """Only rewards a preferred airline.

    Avoidance is handled as a filter rather than a discount: a traveller who
    listed an airline to avoid told us something, and a dimension weighted
    alongside price would let eighty dollars overrule it.
    """
    if not preferences.preferred_airlines:
        return None
    preferred = {code.upper() for code in preferences.preferred_airlines}
    return 1.0 if {code.upper() for code in option.airlines} & preferred else 0.4


def _red_eye_score(option: FlightOptionData, preferences: FlightPreferences) -> float | None:
    """Its own dimension, and only when the traveller asked.

    Folding this into general schedule preference made "please avoid red-eyes"
    worth about forty dollars, which is not what asking means.
    """
    if not preferences.avoid_red_eye or option.departure_at is None:
        return None
    departs_overnight = option.departure_at.hour in RED_EYE_HOURS
    arrives_overnight = option.arrival_at is not None and option.arrival_at.hour in RED_EYE_HOURS
    if departs_overnight:
        return 0.0
    return 0.4 if arrives_overnight else 1.0


def flies_avoided_airline(option: FlightOptionData, preferences: FlightPreferences) -> bool:
    avoided = {code.upper() for code in preferences.avoided_airlines}
    return bool(avoided and {code.upper() for code in option.airlines} & avoided)


def score_flight(
    option: FlightOptionData,
    *,
    cheapest: float,
    dearest: float,
    shortest: int | None,
    ground_minutes: dict[str, float] | None = None,
    preferences: FlightPreferences | None = None,
) -> RankedFlight:
    preferences = preferences or FlightPreferences()
    ground = ground_minutes or {}

    # Weight names map onto the spec's dimensions; schedule_importance covers
    # both ends of the day, since a traveller who cares about one usually cares
    # about the other.
    components: dict[str, tuple[float | None, float]] = {
        "price": (_price_score(option, cheapest, dearest), preferences.price_importance),
        "nonstop": (_nonstop_score(option), preferences.nonstop_importance),
        "duration": (_duration_score(option, shortest), 0.5 * preferences.schedule_importance),
        "departure": (
            _departure_score(option, preferences.avoid_red_eye),
            0.3 * preferences.schedule_importance,
        ),
        "arrival": (_arrival_score(option), 0.2 * preferences.schedule_importance),
        "airport": (_airport_score(option, ground), 0.4),
        "airline": (_airline_score(option, preferences), 0.3),
        "red_eye": (_red_eye_score(option, preferences), 0.6),
    }

    return RankedFlight(
        option=option,
        score=combine(components),
        pros=_pros(option, ground),
        cons=_cons(option, ground, preferences),
    )


def _pros(option: FlightOptionData, ground: dict[str, float]) -> list[str]:
    pros: list[str] = []
    if option.stops == 0:
        pros.append("nonstop")
    if option.duration_minutes:
        pros.append(f"{_hhmm(option.duration_minutes)} total travel time")
    minutes = ground.get(option.origin)
    if minutes is not None and minutes <= 30:
        pros.append(f"{option.origin} is {minutes:.0f} minutes' drive")
    if option.baggage and option.baggage.checked:
        pros.append(f"{option.baggage.checked} checked bag(s) included")
    if option.refundable:
        pros.append("refundable")
    return pros


def _cons(
    option: FlightOptionData, ground: dict[str, float], preferences: FlightPreferences
) -> list[str]:
    cons: list[str] = []
    if option.stops:
        where = ", ".join(option.connection_airports)
        cons.append(f"{option.stops} stop(s)" + (f" via {where}" if where else ""))
    minutes = ground.get(option.origin)
    if minutes is not None and minutes > 45:
        cons.append(f"{option.origin} is {minutes:.0f} minutes' drive")
    if option.departure_at and option.departure_at.hour in RED_EYE_HOURS:
        cons.append(f"departs {option.departure_at:%H:%M}")
        if preferences.avoid_red_eye:
            cons.append("this traveller asked to avoid red-eyes")
    if option.baggage and option.baggage.checked == 0:
        cons.append("no checked bag included")
    if flies_avoided_airline(option, preferences):
        flown = ", ".join(option.airlines)
        cons.append(f"flown by {flown}, which this traveller asked to avoid")
    return cons


def rank_flights(
    options: list[FlightOptionData],
    *,
    preferences: FlightPreferences | None = None,
    airports: list[AirportOption] | None = None,
    limit: int | None = None,
) -> list[RankedFlight]:
    """Score and sort. Ties break on offer_ref so the order is reproducible.

    Airlines the traveller asked to avoid are filtered out first, in the same
    way the place ranker hard-filters - unless that would leave nothing, in
    which case they come back with the objection stated in their cons rather
    than the traveller being told there are no flights.
    """
    if not options:
        return []

    preferences = preferences or FlightPreferences()
    acceptable = [option for option in options if not flies_avoided_airline(option, preferences)]
    if acceptable:
        options = acceptable

    ground = {
        airport.iata: airport.ground_travel_minutes
        for airport in (airports or [])
        if airport.ground_travel_minutes is not None
    }

    prices = [(option.price_per_person or option.price).amount for option in options]
    durations = [option.duration_minutes for option in options if option.duration_minutes]

    ranked = [
        score_flight(
            option,
            cheapest=min(prices),
            dearest=max(prices),
            shortest=min(durations) if durations else None,
            ground_minutes=ground,
            preferences=preferences,
        )
        for option in options
    ]
    # Ordered on ranking_value, not the stored total: the stored score says what
    # the evidence says, and the ordering discounts thin evidence (INVARIANTS.md
    # section 2). Price breaks a tie before the offer id does: two otherwise
    # identical options should never be separated by which reference sorted first.
    ranked.sort(
        key=lambda item: (-ranking_value(item.score), item.per_person, item.option.offer_ref)
    )
    return ranked[:limit] if limit else ranked


# --- explanation -------------------------------------------------------------


@dataclass
class TradeOff:
    """One comparison, in figures that came from a tool.

    Rendered by the model into a sentence; sourced here so that every number is
    traceable to stored data rather than invented (spec section 30).
    """

    recommended_ref: str
    alternative_ref: str

    price_difference: float
    currency: str
    minutes_saved: int | None
    stops_difference: int | None
    ground_difference: float | None

    statements: list[str] = field(default_factory=list)
    live_mode: bool = True

    @property
    def recommended_is_dearer(self) -> bool:
        return self.price_difference > 0


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _stops_phrase(recommended: RankedFlight, alternative: RankedFlight, difference: int) -> str:
    """How much less connecting the recommendation involves."""
    if recommended.option.stops != 0:
        return f"{difference} fewer stop(s)"

    via = ", ".join(alternative.option.connection_airports)
    if alternative.option.stops == 1 and via:
        return f"nonstop instead of one stop at {via}"
    return f"nonstop instead of {alternative.option.stops} stop(s)"


def explain_choice(
    recommended: RankedFlight,
    alternative: RankedFlight,
    *,
    airports: list[AirportOption] | None = None,
) -> TradeOff:
    """Why take this one over that one.

    Deliberately states the case even when the recommendation costs more - that
    is the comparison a traveller actually needs, and hiding the price gap would
    make the rest untrustworthy.
    """
    ground = {
        airport.iata: airport.ground_travel_minutes
        for airport in (airports or [])
        if airport.ground_travel_minutes is not None
    }

    price_gap = round(recommended.per_person - alternative.per_person, 2)

    minutes_saved = None
    if recommended.option.duration_minutes and alternative.option.duration_minutes:
        minutes_saved = alternative.option.duration_minutes - recommended.option.duration_minutes

    stops_difference = None
    if recommended.option.stops is not None and alternative.option.stops is not None:
        stops_difference = alternative.option.stops - recommended.option.stops

    ground_difference = None
    here = ground.get(recommended.option.origin)
    there = ground.get(alternative.option.origin)
    if here is not None and there is not None:
        ground_difference = round(there - here, 1)

    statements: list[str] = []
    currency = recommended.currency

    if price_gap > 0:
        statements.append(f"{price_gap:.0f} {currency} more per person")
    elif price_gap < 0:
        statements.append(f"{abs(price_gap):.0f} {currency} less per person")
    else:
        statements.append("the same price per person")

    if minutes_saved and minutes_saved > 0:
        statements.append(f"{_hhmm(minutes_saved)} shorter")
    elif minutes_saved and minutes_saved < 0:
        statements.append(f"{_hhmm(abs(minutes_saved))} longer")

    if stops_difference and stops_difference > 0:
        statements.append(_stops_phrase(recommended, alternative, stops_difference))

    if ground_difference is not None and abs(ground_difference) >= 5:
        if ground_difference > 0:
            statements.append(
                f"{recommended.option.origin} is {here:.0f} minutes' drive against "
                f"{there:.0f} to {alternative.option.origin}"
            )
        else:
            statements.append(
                f"{recommended.option.origin} is {here:.0f} minutes' drive, "
                f"{abs(ground_difference):.0f} more than {alternative.option.origin}"
            )

    return TradeOff(
        recommended_ref=recommended.option.offer_ref,
        alternative_ref=alternative.option.offer_ref,
        price_difference=price_gap,
        currency=currency,
        minutes_saved=minutes_saved,
        stops_difference=stops_difference,
        ground_difference=ground_difference,
        statements=statements,
        live_mode=recommended.option.live_mode and alternative.option.live_mode,
    )


def cheapest_of(ranked: list[RankedFlight]) -> RankedFlight | None:
    return min(ranked, key=lambda item: (item.per_person, item.option.offer_ref), default=None)
