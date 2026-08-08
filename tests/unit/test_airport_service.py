"""Airport lookup and the drive times that make comparing airports meaningful."""

import pytest

from app.models.flight import SearchAirportsInput
from app.models.place import PlaceSummary
from app.models.route import RouteLeg
from app.services.airport_service import AirportService, find_by_iata, load_airports, nearby
from app.services.cache import InProcessCache, LayeredCache
from app.services.place_service import PlaceService

# Downtown San Francisco.
SF = (37.7749, -122.4194)


class FakePlacesProvider:
    def __init__(self, lat=SF[0], lng=SF[1], found=True):
        self.lat, self.lng, self.found = lat, lng, found

    async def search_text(self, **kwargs):
        if not self.found:
            return []
        return [PlaceSummary(place_id="p1", name="San Francisco", lat=self.lat, lng=self.lng)]

    async def get_details(self, place_id, **kwargs):
        return PlaceSummary(place_id=place_id, name="x")


class FakeRoutesProvider:
    """Returns the drive times the Bay Area actually has, roughly."""

    def __init__(self, minutes: list[float] | None = None, fail: bool = False):
        self.minutes = minutes
        self.fail = fail

    async def compute_route_matrix(self, origins, destinations, *, mode, departure_at=None):
        if self.fail:
            from app.providers.base import ProviderUnavailable

            raise ProviderUnavailable("routes down", "google_routes")
        return [
            RouteLeg(
                origin_index=0,
                destination_index=index,
                mode=mode,
                duration_seconds=int((self.minutes or [20, 35, 60])[index % 3] * 60),
                distance_meters=30_000,
            )
            for index in range(len(destinations))
        ]


def build(places_provider=None, routes_provider=None) -> AirportService:
    from app.services.route_service import RouteService

    cache = LayeredCache(InProcessCache(), None)
    return AirportService(
        PlaceService(places_provider or FakePlacesProvider(), cache),
        RouteService(routes_provider or FakeRoutesProvider(), cache),
    )


# --- the dataset -------------------------------------------------------------


def test_the_dataset_loads():
    airports = load_airports()

    assert len(airports) > 50_000, "expected the full OurAirports dataset"


def test_the_bay_area_trio_is_present():
    for code in ("SFO", "SJC", "OAK"):
        record = find_by_iata(code)
        assert record is not None, code
        assert record.serves_passengers


def test_lookup_is_case_insensitive():
    assert find_by_iata("sfo") is find_by_iata("SFO")


def test_an_unknown_code_is_none():
    assert find_by_iata("ZZZZ") is None


# --- nearby ------------------------------------------------------------------


def test_the_bay_area_trio_is_found_near_san_francisco():
    codes = {record.iata for record in nearby(*SF, radius_km=120, limit=12)}

    assert {"SFO", "OAK", "SJC"} <= codes


def test_only_airports_with_scheduled_service_are_returned():
    for record in nearby(*SF, radius_km=120, limit=20):
        assert record.iata
        assert record.scheduled


def test_general_aviation_fields_are_left_out_when_real_airports_exist():
    """Buchanan Field and San Carlos are flagged as scheduled in the source data
    and turned up beside SFO. No airline serves either."""
    codes = {record.iata for record in nearby(*SF, radius_km=120, limit=20)}

    assert {"SFO", "OAK", "SJC"} <= codes
    assert "CCR" not in codes
    assert "SQL" not in codes


def test_secondary_fields_can_be_asked_for_explicitly():
    default = {r.iata for r in nearby(*SF, radius_km=120, limit=30)}
    widened = {r.iata for r in nearby(*SF, radius_km=120, limit=30, include_secondary=True)}

    assert default < widened


def test_a_region_with_no_major_airport_still_gets_its_medium_one():
    """The rule is comparative, so somewhere served only by a medium field is
    not told there are no airports."""
    from app.services.airport_service import find_by_iata

    # Nauru International: the country's only airport, and a medium one.
    nauru = find_by_iata("INU")
    assert nauru is not None and nauru.kind == "medium_airport"

    found = nearby(nauru.lat, nauru.lng, radius_km=150, limit=5)

    assert "INU" in {record.iata for record in found}


def test_a_tight_radius_excludes_the_further_airports():
    codes = {record.iata for record in nearby(*SF, radius_km=20, limit=12)}

    assert "SFO" in codes
    assert "SJC" not in codes


def test_nowhere_near_an_airport_returns_nothing():
    # Middle of the South Pacific.
    assert nearby(-40.0, -140.0, radius_km=50, limit=5) == []


# --- the service -------------------------------------------------------------


async def test_search_returns_airports_with_real_drive_times():
    service = build(routes_provider=FakeRoutesProvider([18, 34, 61]))

    result = await service.search_airports(SearchAirportsInput(location="San Francisco", limit=3))

    assert result.ok
    assert result.results
    for option in result.results:
        assert option.ground_travel_minutes is not None
        assert option.ground_travel_source == "routes_api"


async def test_results_are_sorted_by_drive_time_not_distance():
    service = build(routes_provider=FakeRoutesProvider([60, 20, 35]))

    result = await service.search_airports(SearchAirportsInput(location="San Francisco", limit=3))

    minutes = [option.ground_travel_minutes for option in result.results]
    assert minutes == sorted(minutes)


async def test_a_drive_time_limit_drops_the_far_ones():
    service = build(routes_provider=FakeRoutesProvider([18, 34, 61]))

    result = await service.search_airports(
        SearchAirportsInput(location="San Francisco", limit=3, max_ground_travel_minutes=40)
    )

    assert all(option.ground_travel_minutes <= 40 for option in result.results)
    assert any("beyond 40 minutes" in warning for warning in result.warnings)


async def test_unavailable_drive_times_are_reported_not_estimated():
    """Spec section 21: never invent a travel time to fill the gap."""
    service = build(routes_provider=FakeRoutesProvider(fail=True))

    result = await service.search_airports(SearchAirportsInput(location="San Francisco", limit=3))

    assert result.ok
    assert result.results
    for option in result.results:
        assert option.ground_travel_minutes is None
        assert option.ground_travel_source == "unavailable"
    assert any("unavailable" in warning for warning in result.warnings)


async def test_skipping_ground_travel_leaves_it_unlooked_up():
    service = build()

    result = await service.search_airports(
        SearchAirportsInput(location="San Francisco", limit=3, with_ground_travel=False)
    )

    for option in result.results:
        assert option.ground_travel_source == "not_looked_up"
        assert option.distance_km is not None


async def test_an_unlocatable_place_is_an_error_not_a_guess():
    service = build(places_provider=FakePlacesProvider(found=False))

    result = await service.search_airports(SearchAirportsInput(location="Atlantis"))

    assert result.ok is False
    assert "could not locate" in result.error.message


async def test_straight_line_distance_is_carried_but_never_called_a_drive():
    service = build(routes_provider=FakeRoutesProvider([18, 34, 61]))

    result = await service.search_airports(SearchAirportsInput(location="San Francisco", limit=3))
    option = result.results[0]

    assert option.distance_km is not None
    assert option.distance_km != option.ground_travel_minutes
    assert "drive" in option.describe()


@pytest.mark.parametrize("code", ["SFO", "OAK", "SJC"])
def test_each_bay_area_airport_has_usable_coordinates(code):
    record = find_by_iata(code)

    assert 36.0 < record.lat < 39.0
    assert -123.0 < record.lng < -121.0
