"""Business-level place search (spec section 13).

The agent calls `search_places` / `get_place_details`, never a Google endpoint,
so the provider stays replaceable. Provider exceptions are translated into
`ToolResult.error` here - the layer above must be able to tell "nowhere in this
neighbourhood matches" from "the API is down", and never sees a raw exception.
"""

from datetime import datetime, timedelta

from app.models.common import utcnow
from app.models.entity import PlaceEntity
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, PlaceSummary, SearchPlacesInput
from app.models.tool import ToolResult
from app.providers.base import ProviderError
from app.providers.google_places import PROVIDER, GooglePlacesProvider
from app.services.cache import (
    LAT_LNG_POLICY,
    VOLATILE_POLICY,
    Cache,
    RequestDeduper,
    cache_key,
)
from app.services.entity_service import place_id_of

# How close `open_at` must be to now for the provider's openNow flag to mean
# what the caller asked.
OPEN_NOW_TOLERANCE = timedelta(minutes=30)

SEARCH_TTL = timedelta(hours=1)


class PlaceService:
    def __init__(
        self,
        provider: GooglePlacesProvider,
        cache: Cache,
        deduper: RequestDeduper | None = None,
        *,
        language: str | None = None,
        region: str | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._deduper = deduper or RequestDeduper()
        self._language = language
        self._region = region

    async def search_places(self, spec: SearchPlacesInput) -> ToolResult[PlaceSummary]:
        warnings: list[str] = []

        open_now = None
        if spec.open_at is not None:
            reference = utcnow()
            target = spec.open_at
            if target.tzinfo is None:
                target = target.replace(tzinfo=reference.tzinfo)
            if abs(target - reference) <= OPEN_NOW_TOLERANCE:
                open_now = True
            else:
                warnings.append(
                    "Places cannot filter by a future opening time; results are unfiltered on "
                    "open_at. Verify with get_place_details before promising a place is open."
                )

        text_query = spec.query or " ".join(spec.categories) or "places"
        included_type = spec.categories[0] if len(spec.categories) == 1 else None
        if len(spec.categories) > 1:
            warnings.append(
                f"Places accepts one includedType; using the text query for {spec.categories}."
            )

        key = cache_key(
            "places:search",
            {
                "q": text_query,
                "type": included_type,
                "lat": round(spec.lat, 5),
                "lng": round(spec.lng, 5),
                "radius": spec.radius_meters,
                "min_rating": spec.min_rating,
                "price_levels": spec.price_levels,
                "open_now": open_now,
                "limit": spec.limit,
                "fields": spec.field_set.value,
                "lang": spec.language or self._language,
                "region": spec.region or self._region,
            },
        )

        cached = await self._cache.get(key, VOLATILE_POLICY)
        if cached is not None:
            return ToolResult[PlaceSummary](
                source=PROVIDER,
                results=[PlaceSummary.model_validate(item) for item in cached],
                warnings=[*warnings, "served from the in-process cache"],
                expires_at=utcnow() + SEARCH_TTL,
            )

        async def call() -> list[PlaceSummary]:
            return await self._provider.search_text(
                text_query=text_query,
                field_set=spec.field_set,
                lat=spec.lat,
                lng=spec.lng,
                radius_meters=spec.radius_meters,
                included_type=included_type,
                min_rating=spec.min_rating,
                price_levels=spec.price_levels,
                open_now=open_now,
                limit=spec.limit,
                language=spec.language or self._language,
                region=spec.region or self._region,
            )

        try:
            places = await self._deduper.run(key, call)
        except ProviderError as exc:
            return ToolResult[PlaceSummary](
                source=PROVIDER, error=exc.as_tool_error(), warnings=warnings
            )

        await self._cache.set(key, [p.model_dump(mode="json") for p in places], VOLATILE_POLICY)
        await self._remember_coordinates(places)

        return ToolResult[PlaceSummary](
            source=PROVIDER,
            results=places,
            warnings=warnings,
            expires_at=utcnow() + SEARCH_TTL,
        )

    async def get_place_details(self, spec: GetPlaceDetailsInput) -> ToolResult[PlaceSummary]:
        """Fetch rich fields for an already-shortlisted set (spec section 18)."""
        results: list[PlaceSummary] = []
        warnings: list[str] = []

        for place_id in spec.place_ids:
            key = cache_key(
                "places:details",
                {
                    "id": place_id,
                    "fields": spec.field_set.value,
                    "lang": spec.language or self._language,
                },
            )

            cached = await self._cache.get(key, VOLATILE_POLICY)
            if cached is not None:
                results.append(PlaceSummary.model_validate(cached))
                continue

            async def call(pid: str = place_id) -> PlaceSummary:
                return await self._provider.get_details(
                    pid, field_set=spec.field_set, language=spec.language or self._language
                )

            try:
                place = await self._deduper.run(key, call)
            except ProviderError as exc:
                # One dead id must not lose the places that did resolve.
                if not results:
                    return ToolResult[PlaceSummary](source=PROVIDER, error=exc.as_tool_error())
                warnings.append(f"details for {place_id} failed: {exc.message}")
                continue

            await self._cache.set(key, place.model_dump(mode="json"), VOLATILE_POLICY)
            results.append(place)

        await self._remember_coordinates(results)
        return ToolResult[PlaceSummary](source=PROVIDER, results=results, warnings=warnings)

    async def refresh_entities(
        self, entities: list[PlaceEntity], *, field_set: PlaceFieldSet = PlaceFieldSet.FULL
    ) -> ToolResult[PlaceSummary]:
        """Re-fetch facts for entities whose snapshot has gone stale."""
        place_ids = [pid for pid in (place_id_of(entity) for entity in entities) if pid]
        if not place_ids:
            return ToolResult[PlaceSummary](
                source=PROVIDER,
                warnings=["none of the supplied entities carry a google_place_id"],
            )
        return await self.get_place_details(
            GetPlaceDetailsInput(place_ids=place_ids, field_set=field_set)
        )

    async def _remember_coordinates(self, places: list[PlaceSummary]) -> None:
        """Persist place_id -> lat/lng, the one fact pair we may keep.

        Under a 30-day expiry, per the Maps terms. Names, ratings and hours
        stay in the in-process cache only.
        """
        for place in places:
            if place.lat is None or place.lng is None:
                continue
            await self._cache.set(
                cache_key("places:latlng", {"id": place.place_id}),
                {"place_id": place.place_id, "lat": place.lat, "lng": place.lng},
                LAT_LNG_POLICY,
            )

    async def cached_coordinates(self, place_id: str) -> tuple[float, float] | None:
        cached = await self._cache.get(cache_key("places:latlng", {"id": place_id}), LAT_LNG_POLICY)
        return None if cached is None else (cached["lat"], cached["lng"])


def build_search(
    *,
    query: str | None,
    lat: float,
    lng: float,
    categories: list[str] | None = None,
    radius_meters: int = 1500,
    limit: int = 20,
    min_rating: float | None = None,
    open_at: datetime | None = None,
    field_set: PlaceFieldSet = PlaceFieldSet.RANKING,
) -> SearchPlacesInput:
    """Convenience for scripts; the agent will fill SearchPlacesInput directly."""
    return SearchPlacesInput(
        query=query,
        categories=categories or [],
        lat=lat,
        lng=lng,
        radius_meters=radius_meters,
        limit=limit,
        min_rating=min_rating,
        open_at=open_at,
        field_set=field_set,
    )
