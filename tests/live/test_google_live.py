"""Contract tests against the real Google APIs.

Opt-in and deliberately tiny - enough to catch a changed field mask or response
shape, not enough to burn quota:

    .\\.venv\\Scripts\\python.exe -m pytest -m live --override-ini addopts=

Skipped unless GOOGLE_MAPS_API_KEY is set. These assert on *shape*, never on
particular ratings or durations, which change without warning.
"""

import pytest

from app.config import get_settings
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, SearchPlacesInput
from app.models.route import GetRoutesInput, LocationRef
from app.services.toolbox import Toolbox

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not get_settings().google_maps_api_key,
        reason="GOOGLE_MAPS_API_KEY is not set",
    ),
]

SHIBUYA = {"lat": 35.6595, "lng": 139.7005}


@pytest.fixture
async def toolbox():
    async with Toolbox() as box:
        yield box


async def test_text_search_returns_real_places(toolbox):
    result = await toolbox.places.search_places(
        SearchPlacesInput(query="ramen", **SHIBUYA, radius_meters=1200, limit=10)
    )

    assert result.ok, result.error
    assert result.results, "Shibuya should have ramen"

    first = result.results[0]
    assert first.place_id
    assert first.name
    assert first.lat is not None and first.lng is not None
    # The RANKING tier must actually deliver what ranking needs.
    assert any(place.rating is not None for place in result.results)
    assert any(place.rating_count is not None for place in result.results)


async def test_details_add_the_enterprise_fields(toolbox):
    """Opening hours are the reason to spend a details call at all.

    Asserted across a batch rather than on one place: plenty of small venues
    publish no hours, so a single miss says nothing about the field mask - but
    a whole batch of Shibuya cafes with none would.
    """
    found = await toolbox.places.search_places(
        SearchPlacesInput(query="coffee", **SHIBUYA, radius_meters=1200, limit=5)
    )
    assert found.ok and found.results

    details = await toolbox.places.get_place_details(
        GetPlaceDetailsInput(
            place_ids=[place.place_id for place in found.results[:5]],
            field_set=PlaceFieldSet.FULL,
        )
    )

    assert details.ok, details.error
    assert len(details.results) == len(found.results[:5])
    assert {p.place_id for p in details.results} == {p.place_id for p in found.results[:5]}

    assert any(place.opening_hours is not None for place in details.results), (
        "no place in the batch returned regularOpeningHours - the FULL field mask "
        "is probably not being honoured"
    )
    # Enterprise-tier ranking fields must survive the details path too.
    assert any(place.rating is not None for place in details.results)


async def test_walking_matrix(toolbox):
    result = await toolbox.routes.get_routes(
        GetRoutesInput(
            origins=[LocationRef(lat=35.6595, lng=139.7005, label="Shibuya")],
            destinations=[
                LocationRef(lat=35.6659, lng=139.6979, label="Tomigaya"),
                LocationRef(lat=35.6717, lng=139.7031, label="Harajuku"),
            ],
            mode="walking",
        )
    )

    assert result.ok, result.error
    assert len(result.results) == 2
    assert all(leg.duration_seconds is not None for leg in result.results if leg.status == "ok")


async def test_transit_matrix_over_the_cap_is_split_for_real(toolbox):
    """11x11 transit is 121 elements - one call would be rejected."""
    points = [
        LocationRef(lat=35.6595 + index * 0.004, lng=139.7005 + index * 0.004, label=f"p{index}")
        for index in range(11)
    ]

    result = await toolbox.routes.get_routes(
        GetRoutesInput(origins=points, destinations=points, mode="transit")
    )

    assert result.ok, result.error
    assert len(result.results) == 121
    assert any("exceeds the 100-element transit cap" in w for w in result.warnings)


async def test_a_nonsense_place_id_is_an_error_not_a_guess(toolbox):
    result = await toolbox.places.get_place_details(
        GetPlaceDetailsInput(place_ids=["ChIJ_definitely_not_a_place_id"])
    )

    assert result.ok is False
    assert result.error.code in ("invalid_request", "provider_unavailable")
    assert result.results == []
