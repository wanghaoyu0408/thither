"""Parking, and the one thing a gap in the data may never become.

`unknown` and `unavailable` are different facts. Nobody having checked is not
the same as somewhere having nowhere to park, and only the second may stop a
plan. Getting that backwards would quietly delete real beaches from real trips
on the strength of a field Google happened not to publish - the same failure as
reading `servesVegetarianFood: false` as a denial.
"""

from datetime import date, datetime

from app.models.arrival import ArrivalContext, ParkingContext, ParkingSpot
from app.models.decision import Decision, PlaceOption
from app.models.entity import PlaceEntity
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.place import PlaceSummary
from app.models.route import RouteLeg
from app.models.tool import ToolError, ToolResult
from app.models.trip import TripState
from app.services.parking_service import ParkingService
from app.services.validation_service import validate_itinerary

DAY = date(2026, 8, 10)


def place(entity_id: str, name: str, *, lat=20.63, lng=-156.44, parking=None) -> PlaceEntity:
    return PlaceEntity(
        entity_id=entity_id,
        name=name,
        categories=["beach"],
        lat=lat,
        lng=lng,
        parking_options=parking,
    )


class FakePlaces:
    def __init__(self, results=None, error: str | None = None):
        self._results = results or []
        self._error = error
        self.searches = 0

    async def search_places(self, spec):
        self.searches += 1
        if self._error:
            return ToolResult[PlaceSummary](
                source="google_places",
                error=ToolError(code="provider_unavailable", message=self._error),
            )
        return ToolResult[PlaceSummary](source="google_places", results=self._results)


class FakeRoutes:
    def __init__(self, minutes: list[float] | None = None, ok: bool = True):
        self._minutes = minutes or []
        self._ok = ok

    async def get_routes(self, spec, **kwargs):
        if not self._ok:
            return ToolResult[RouteLeg](
                source="google_routes",
                error=ToolError(code="provider_unavailable", message="down"),
            )
        return ToolResult[RouteLeg](
            source="google_routes",
            results=[
                RouteLeg(
                    origin_index=index,
                    destination_index=0,
                    mode="walking",
                    duration_seconds=int(value * 60),
                    distance_meters=int(value * 80),
                    status="ok",
                )
                for index, value in enumerate(self._minutes)
            ],
        )


def lot(name: str, lat: float = 20.632, lng: float = -156.442) -> PlaceSummary:
    return PlaceSummary(place_id=f"p_{name}", name=name, lat=lat, lng=lng, categories=["parking"])


def state_with(entity: PlaceEntity) -> TripState:
    state = TripState.new(title="Maui")
    state.brief.scope.rental_car = "already_arranged"
    state.entities[entity.entity_id] = entity
    return state


# --- the invariant ------------------------------------------------------------


async def test_nothing_found_leaves_it_unknown_never_unavailable():
    """The whole point. Google publishing no parking field is Google saying
    nothing, and a beach with nothing said about it is not a beach with no
    parking."""
    entity = place("ent_beach", "Makena Beach")
    service = ParkingService(FakePlaces(results=[]), FakeRoutes())

    context = await service.context_for(state_with(entity), entity)

    assert context.parking.availability == "unknown"
    assert context.parking.availability != "unavailable"
    assert context.parking.spots == []
    assert "no car park found" in context.parking.notes


async def test_a_failed_lookup_is_also_unknown_and_says_which_it_was():
    entity = place("ent_beach", "Makena Beach")
    service = ParkingService(FakePlaces(error="quota exceeded"), FakeRoutes())

    context = await service.context_for(state_with(entity), entity)

    assert context.parking.availability == "unknown"
    # "we looked and found nothing" and "we could not look" are different facts.
    assert "search failed" in context.parking.notes
    assert "quota exceeded" in context.parking.notes


# --- what evidence does move -------------------------------------------------


async def test_google_saying_there_is_a_car_park_confirms_it():
    entity = place("ent_museum", "Maui Ocean Center", parking={"freeParkingLot": True})
    places = FakePlaces()
    service = ParkingService(places, FakeRoutes())

    context = await service.context_for(state_with(entity), entity)

    assert context.parking.availability == "confirmed"
    assert context.parking.spots[0].cost == "free"
    assert context.parking.spots[0].kind == "parking_lot"
    assert places.searches == 0, "no need to look nearby when the place itself says so"


async def test_a_car_park_nearby_is_likely_rather_than_confirmed():
    entity = place("ent_beach", "Makena Beach")
    service = ParkingService(FakePlaces(results=[lot("Beach lot")]), FakeRoutes(minutes=[3.0]))

    context = await service.context_for(state_with(entity), entity)

    assert context.parking.availability == "likely", "nearby is weaker than the place saying so"
    assert context.parking.spots[0].cost == "unknown", "a lot with no published price is not free"


async def test_an_absent_google_key_is_not_read_as_a_negative():
    """`freeParkingLot` missing does not mean paid, and does not mean none."""
    entity = place("ent_x", "Somewhere", parking={"paidParkingLot": True})
    service = ParkingService(FakePlaces(), FakeRoutes())

    context = await service.context_for(state_with(entity), entity)

    assert [spot.cost for spot in context.parking.spots] == ["paid"]


# --- the walk -----------------------------------------------------------------


async def test_the_walk_from_the_car_park_is_measured_not_guessed():
    entity = place("ent_beach", "Makena Beach")
    service = ParkingService(
        FakePlaces(results=[lot("Far lot"), lot("Near lot")]), FakeRoutes(minutes=[12.0, 3.0])
    )

    context = await service.context_for(state_with(entity), entity)

    assert [spot.walking_minutes for spot in context.parking.spots] == [12.0, 3.0]
    # The one to plan around is the shortest measured walk.
    assert context.parking.best.name == "Near lot"
    assert context.overhead_minutes == 3.0


async def test_an_unmeasured_walk_never_wins_by_looking_like_zero():
    entity = place("ent_beach", "Makena Beach")
    service = ParkingService(FakePlaces(results=[lot("A"), lot("B")]), FakeRoutes(ok=False))

    context = await service.context_for(state_with(entity), entity)

    assert all(spot.walking_minutes is None for spot in context.parking.spots)
    assert context.overhead_minutes is None, "no measurement is not a short walk"


async def test_a_car_park_too_far_to_walk_is_not_this_places_parking():
    entity = place("ent_beach", "Makena Beach")
    service = ParkingService(FakePlaces(results=[lot("Miles away")]), FakeRoutes(minutes=[45.0]))

    context = await service.context_for(state_with(entity), entity)

    assert context.parking.spots == []
    assert context.parking.availability == "unknown"
    # "Nothing found" and "everything found was too far" are different answers,
    # and an unexplained `unknown` is the least useful thing this can return.
    assert "45 min on foot" in context.parking.notes


# --- feasibility --------------------------------------------------------------


def two_stop_day(overhead: float | None, gap_minutes: int, availability="confirmed") -> TripState:
    state = TripState.new(title="Maui")
    state.brief.scope.rental_car = "already_arranged"
    state.entities["ent_a"] = place("ent_a", "First", lat=20.60, lng=-156.40)
    state.entities["ent_b"] = place("ent_b", "Second", lat=20.90, lng=-156.50)

    start = datetime(2026, 8, 10, 10, 0)
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=DAY,
                items=[
                    ItineraryItem(
                        item_id="item_a", type="activity", entity_id="ent_a", title="First",
                        start_at=start, end_at=start.replace(hour=11),
                    ),
                    ItineraryItem(
                        item_id="item_b", type="activity", entity_id="ent_b", title="Second",
                        start_at=start.replace(hour=11) + _minutes(gap_minutes),
                        end_at=start.replace(hour=13),
                    ),
                ],
            )
        ]
    )
    spots = (
        [ParkingSpot(kind="parking_lot", name="Lot", walking_minutes=overhead)]
        if overhead is not None
        else []
    )
    state.arrival["ent_b"] = ArrivalContext(
        entity_id="ent_b",
        mode="driving",
        parking=ParkingContext(availability=availability, spots=spots),
    )
    return state


def _minutes(count: int):
    from datetime import timedelta

    return timedelta(minutes=count)


def travel(minutes: float):
    from app.services.validation_service import TravelLookup

    return TravelLookup(minutes={("ent_a", "ent_b", "driving"): minutes})


def types_of(state, lookup):
    return [issue.type for issue in validate_itinerary(state, travel=lookup).issues]


def test_the_walk_from_the_car_park_can_make_a_day_infeasible():
    """Activity at 10:00, arrival 09:55, twelve minutes from the car park."""
    state = two_stop_day(overhead=12.0, gap_minutes=20)

    issues = validate_itinerary(state, travel=travel(18.0)).issues
    access = next(i for i in issues if i.type == "parking_access_time")

    assert access.severity == "error"
    assert "12 min from the car park" in access.message


def test_enough_time_for_the_drive_and_the_walk_is_fine():
    state = two_stop_day(overhead=12.0, gap_minutes=45)

    assert "parking_access_time" not in types_of(state, travel(18.0))


def test_unverified_parking_warns_and_never_removes_the_stop():
    state = two_stop_day(overhead=None, gap_minutes=60, availability="unknown")

    kinds = types_of(state, travel(18.0))
    assert "parking_unverified" in kinds
    issue = next(
        i for i in validate_itinerary(state, travel=travel(18.0)).issues
        if i.type == "parking_unverified"
    )
    assert issue.severity == "warning", "nobody having checked is not a reason to drop a place"
    assert len(state.itinerary.days[0].items) == 2


def test_confirmed_unavailable_parking_is_an_error_for_a_driving_trip():
    state = two_stop_day(overhead=None, gap_minutes=60, availability="unavailable")

    issue = next(
        i for i in validate_itinerary(state, travel=travel(18.0)).issues
        if i.type == "parking_unavailable"
    )
    assert issue.severity == "error"


def test_a_trip_without_a_car_is_not_warned_about_parking():
    state = two_stop_day(overhead=None, gap_minutes=60, availability="unknown")
    state.brief.scope.rental_car = "not_needed"

    assert "parking_unverified" not in types_of(state, travel(18.0))


def test_permits_and_reservations_are_surfaced_separately():
    state = two_stop_day(overhead=2.0, gap_minutes=60)
    state.arrival["ent_b"].parking.permit_required = True
    state.arrival["ent_b"].parking.reservation_required = True

    kinds = types_of(state, travel(18.0))
    assert "parking_permit_required" in kinds
    assert "parking_reservation_required" in kinds


# --- parking and weather move the replacement --------------------------------


def swap_trip(*, rain: float | None = None):
    """One scheduled stop and two alternatives: one easy, one hard to park at."""
    from app.models.weather import WeatherContext

    state = TripState.new(title="Maui")
    state.brief.scope.rental_car = "already_arranged"
    state.entities["ent_now"] = place("ent_now", "Current stop")
    state.entities["ent_easy"] = place("ent_easy", "Easy parking", lat=20.64, lng=-156.45)
    state.entities["ent_hard"] = place("ent_hard", "Hard parking", lat=20.65, lng=-156.46)
    # The hard one is better rated, so only parking can move the choice.
    state.entities["ent_easy"].rating = 4.1
    state.entities["ent_hard"].rating = 4.9

    weather = (
        WeatherContext(date=DAY, kind="forecast", precipitation_probability=rain)
        if rain is not None
        else None
    )
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=DAY,
                weather=weather,
                items=[
                    ItineraryItem(
                        item_id="item_now", type="activity", entity_id="ent_now",
                        title="Current stop",
                        start_at=datetime(2026, 8, 10, 11, 0),
                        end_at=datetime(2026, 8, 10, 12, 30),
                    )
                ],
            )
        ]
    )
    for entity_id, minutes in (("ent_easy", 3.0), ("ent_hard", 19.0)):
        state.arrival[entity_id] = ArrivalContext(
            entity_id=entity_id,
            mode="driving",
            parking=ParkingContext(
                availability="confirmed",
                spots=[ParkingSpot(kind="parking_lot", name="Lot", walking_minutes=minutes)],
            ),
        )
    return state


def test_easier_parking_beats_a_better_rating():
    """"Somewhere easier" has to mean something, and this is what it means."""
    from app.services.itinerary_service import substitute_item

    proposal = substitute_item(swap_trip(), "item_now")

    assert proposal.days[0].items[0].title == "Easy parking"
    assert "3 min from the car park" in proposal.summary


def test_parking_only_nudges_and_never_filters():
    """An unchecked place is still a real place. It loses; it is not removed."""
    from app.services.itinerary_service import arrival_penalty

    state = swap_trip()
    state.arrival.clear()

    assert arrival_penalty(state, state.entities["ent_easy"]) == 1.0
    state.arrival["ent_easy"] = ArrivalContext(
        entity_id="ent_easy", parking=ParkingContext(availability="unavailable")
    )
    # Worse than unchecked, and still only a number in a sort key.
    assert arrival_penalty(state, state.entities["ent_easy"]) == 3.0


def test_a_wet_forecast_pushes_the_choice_indoors():
    """Isolated to weather: both alternatives park equally well, so only the
    forecast can separate them."""
    from app.services.itinerary_service import substitute_item, weather_penalty

    state = swap_trip(rain=0.8)
    indoor = state.entities["ent_hard"]
    indoor.categories = ["museum"]
    state.arrival["ent_hard"].parking.spots[0].walking_minutes = 3.0

    assert weather_penalty(state, state.entities["ent_easy"], state.itinerary.days[0]) == 1.5
    assert weather_penalty(state, indoor, state.itinerary.days[0]) == 0.0

    proposal = substitute_item(state, "item_now")
    assert proposal.days[0].items[0].title == "Hard parking", "indoors wins a wet day"
    assert "rain forecast at 80%" in proposal.summary


def test_a_long_walk_in_the_rain_is_weighed_against_being_indoors():
    """Neither factor overrides the other. Walking nineteen minutes through a
    downpour to reach a museum is not obviously better than three minutes to a
    beach, and the ranking treats it as the close call it is."""
    from app.services.itinerary_service import _ease

    state = swap_trip(rain=0.8)
    state.entities["ent_hard"].categories = ["museum"]
    day = state.itinerary.days[0]

    outdoor_close = _ease(state, state.entities["ent_easy"], day)
    indoor_far = _ease(state, state.entities["ent_hard"], day)

    assert abs(outdoor_close - indoor_far) < 0.5, "the two should be close, not lopsided"


def test_a_seasonal_norm_never_moves_a_specific_days_choice():
    """The same rule the validator holds: a norm informs, it does not decide."""
    from app.models.weather import WeatherContext
    from app.services.itinerary_service import weather_penalty

    state = swap_trip()
    state.itinerary.days[0].weather = WeatherContext(
        date=DAY, kind="historical_norm", precipitation_day_frequency=0.9
    )

    assert weather_penalty(state, state.entities["ent_easy"], state.itinerary.days[0]) == 0.0


def test_a_trip_without_a_car_is_not_reordered_by_parking():
    from app.services.itinerary_service import arrival_penalty

    state = swap_trip()
    state.brief.scope.rental_car = "not_needed"

    assert arrival_penalty(state, state.entities["ent_hard"]) == 0.0


def test_the_model_can_see_parking_and_weather_at_all():
    """It could not. Neither appeared in the state projection, so the agent had
    to take "this place has bad parking" on trust about a fact the trip held."""
    from app.agent.context import summarize

    summary = summarize(swap_trip(rain=0.8))
    day = summary["itinerary"][0]

    assert day["weather"]["kind"] == "forecast"
    assert day["weather"]["is_forecast"] is True
    scheduled = day["items"][0]
    # The scheduled stop has no arrival record, and says so rather than lying.
    assert scheduled["arrival"] is None


def test_a_scheduled_stop_reports_its_parking_to_the_model():
    from app.agent.context import summarize

    state = swap_trip()
    state.arrival["ent_now"] = ArrivalContext(
        entity_id="ent_now",
        parking=ParkingContext(
            availability="unknown", spots=[], permit_required=None
        ),
    )

    arrival = summarize(state)["itinerary"][0]["items"][0]["arrival"]
    assert arrival["parking"] == "unknown"
    assert arrival["walk_minutes"] is None


async def test_a_replacement_records_why_it_was_chosen():
    """A swap made for a measured two-minute walk over a sixteen-minute one
    reported "no stored decision recommended this place" - the provenance gap
    this project keeps finding somewhere new."""
    from app.agent.tool_registry import ToolContext, _replace_item
    from app.config import Settings
    from app.services.explanation_service import explain
    from app.services.proposal_store import ProposalStore

    state = swap_trip()
    context = ToolContext(
        state=state, toolbox=None, proposals=ProposalStore(), settings=Settings()
    )

    reply = await _replace_item(context, {"item_id": "item_now"})

    assert "proposal_id" in reply, reply
    assert context.pending_decisions, "the reasoning must survive the turn"

    # Apply what was staged, then ask why - the question a traveller asks next.
    for name, value in context.pending_decisions.items():
        _, _, key = name.partition(".")
        state.decisions.place_shortlists[key] = Decision[PlaceOption].model_validate(value)

    explanation = explain(state, "ent_easy")
    assert explanation.complete is True
    assert any("car park" in reason for reason in explanation.pros)


def test_a_trip_with_no_arrival_records_produces_no_parking_issues():
    state = two_stop_day(overhead=None, gap_minutes=60)
    state.arrival.clear()

    assert not [kind for kind in types_of(state, travel(18.0)) if kind.startswith("parking_")]
