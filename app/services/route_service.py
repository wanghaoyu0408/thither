"""Business-level routing (spec section 21).

Critical for hotel-area ranking, daily feasibility and geographic clustering -
and the reason the agent is never allowed to estimate a travel time itself.

The interesting problem here is the element cap. `computeRouteMatrix` allows
625 origin-destination pairs for walking, driving and cycling but only **100
for transit**, so a 12x12 transit matrix fails as a single call. Requests are
therefore split per mode, issued concurrently, and stitched back by index.
"""

import asyncio
from datetime import datetime, timedelta

from app.models.common import utcnow
from app.models.entity import PlaceEntity
from app.models.route import (
    GOOGLE_TRAVEL_MODE,
    MAX_MATRIX_ELEMENTS,
    GetRoutesInput,
    LocationRef,
    RouteLeg,
    TravelMode,
)
from app.models.tool import ToolResult
from app.providers.base import ProviderError
from app.providers.google_routes import PROVIDER, GoogleRoutesProvider
from app.services.cache import VOLATILE_POLICY, Cache, RequestDeduper, cache_key
from app.services.entity_service import place_id_of

MAX_CONCURRENT_CHUNKS = 4
ROUTE_TTL = timedelta(hours=1)


def max_elements_for(mode: TravelMode) -> int:
    return MAX_MATRIX_ELEMENTS[GOOGLE_TRAVEL_MODE[mode]]


def chunk_matrix(
    origin_count: int, destination_count: int, max_elements: int
) -> list[tuple[range, range]]:
    """Split an origins x destinations matrix into sub-matrices within the cap.

    Destinations are packed first because the API bills per element either way,
    and wider blocks mean fewer round trips.
    """
    if origin_count <= 0 or destination_count <= 0:
        return []

    destination_block = min(destination_count, max_elements)
    origin_block = max(1, max_elements // destination_block)

    chunks: list[tuple[range, range]] = []
    for origin_start in range(0, origin_count, origin_block):
        origin_stop = min(origin_start + origin_block, origin_count)
        for destination_start in range(0, destination_count, destination_block):
            destination_stop = min(destination_start + destination_block, destination_count)
            chunks.append(
                (range(origin_start, origin_stop), range(destination_start, destination_stop))
            )
    return chunks


def resolve_location_refs(
    refs: list[LocationRef], entities: dict[str, PlaceEntity]
) -> tuple[list[LocationRef], list[str]]:
    """Turn entity_id references into something the Routes API can address."""
    resolved: list[LocationRef] = []
    problems: list[str] = []

    for ref in refs:
        if not ref.entity_id:
            resolved.append(ref)
            continue

        entity = entities.get(ref.entity_id)
        if entity is None:
            problems.append(f"unknown entity {ref.entity_id!r}")
            resolved.append(ref)
            continue

        resolved.append(
            LocationRef(
                entity_id=ref.entity_id,
                place_id=place_id_of(entity),
                lat=entity.lat,
                lng=entity.lng,
                label=ref.label or entity.name,
            )
        )
    return resolved, problems


class RouteService:
    def __init__(
        self,
        provider: GoogleRoutesProvider,
        cache: Cache,
        deduper: RequestDeduper | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._deduper = deduper or RequestDeduper()

    async def get_routes(
        self,
        spec: GetRoutesInput,
        *,
        entities: dict[str, PlaceEntity] | None = None,
    ) -> ToolResult[RouteLeg]:
        warnings: list[str] = []

        origins, destinations = spec.origins, spec.destinations
        # `is not None`, not truthiness: an empty registry still has to report
        # entity references it cannot resolve rather than silently skipping them.
        if entities is not None:
            origins, origin_problems = resolve_location_refs(origins, entities)
            destinations, destination_problems = resolve_location_refs(destinations, entities)
            warnings.extend(origin_problems + destination_problems)

        cap = max_elements_for(spec.mode)
        chunks = chunk_matrix(len(origins), len(destinations), cap)
        if len(chunks) > 1:
            warnings.append(
                f"{spec.element_count} elements exceeds the {cap}-element {spec.mode} cap; "
                f"split into {len(chunks)} matrix calls"
            )

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)

        async def run_chunk(origin_range: range, destination_range: range) -> list[RouteLeg]:
            sub_origins = [origins[i] for i in origin_range]
            sub_destinations = [destinations[i] for i in destination_range]

            key = cache_key(
                "routes:matrix",
                {
                    "o": [_ref_key(ref) for ref in sub_origins],
                    "d": [_ref_key(ref) for ref in sub_destinations],
                    "mode": spec.mode,
                    # Bucket to the hour: transit is timetable-sensitive, but
                    # not minute-sensitive.
                    "departure": spec.departure_at.replace(
                        minute=0, second=0, microsecond=0
                    ).isoformat()
                    if spec.departure_at
                    else None,
                },
            )

            cached = await self._cache.get(key, VOLATILE_POLICY)
            if cached is not None:
                legs = [RouteLeg.model_validate(item) for item in cached]
            else:

                async def call() -> list[RouteLeg]:
                    async with semaphore:
                        return await self._provider.compute_route_matrix(
                            sub_origins,
                            sub_destinations,
                            mode=spec.mode,
                            departure_at=spec.departure_at,
                        )

                legs = await self._deduper.run(key, call)
                await self._cache.set(
                    key, [leg.model_dump(mode="json") for leg in legs], VOLATILE_POLICY
                )

            # Chunk-local indices mean nothing to the caller; remap to global.
            return [
                leg.model_copy(
                    update={
                        "origin_index": origin_range.start + leg.origin_index,
                        "destination_index": destination_range.start + leg.destination_index,
                    }
                )
                for leg in legs
            ]

        try:
            batches = await asyncio.gather(*(run_chunk(o, d) for o, d in chunks))
        except ProviderError as exc:
            return ToolResult[RouteLeg](
                source=PROVIDER, error=exc.as_tool_error(), warnings=warnings
            )

        legs = [leg for batch in batches for leg in batch]
        legs.sort(key=lambda leg: (leg.origin_index, leg.destination_index))

        unreachable = sum(1 for leg in legs if leg.status != "ok")
        if unreachable:
            warnings.append(f"{unreachable} of {len(legs)} pairs had no route")

        return ToolResult[RouteLeg](
            source=PROVIDER,
            results=legs,
            warnings=warnings,
            expires_at=utcnow() + ROUTE_TTL,
        )

    async def duration_matrix(
        self,
        refs: list[LocationRef],
        *,
        mode: TravelMode = "walking",
        departure_at: datetime | None = None,
        entities: dict[str, PlaceEntity] | None = None,
    ) -> tuple[list[list[float | None]], ToolResult[RouteLeg]]:
        """All-pairs travel minutes, as a square matrix plus the raw result."""
        result = await self.get_routes(
            GetRoutesInput(origins=refs, destinations=refs, mode=mode, departure_at=departure_at),
            entities=entities,
        )

        size = len(refs)
        matrix: list[list[float | None]] = [[None] * size for _ in range(size)]
        for leg in result.results:
            if leg.status == "ok":
                matrix[leg.origin_index][leg.destination_index] = leg.duration_minutes
        return matrix, result


def _ref_key(ref: LocationRef) -> str:
    if ref.place_id:
        return f"pid:{ref.place_id}"
    if ref.lat is not None and ref.lng is not None:
        return f"ll:{ref.lat:.5f},{ref.lng:.5f}"
    return f"addr:{ref.address}"
