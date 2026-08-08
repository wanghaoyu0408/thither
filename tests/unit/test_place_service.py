"""Place service behaviour, driven by a fake provider (no network)."""

from datetime import timedelta

from app.models.common import utcnow
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, PlaceSummary, SearchPlacesInput
from app.providers.base import ProviderRateLimited, ProviderUnavailable
from app.services.cache import InProcessCache, LayeredCache
from app.services.place_service import PlaceService

SHIBUYA = {"lat": 35.6595, "lng": 139.7005}


class FakePlacesProvider:
    def __init__(self, places=None, raises=None):
        self.places = places or []
        self.raises = raises
        self.search_calls: list[dict] = []
        self.detail_calls: list[str] = []

    async def search_text(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.raises:
            raise self.raises
        return list(self.places)

    async def get_details(self, place_id, *, field_set=PlaceFieldSet.FULL, language=None):
        self.detail_calls.append(place_id)
        if self.raises:
            raise self.raises
        return next(p for p in self.places if p.place_id == place_id)


def summary(place_id="ChIJ_a", **overrides) -> PlaceSummary:
    base = {"place_id": place_id, "name": "Fuglen", "lat": 35.6659, "lng": 139.6979, "rating": 4.3}
    return PlaceSummary(**{**base, **overrides})


def service(provider) -> PlaceService:
    return PlaceService(provider, LayeredCache(InProcessCache(), None))


async def test_search_returns_normalized_results():
    provider = FakePlacesProvider([summary(), summary("ChIJ_b", name="Other")])

    result = await service(provider).search_places(
        SearchPlacesInput(query="ramen", **SHIBUYA, radius_meters=1000)
    )

    assert result.ok
    assert [p.place_id for p in result.results] == ["ChIJ_a", "ChIJ_b"]
    assert result.source == "google_places"
    assert result.expires_at is not None


async def test_a_provider_failure_becomes_a_tool_error_not_an_exception():
    provider = FakePlacesProvider(raises=ProviderUnavailable("boom", "google_places"))

    result = await service(provider).search_places(SearchPlacesInput(query="ramen", **SHIBUYA))

    assert result.ok is False
    assert result.error.code == "provider_unavailable"
    assert result.error.retryable is True
    assert result.results == []


async def test_no_results_is_distinguishable_from_a_failure():
    """Spec section 38: an empty neighbourhood and a dead API must not look alike."""
    empty = await service(FakePlacesProvider([])).search_places(
        SearchPlacesInput(query="ramen", **SHIBUYA)
    )
    broken = await service(
        FakePlacesProvider(raises=ProviderRateLimited("slow down", "google_places"))
    ).search_places(SearchPlacesInput(query="ramen", **SHIBUYA))

    assert empty.found_nothing is True and empty.ok is True
    assert broken.found_nothing is False and broken.ok is False


async def test_identical_searches_are_served_from_cache():
    provider = FakePlacesProvider([summary()])
    places = service(provider)
    spec = SearchPlacesInput(query="ramen", **SHIBUYA)

    first = await places.search_places(spec)
    second = await places.search_places(spec)

    assert len(provider.search_calls) == 1
    assert [p.place_id for p in second.results] == [p.place_id for p in first.results]
    assert any("in-process cache" in w for w in second.warnings)


async def test_a_different_query_is_not_a_cache_hit():
    provider = FakePlacesProvider([summary()])
    places = service(provider)

    await places.search_places(SearchPlacesInput(query="ramen", **SHIBUYA))
    await places.search_places(SearchPlacesInput(query="coffee", **SHIBUYA))

    assert len(provider.search_calls) == 2


async def test_search_defaults_to_the_ranking_tier():
    """Ranking needs rating and rating count, so the search cannot be Pro-tier."""
    provider = FakePlacesProvider([summary()])

    await service(provider).search_places(SearchPlacesInput(query="ramen", **SHIBUYA))

    assert provider.search_calls[0]["field_set"] == PlaceFieldSet.RANKING


async def test_a_future_opening_time_warns_instead_of_pretending_to_filter():
    provider = FakePlacesProvider([summary()])

    result = await service(provider).search_places(
        SearchPlacesInput(query="ramen", **SHIBUYA, open_at=utcnow() + timedelta(days=3))
    )

    assert provider.search_calls[0]["open_now"] is None
    assert any("future opening time" in w for w in result.warnings)


async def test_open_now_is_used_when_the_time_really_is_now():
    provider = FakePlacesProvider([summary()])

    await service(provider).search_places(
        SearchPlacesInput(query="ramen", **SHIBUYA, open_at=utcnow())
    )

    assert provider.search_calls[0]["open_now"] is True


async def test_multiple_categories_warn_because_the_api_takes_one():
    provider = FakePlacesProvider([summary()])

    result = await service(provider).search_places(
        SearchPlacesInput(categories=["restaurant", "cafe"], **SHIBUYA)
    )

    assert provider.search_calls[0]["included_type"] is None
    assert any("one includedType" in w for w in result.warnings)


async def test_a_single_category_becomes_included_type():
    provider = FakePlacesProvider([summary()])

    await service(provider).search_places(SearchPlacesInput(categories=["restaurant"], **SHIBUYA))

    assert provider.search_calls[0]["included_type"] == "restaurant"


async def test_details_are_fetched_per_place_and_cached():
    provider = FakePlacesProvider([summary("ChIJ_a"), summary("ChIJ_b")])
    places = service(provider)
    spec = GetPlaceDetailsInput(place_ids=["ChIJ_a", "ChIJ_b"])

    await places.get_place_details(spec)
    await places.get_place_details(spec)

    assert provider.detail_calls == ["ChIJ_a", "ChIJ_b"]


async def test_details_default_to_the_full_tier():
    assert GetPlaceDetailsInput(place_ids=["x"]).field_set == PlaceFieldSet.FULL


async def test_coordinates_are_remembered_for_later_routing():
    provider = FakePlacesProvider([summary()])
    places = service(provider)

    await places.search_places(SearchPlacesInput(query="ramen", **SHIBUYA))

    assert await places.cached_coordinates("ChIJ_a") == (35.6659, 139.6979)


async def test_unknown_coordinates_are_none():
    assert await service(FakePlacesProvider()).cached_coordinates("ChIJ_missing") is None
