"""Milestone 5 acceptance.

    Compare SFO / SJC / OAK when reasonable, and explain why a slightly more
    expensive flight may be preferable.

Fixtures rather than the sandbox, deliberately. Duffel's own docs say test mode
"won't see realistic flight schedules or prices", so the reasoning is proved
against offers shaped like real ones. The sandbox proves the contract; these
prove the argument.
"""

from datetime import UTC, date, datetime, timedelta

from app.models.common import Money
from app.models.decision import Decision, DecisionOption, FlightOptionData
from app.models.flight import AirportOption, BaggageAllowance, FlightSegment, FlightSlice
from app.models.traveler import FlightPreferences
from app.models.trip import TripState
from app.services.flight_ranking import cheapest_of, explain_choice, rank_flights
from app.services.validation_service import validate_itinerary

DEPART = date(2026, 10, 3)

# Real drive times from the Bay Area, of the kind the Routes API returns.
BAY_AREA = [
    AirportOption(
        iata="SFO",
        name="San Francisco International",
        city="San Francisco",
        lat=37.6213,
        lng=-122.379,
        ground_travel_minutes=18.0,
        ground_travel_source="routes_api",
    ),
    AirportOption(
        iata="OAK",
        name="Oakland International",
        city="Oakland",
        lat=37.7213,
        lng=-122.221,
        ground_travel_minutes=34.0,
        ground_travel_source="routes_api",
    ),
    AirportOption(
        iata="SJC",
        name="Norman Y. Mineta San Jose International",
        city="San Jose",
        lat=37.3626,
        lng=-121.929,
        ground_travel_minutes=61.0,
        ground_travel_source="routes_api",
    ),
]


def segment(origin, destination, depart, arrive, carrier="UA", minutes=None) -> FlightSegment:
    return FlightSegment(
        origin=origin,
        destination=destination,
        departing_at=depart,
        arriving_at=arrive,
        marketing_carrier=carrier,
        duration_minutes=minutes,
    )


def offer(
    ref: str,
    origin: str,
    *,
    price: float,
    stops: int,
    minutes: int,
    depart_hour: int = 11,
    carrier: str = "UA",
    via: str | None = None,
    live_mode: bool = True,
    expires_in: timedelta | None = timedelta(hours=1),
) -> FlightOptionData:
    depart = datetime(2026, 10, 3, depart_hour, 0)
    arrive = depart + timedelta(minutes=minutes)

    if stops == 0:
        segments = [segment(origin, "NRT", depart, arrive, carrier, minutes)]
    else:
        midpoint = depart + timedelta(minutes=minutes // 2)
        hop = via or "LAX"
        segments = [
            segment(origin, hop, depart, midpoint, carrier, minutes // 2),
            segment(hop, "NRT", midpoint + timedelta(minutes=70), arrive, carrier, minutes // 2),
        ]

    return FlightOptionData(
        provider="duffel",
        offer_ref=ref,
        live_mode=live_mode,
        price=Money(amount=price * 4, currency="USD"),
        price_per_person=Money(amount=price, currency="USD"),
        origin=origin,
        destination="NRT",
        slices=[
            FlightSlice(
                origin=origin,
                destination="NRT",
                departing_at=depart,
                arriving_at=arrive,
                duration_minutes=minutes,
                segments=segments,
            )
        ],
        departure_at=depart,
        arrival_at=arrive,
        duration_minutes=minutes,
        stops=stops,
        airlines=[carrier],
        baggage=BaggageAllowance(checked=1, cabin=1),
        expires_at=datetime.now(UTC) + expires_in if expires_in else None,
    )


# The comparison the acceptance describes: a nonstop from the near airport
# against a cheaper one-stop from a further one.
SFO_NONSTOP = offer("off_sfo", "SFO", price=642.0, stops=0, minutes=655)
OAK_ONE_STOP = offer("off_oak", "OAK", price=600.0, stops=1, minutes=890, via="LAX")
SJC_ONE_STOP = offer("off_sjc", "SJC", price=618.0, stops=1, minutes=920, via="SEA")

ALL_THREE = [SFO_NONSTOP, OAK_ONE_STOP, SJC_ONE_STOP]


# --- the acceptance ----------------------------------------------------------


def test_three_bay_area_airports_are_compared_in_one_ranking():
    ranked = rank_flights(ALL_THREE, airports=BAY_AREA)

    assert {item.option.origin for item in ranked} == {"SFO", "OAK", "SJC"}
    assert len(ranked) == 3


def test_the_slightly_dearer_nonstop_wins_for_a_normal_traveller():
    """42 dollars more, four hours shorter, and a much shorter drive."""
    preferences = FlightPreferences(
        nonstop_importance=0.9, price_importance=0.6, schedule_importance=0.8
    )

    ranked = rank_flights(ALL_THREE, preferences=preferences, airports=BAY_AREA)

    assert ranked[0].option.offer_ref == "off_sfo"
    assert ranked[0].per_person > cheapest_of(ranked).per_person


def test_the_explanation_states_the_trade_off_in_real_figures():
    preferences = FlightPreferences(nonstop_importance=0.9, price_importance=0.6)
    ranked = rank_flights(ALL_THREE, preferences=preferences, airports=BAY_AREA)

    trade_off = explain_choice(ranked[0], cheapest_of(ranked), airports=BAY_AREA)
    said = " | ".join(trade_off.statements)

    assert trade_off.recommended_is_dearer
    assert "42 USD more per person" in said
    assert "3h55m shorter" in said
    assert "nonstop instead of one stop at LAX" in said
    assert "SFO is 18 minutes' drive against 34 to OAK" in said


def test_every_figure_in_the_explanation_traces_to_stored_data():
    """Spec section 30: no number reaches the user that a tool did not produce."""
    preferences = FlightPreferences(nonstop_importance=0.9, price_importance=0.6)
    ranked = rank_flights(ALL_THREE, preferences=preferences, airports=BAY_AREA)
    trade_off = explain_choice(ranked[0], cheapest_of(ranked), airports=BAY_AREA)

    assert trade_off.price_difference == round(
        ranked[0].per_person - cheapest_of(ranked).per_person, 2
    )
    assert trade_off.minutes_saved == (
        cheapest_of(ranked).option.duration_minutes - ranked[0].option.duration_minutes
    )
    assert trade_off.ground_difference == 34.0 - 18.0


def test_a_price_first_traveller_gets_the_cheap_one_stop():
    """Weights have to actually move the answer, or they are decoration."""
    thrifty = FlightPreferences(
        price_importance=1.0, nonstop_importance=0.05, schedule_importance=0.05
    )

    ranked = rank_flights(ALL_THREE, preferences=thrifty, airports=BAY_AREA)

    assert ranked[0].option.offer_ref == "off_oak"


def test_the_explanation_is_honest_when_the_recommendation_is_also_cheapest():
    cheap_nonstop = offer("off_cheap", "SFO", price=520.0, stops=0, minutes=650)
    ranked = rank_flights([cheap_nonstop, OAK_ONE_STOP], airports=BAY_AREA)

    trade_off = explain_choice(ranked[0], ranked[1], airports=BAY_AREA)

    assert trade_off.recommended_is_dearer is False
    assert any("less per person" in line for line in trade_off.statements)


def test_a_trivial_price_gap_does_not_score_like_a_large_one():
    """Price is measured against the cheapest fare, not stretched over the range.

    Normalizing across the candidate spread made a ten-dollar difference look
    identical to a five-hundred-dollar one.
    """
    from app.services.flight_ranking import score_flight

    def price_score(amount: float, cheapest: float) -> float:
        return score_flight(
            offer("off_x", "SFO", price=amount, stops=0, minutes=655),
            cheapest=cheapest,
            dearest=amount,
            shortest=655,
        ).score.dimensions["price"]

    assert price_score(600.0, 600.0) == 1.0
    assert price_score(610.0, 600.0) > 0.9
    assert price_score(1100.0, 600.0) < 0.4


def test_price_keeps_discriminating_among_expensive_fares():
    """The floored version made everything past the tolerance score 0.0.

    Seen live: a 916 and a 1072 fare tied, the tie fell to whichever offer id
    sorted first, and the dearer one was recommended for no reason at all.
    """
    from app.services.flight_ranking import score_flight

    def price_score(amount: float) -> float:
        return score_flight(
            offer("off_x", "SFO", price=amount, stops=0, minutes=635),
            cheapest=526.0,
            dearest=1200.0,
            shortest=635,
        ).score.dimensions["price"]

    assert price_score(916.0) > price_score(1072.0)
    assert price_score(1072.0) > 0.0


def test_two_identical_fares_are_separated_by_price_not_by_offer_id():
    cheaper = offer("off_zzz", "SFO", price=916.0, stops=0, minutes=635)
    dearer = offer("off_aaa", "SFO", price=1072.0, stops=0, minutes=635)

    ranked = rank_flights([dearer, cheaper], airports=BAY_AREA)

    assert ranked[0].option.offer_ref == "off_zzz"


def test_airport_drive_time_moves_the_ranking_on_its_own():
    """Identical flights from two airports differ only by the drive."""
    from_sfo = offer("off_a", "SFO", price=600.0, stops=0, minutes=655)
    from_sjc = offer("off_b", "SJC", price=600.0, stops=0, minutes=655)

    ranked = rank_flights([from_sjc, from_sfo], airports=BAY_AREA)

    assert ranked[0].option.origin == "SFO"
    assert ranked[0].score.dimensions["airport"] > ranked[1].score.dimensions["airport"]


def test_a_flight_from_an_airport_with_no_drive_time_is_not_penalised():
    """Unknown is not bad - the dimension is skipped, as elsewhere."""
    unknown = offer("off_unknown", "LAX", price=642.0, stops=0, minutes=655)

    ranked = rank_flights([unknown], airports=BAY_AREA)

    assert "airport" not in ranked[0].score.dimensions
    assert "airport" in (ranked[0].score.notes or "")


# --- red-eyes and preferences ------------------------------------------------


def test_a_red_eye_is_penalised_for_someone_who_asked_to_avoid_them():
    red_eye = offer("off_red", "SFO", price=560.0, stops=0, minutes=655, depart_hour=1)
    daytime = offer("off_day", "SFO", price=600.0, stops=0, minutes=655, depart_hour=11)

    avoids = FlightPreferences(avoid_red_eye=True, schedule_importance=0.9, price_importance=0.4)
    ranked = rank_flights([red_eye, daytime], preferences=avoids, airports=BAY_AREA)

    assert ranked[0].option.offer_ref == "off_day"
    assert any("red-eye" in con for con in ranked[-1].cons)


def test_an_avoided_airline_is_filtered_out_not_merely_discounted():
    """Being told to avoid an airline should not be outbid by eighty dollars."""
    disliked = offer("off_x", "SFO", price=560.0, stops=0, minutes=655, carrier="XX")
    fine = offer("off_y", "SFO", price=640.0, stops=0, minutes=655, carrier="UA")

    preferences = FlightPreferences(avoided_airlines=["XX"], price_importance=0.5)
    ranked = rank_flights([disliked, fine], preferences=preferences, airports=BAY_AREA)

    assert [item.option.offer_ref for item in ranked] == ["off_y"]


def test_an_avoided_airline_still_shows_when_it_is_the_only_option():
    """Better to show it with the objection than to claim there are no flights."""
    only = offer("off_x", "SFO", price=560.0, stops=0, minutes=655, carrier="XX")

    preferences = FlightPreferences(avoided_airlines=["XX"])
    ranked = rank_flights([only], preferences=preferences, airports=BAY_AREA)

    assert len(ranked) == 1
    assert any("asked to avoid" in con for con in ranked[0].cons)


def test_a_preferred_airline_is_rewarded_without_excluding_others():
    preferred = offer("off_p", "SFO", price=660.0, stops=0, minutes=655, carrier="NH")
    other = offer("off_o", "SFO", price=650.0, stops=0, minutes=655, carrier="UA")

    preferences = FlightPreferences(preferred_airlines=["NH"], price_importance=0.2)
    ranked = rank_flights([preferred, other], preferences=preferences, airports=BAY_AREA)

    assert len(ranked) == 2
    assert ranked[0].option.offer_ref == "off_p"


# --- sandbox data must never look real ---------------------------------------


def test_live_mode_has_no_default_and_must_be_stated():
    """Forgetting to say which it is should be impossible, not merely discouraged."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FlightOptionData(
            provider="duffel",
            offer_ref="off_x",
            price=Money(amount=100),
            origin="SFO",
            destination="NRT",
        )


def test_the_sandbox_flag_survives_into_trip_state():
    sandbox = offer("off_test", "SFO", price=312.0, stops=0, minutes=655, live_mode=False)

    state = TripState.new(title="Tokyo")
    state.decisions.flights = Decision[FlightOptionData](
        decision_id="dec_flights",
        options=[DecisionOption[FlightOptionData](option_id="opt_1", data=sandbox)],
    )

    restored = TripState.model_validate(state.model_dump(mode="json"))

    assert restored.decisions.flights.options[0].data.live_mode is False


def test_the_validator_flags_a_trip_holding_sandbox_flights():
    sandbox = offer("off_test", "SFO", price=312.0, stops=0, minutes=655, live_mode=False)

    state = TripState.new(title="Tokyo")
    state.decisions.flights = Decision[FlightOptionData](
        decision_id="dec_flights",
        options=[DecisionOption[FlightOptionData](option_id="opt_1", data=sandbox)],
    )

    issues = validate_itinerary(state).issues
    flagged = [issue for issue in issues if issue.type == "sandbox_flight_data"]

    assert flagged
    assert flagged[0].severity == "info"
    assert "not real" in flagged[0].message


def test_selecting_a_sandbox_flight_raises_the_severity():
    sandbox = offer("off_test", "SFO", price=312.0, stops=0, minutes=655, live_mode=False)

    state = TripState.new(title="Tokyo")
    state.decisions.flights = Decision[FlightOptionData](
        decision_id="dec_flights",
        options=[DecisionOption[FlightOptionData](option_id="opt_1", data=sandbox)],
        selected_option_id="opt_1",
    )

    flagged = [
        issue for issue in validate_itinerary(state).issues if issue.type == "sandbox_flight_data"
    ]

    assert flagged[0].severity == "warning"
    assert "currently selected" in flagged[0].message


def test_real_flights_raise_no_sandbox_warning():
    state = TripState.new(title="Tokyo")
    state.decisions.flights = Decision[FlightOptionData](
        decision_id="dec_flights",
        options=[DecisionOption[FlightOptionData](option_id="opt_1", data=SFO_NONSTOP)],
        selected_option_id="opt_1",
    )

    assert not [
        issue for issue in validate_itinerary(state).issues if issue.type == "sandbox_flight_data"
    ]


def test_a_trade_off_between_sandbox_offers_is_marked_as_such():
    a = offer("off_a", "SFO", price=642.0, stops=0, minutes=655, live_mode=False)
    b = offer("off_b", "OAK", price=600.0, stops=1, minutes=890, live_mode=False)

    ranked = rank_flights([a, b], airports=BAY_AREA)
    trade_off = explain_choice(ranked[0], ranked[1], airports=BAY_AREA)

    assert trade_off.live_mode is False


# --- how a failure is reported -----------------------------------------------


async def test_an_auth_failure_is_not_reported_as_something_worth_retrying():
    """Seen live: a token missing a permission came back as a passing outage.

    Flattening every fan-out failure into provider_unavailable/retryable would
    have the agent retrying a request that can never succeed.
    """
    from app.models.flight import SearchFlightsInput
    from app.providers.base import ProviderAuthFailed
    from app.services.cache import InProcessCache, LayeredCache
    from app.services.flight_service import FlightService

    class RefusingProvider:
        live_mode = True

        async def search_offers(self, *, origin, destination, spec):
            raise ProviderAuthFailed("insufficient permissions", "duffel", 403)

    service = FlightService(RefusingProvider(), LayeredCache(InProcessCache(), None))

    result = await service.search_flights(
        SearchFlightsInput(origins=["SFO"], destinations=["NRT"], departure_date=DEPART)
    )

    assert result.ok is False
    assert result.error.code == "auth_failed"
    assert result.error.retryable is False
    assert "insufficient permissions" in result.error.message


async def test_a_genuine_outage_stays_retryable():
    from app.models.flight import SearchFlightsInput
    from app.providers.base import ProviderUnavailable
    from app.services.cache import InProcessCache, LayeredCache
    from app.services.flight_service import FlightService

    class DownProvider:
        live_mode = True

        async def search_offers(self, *, origin, destination, spec):
            raise ProviderUnavailable("502 bad gateway", "duffel", 502)

    service = FlightService(DownProvider(), LayeredCache(InProcessCache(), None))

    result = await service.search_flights(
        SearchFlightsInput(origins=["SFO"], destinations=["NRT"], departure_date=DEPART)
    )

    assert result.error.code == "provider_unavailable"
    assert result.error.retryable is True


async def test_one_failing_route_does_not_sink_the_others():
    from app.models.flight import SearchFlightsInput
    from app.providers.base import ProviderUnavailable
    from app.services.cache import InProcessCache, LayeredCache
    from app.services.flight_service import FlightService

    class PartlyDown:
        live_mode = True

        async def search_offers(self, *, origin, destination, spec):
            if origin == "OAK":
                raise ProviderUnavailable("route unavailable", "duffel", 503)
            return [SFO_NONSTOP]

    service = FlightService(PartlyDown(), LayeredCache(InProcessCache(), None))

    result = await service.search_flights(
        SearchFlightsInput(origins=["SFO", "OAK"], destinations=["NRT"], departure_date=DEPART)
    )

    assert result.ok
    assert result.results
    assert any("OAK-NRT search failed" in warning for warning in result.warnings)


# --- expiry ------------------------------------------------------------------


def test_an_expired_offer_knows_it_is_expired():
    stale = offer(
        "off_old", "SFO", price=600.0, stops=0, minutes=655, expires_in=-timedelta(minutes=5)
    )

    assert stale.is_expired() is True
    assert SFO_NONSTOP.is_expired() is False


def test_an_offer_without_an_expiry_is_not_treated_as_expired():
    forever = offer("off_forever", "SFO", price=600.0, stops=0, minutes=655, expires_in=None)

    assert forever.is_expired() is False
