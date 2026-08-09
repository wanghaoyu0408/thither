"""Parking, and the one thing a gap in the data may never become.

`unknown` and `unavailable` are different facts. Nobody having checked is not
the same as somewhere having nowhere to park, and only the second may stop a
plan. Getting that backwards would quietly delete real beaches from real trips
on the strength of a field Google happened not to publish - the same failure as
reading `servesVegetarianFood: false` as a denial.
"""

from datetime import date, datetime

from app.models.arrival import ArrivalContext, ParkingContext, ParkingSpot
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


def test_a_trip_with_no_arrival_records_produces_no_parking_issues():
    state = two_stop_day(overhead=None, gap_minutes=60)
    state.arrival.clear()

    assert not [kind for kind in types_of(state, travel(18.0)) if kind.startswith("parking_")]
