"""The Places attestations the group layer rests on, opt-in.

    .\\.venv\\Scripts\\python.exe -m pytest -m live --override-ini addopts=

One contract, checked against real data: `servesVegetarianFood` and
`accessibilityOptions` arrive on a FULL details fetch, and `True` is a positive
attestation rather than a default.

That asymmetry is load-bearing. The whole dietary rule - raise a question, never
filter - exists because Google says a place *does* serve vegetarian food and
never says one does not. If that ever became a reliable two-way signal, the
conflict service should be reconsidered, and this test is what would notice.
"""

import pytest

from app.config import get_settings
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, SearchPlacesInput
from app.services.toolbox import Toolbox

settings = get_settings()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.google_maps_api_key, reason="needs GOOGLE_MAPS_API_KEY"),
]


@pytest.fixture
async def toolbox():
    async with Toolbox(settings) as box:
        yield box


async def details_for(toolbox, query: str, limit: int = 6):
    found = await toolbox.places.search_places(
        SearchPlacesInput(query=query, lat=0.0, lng=0.0, limit=limit)
    )
    assert found.ok, found.error
    if found.found_nothing:
        pytest.skip(f"nothing matched {query!r} today")

    result = await toolbox.places.get_place_details(
        GetPlaceDetailsInput(
            place_ids=[place.place_id for place in found.results],
            field_set=PlaceFieldSet.FULL,
        )
    )
    assert result.ok, result.error
    return result.results


async def test_a_full_fetch_carries_the_dietary_attestation(toolbox):
    places = await details_for(toolbox, "vegetarian restaurant in Shibuya Tokyo")

    confirmed = [place for place in places if place.serves_vegetarian == "confirmed_true"]
    assert confirmed, "no vegetarian restaurant came back attested; the field mask may be wrong"
    # Google can only assert the positive case, so nothing it returns may map
    # to confirmed_false. If one appears, the provider mapping has changed and
    # the conflict service would start treating silence as a denial.
    assert all(place.serves_vegetarian in ("confirmed_true", "unknown") for place in places)


async def test_silence_is_not_a_denial(toolbox):
    """The single most important fact about this field.

    Every pizzeria on earth serves a margherita. Google attests it for roughly
    one in eight of them - measured live: 1/8 in Shibuya against 8/8 for Indian
    restaurants in Shinjuku. So an unattested pizzeria is unattested, not
    meatless-free, and a dietary conflict must raise a question rather than
    filter. If Google ever attested all of them, that reasoning would need
    revisiting, and this is what would notice.
    """
    places = await details_for(toolbox, "pizza in Shibuya Tokyo", limit=8)

    assert places
    unattested = [place for place in places if place.serves_vegetarian != "confirmed_true"]
    assert unattested, (
        "Google now attests vegetarian food at every pizzeria; revisit whether a dietary "
        "conflict should still raise a question rather than filter"
    )


async def test_the_attestation_is_a_full_tier_field_only(toolbox):
    """Cost discipline: it rides on the details call the planner already makes.

    A RANKING-tier search does not request it, so every search result is
    unknown on diet - which is correct, and why the check happens on the
    shortlist rather than on twenty candidates.
    """
    found = await toolbox.places.search_places(
        SearchPlacesInput(query="Indian restaurant in Shinjuku Tokyo", lat=0.0, lng=0.0, limit=6)
    )
    assert found.ok, found.error
    if found.found_nothing:
        pytest.skip("nothing matched today")

    assert all(place.serves_vegetarian == "unknown" for place in found.results)

    detailed = await details_for(toolbox, "Indian restaurant in Shinjuku Tokyo", limit=6)
    assert any(place.serves_vegetarian == "confirmed_true" for place in detailed)


async def test_accessibility_keeps_only_what_was_attested(toolbox):
    places = await details_for(toolbox, "department store in Shinjuku Tokyo")

    attested = [place for place in places if place.accessibility]
    assert attested, "no accessibility attestation at all; the field mask may be wrong"
    # Only positive attestations survive normalization, for the same reason.
    assert all(
        value == "confirmed_true" for place in places for value in place.accessibility.values()
    )


async def test_the_attestation_survives_into_a_registry_entity(toolbox):
    from app.services.entity_service import resolve_places

    places = await details_for(toolbox, "vegetarian restaurant in Shibuya Tokyo", limit=4)
    entities = resolve_places(places, {})

    assert any(entity.serves_vegetarian == "confirmed_true" for entity in entities)
    assert all(entity.serves_vegetarian in ("confirmed_true", "unknown") for entity in entities)


async def test_a_cheaper_search_does_not_wipe_a_details_attestation(toolbox):
    """RANKING-tier searches carry no dietary fields; a later one must not erase one."""
    from app.services.entity_service import resolve_places

    places = await details_for(toolbox, "vegetarian restaurant in Shibuya Tokyo", limit=4)
    entities = resolve_places(places, {})
    known = {entity.entity_id: entity for entity in entities}
    attested = next(
        (entity for entity in entities if entity.serves_vegetarian == "confirmed_true"), None
    )
    if attested is None:
        pytest.skip("nothing was attested today")

    plain = await toolbox.places.search_places(
        SearchPlacesInput(query=attested.name, lat=0.0, lng=0.0, limit=1)
    )
    assert plain.ok, plain.error
    if plain.found_nothing:
        pytest.skip("the place could not be found again by name")

    refreshed = resolve_places(plain.results, known)

    same = [entity for entity in refreshed if entity.entity_id == attested.entity_id]
    if same:
        assert same[0].serves_vegetarian == "confirmed_true"
