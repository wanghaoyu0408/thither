"""Hotels against live providers, opt-in.

    .\\.venv\\Scripts\\python.exe -m pytest -m live --override-ini addopts=

Two halves, with different requirements and different confidence:

**The area decision needs only Google**, so it is verified for real: candidate
neighbourhoods around a trip's actual anchor places, ranked by travel times the
Routes API measured. This is the half spec section 25 puts first, and it is
fully live.

**The hotel search needs SerpApi**, and is a contract acceptance: the request is
accepted, properties parse, both kinds of rating and the per-vendor quotes come
through. Nothing here asserts a price is good - that is a judgement about
Tokyo's hotel market, not about this code.
"""

from datetime import date, timedelta

import pytest

from app.config import get_settings
from app.models.entity import PlaceEntity
from app.models.hotel import SearchHotelsInput
from app.models.trip import DestinationSpec, TripBrief, TripDates, TripState
from app.services.toolbox import Toolbox

settings = get_settings()

pytestmark = pytest.mark.live

CHECK_IN = date.today() + timedelta(days=45)
CHECK_OUT = CHECK_IN + timedelta(days=4)

TOKYO_ANCHORS = [
    ("Senso-ji", 35.7148, 139.7967),
    ("Tokyo Skytree", 35.7101, 139.8107),
    ("Ueno Park", 35.7148, 139.7737),
    ("Meiji Jingu", 35.6764, 139.6993),
]


@pytest.fixture
async def toolbox():
    async with Toolbox(settings) as box:
        yield box


def tokyo_trip() -> TripState:
    state = TripState.new(title="live hotel area check")
    state.brief = TripBrief(
        destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
        timezone="Asia/Tokyo",
        dates=TripDates(start=CHECK_IN, end=CHECK_OUT),
    )
    state.entities = {
        f"ent_{index}": PlaceEntity(
            entity_id=f"ent_{index}", name=name, lat=lat, lng=lng, categories=["tourist_attraction"]
        )
        for index, (name, lat, lng) in enumerate(TOKYO_ANCHORS)
    }
    return state


# --- the area half: fully live -----------------------------------------------


@pytest.mark.skipif(not settings.google_maps_api_key, reason="needs GOOGLE_MAPS_API_KEY")
async def test_real_neighbourhoods_are_ranked_by_real_travel_times(toolbox):
    result = await toolbox.hotel_areas.recommend_areas(
        tokyo_trip(),
        suggested_areas=["Asakusa", "Shinjuku", "Ginza"],
        # No research: this asserts the routing half, and an LLM call would make
        # the outcome depend on what the web said today.
        with_research=False,
    )

    assert result.ok, result.error
    assert result.results, "no area came back"

    for area in result.results:
        if area.mean_minutes is None:
            continue
        # Sanity, not judgement: anywhere inside Tokyo is minutes not hours.
        assert 0 < area.mean_minutes < 180, area
        assert area.worst_minutes >= area.mean_minutes
        assert "mean_travel" in area.score.dimensions

    best = result.results[0]
    assert best.minutes_by_anchor, "the winner has no measured travel time"


@pytest.mark.skipif(not settings.google_maps_api_key, reason="needs GOOGLE_MAPS_API_KEY")
async def test_an_area_far_from_the_trip_ranks_below_one_beside_it(toolbox):
    """Every anchor above is in Tokyo; Yokohama is a different city.

    A real assertion about real geography: whatever the timetable says today,
    staying an hour down the coast is worse than staying next to the trip.
    """
    result = await toolbox.hotel_areas.recommend_areas(
        tokyo_trip(), suggested_areas=["Asakusa", "Yokohama"], with_research=False
    )

    assert result.ok, result.error
    ranked = {area.candidate.area_name: area for area in result.results}
    if "Yokohama" not in ranked or "Asakusa" not in ranked:
        pytest.skip("one of the two areas could not be geocoded today")

    assert ranked["Asakusa"].score.total > ranked["Yokohama"].score.total
    assert ranked["Asakusa"].mean_minutes < ranked["Yokohama"].mean_minutes


# --- the hotel half: contract only -------------------------------------------


@pytest.mark.skipif(not settings.serpapi_api_key, reason="needs SERPAPI_API_KEY")
async def test_the_hotel_search_contract_holds(toolbox):
    result = await toolbox.hotels.search_hotels(
        SearchHotelsInput(
            city="Tokyo",
            area_name="Asakusa",
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            adults=2,
            limit=10,
        ),
        state=tokyo_trip(),
    )

    assert result.ok, result.error
    if result.found_nothing:
        pytest.skip("the provider returned no properties for these dates today")

    for option in result.results:
        assert option.name
        assert option.provider == "serpapi_google_hotels"
        assert option.live_mode is True
        # Whatever else is missing, a rating must say what it measures and who
        # said it - that is the whole point of the model.
        for rating in option.ratings:
            assert rating.type in ("star_category", "user_rating", "location_score")
            assert rating.source in ("google_hotels", "google_places", "other")
        for quote in option.quotes:
            assert quote.source

    # Across ten Tokyo hotels, at least one should carry each kind of rating.
    assert any(option.star_category for option in result.results)
    assert any(option.user_rating for option in result.results)


@pytest.mark.skipif(not settings.serpapi_api_key, reason="needs SERPAPI_API_KEY")
async def test_the_shortlist_measures_real_travel_times_to_the_trips_places(toolbox):
    state = tokyo_trip()
    search = await toolbox.hotels.search_hotels(
        SearchHotelsInput(
            city="Tokyo", area_name="Asakusa", check_in=CHECK_IN, check_out=CHECK_OUT, limit=10
        ),
        state=state,
    )
    if not search.ok or search.found_nothing:
        pytest.skip("no properties to shortlist today")

    shortlist = await toolbox.hotels.shortlist(search.results, state=state, size=3)

    assert shortlist.ranked
    measured = [item for item in shortlist.ranked if item.option.mean_route_minutes() is not None]
    assert measured, "no shortlisted hotel was routed to the trip's places"
    for item in measured:
        assert 0 < item.option.mean_route_minutes() < 180
        assert "location" in item.score.dimensions
