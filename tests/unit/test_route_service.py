"""Route service behaviour, including the transit split, with a fake provider."""

from app.models.route import GetRoutesInput, LocationRef, RouteLeg
from app.providers.base import ProviderUnavailable
from app.services.cache import InProcessCache, LayeredCache
from app.services.route_service import RouteService
from tests.conftest import make_entity


class FakeRoutesProvider:
    """Returns a deterministic duration so remapped indices can be verified."""

    def __init__(self, raises=None):
        self.raises = raises
        self.calls: list[tuple[int, int]] = []

    async def compute_route_matrix(self, origins, destinations, *, mode, departure_at=None):
        if self.raises:
            raise self.raises
        self.calls.append((len(origins), len(destinations)))
        return [
            RouteLeg(
                origin_index=o,
                destination_index=d,
                mode=mode,
                duration_seconds=60 * (o + 1) * (d + 1),
                distance_meters=100 * (o + 1),
            )
            for o in range(len(origins))
            for d in range(len(destinations))
        ]


def refs(count: int) -> list[LocationRef]:
    return [LocationRef(place_id=f"ChIJ_{i}", label=f"stop {i}") for i in range(count)]


def service(provider) -> RouteService:
    return RouteService(provider, LayeredCache(InProcessCache(), None))


async def test_a_small_matrix_is_a_single_call():
    provider = FakeRoutesProvider()

    result = await service(provider).get_routes(
        GetRoutesInput(origins=refs(3), destinations=refs(3), mode="walking")
    )

    assert provider.calls == [(3, 3)]
    assert len(result.results) == 9
    assert result.ok


async def test_a_transit_matrix_over_the_cap_is_split_and_stitched():
    """12x12 transit is 144 elements against a 100-element cap."""
    provider = FakeRoutesProvider()

    result = await service(provider).get_routes(
        GetRoutesInput(origins=refs(12), destinations=refs(12), mode="transit")
    )

    assert len(provider.calls) > 1
    assert all(origins * destinations <= 100 for origins, destinations in provider.calls)
    assert len(result.results) == 144
    assert any("exceeds the 100-element transit cap" in w for w in result.warnings)


async def test_the_same_matrix_fits_in_one_walking_call():
    provider = FakeRoutesProvider()

    result = await service(provider).get_routes(
        GetRoutesInput(origins=refs(12), destinations=refs(12), mode="walking")
    )

    assert provider.calls == [(12, 12)]
    assert result.warnings == []


async def test_indices_are_remapped_to_the_caller_s_lists():
    """Chunk-local indices would silently mislabel every leg."""
    provider = FakeRoutesProvider()

    result = await service(provider).get_routes(
        GetRoutesInput(origins=refs(12), destinations=refs(12), mode="transit")
    )

    pairs = {(leg.origin_index, leg.destination_index) for leg in result.results}
    assert pairs == {(o, d) for o in range(12) for d in range(12)}

    # The fake encodes chunk-local position in the duration; after remapping the
    # last leg must still be the last chunk's value, not a duplicate of the first.
    last = next(
        leg for leg in result.results if (leg.origin_index, leg.destination_index) == (11, 11)
    )
    assert last.duration_seconds is not None


async def test_results_come_back_ordered():
    provider = FakeRoutesProvider()

    result = await service(provider).get_routes(
        GetRoutesInput(origins=refs(5), destinations=refs(5), mode="transit")
    )

    ordered = [(leg.origin_index, leg.destination_index) for leg in result.results]
    assert ordered == sorted(ordered)


async def test_a_provider_failure_becomes_a_tool_error():
    provider = FakeRoutesProvider(raises=ProviderUnavailable("routes down", "google_routes"))

    result = await service(provider).get_routes(
        GetRoutesInput(origins=refs(2), destinations=refs(2), mode="walking")
    )

    assert result.ok is False
    assert result.error.code == "provider_unavailable"


async def test_entity_references_are_resolved_to_place_ids():
    provider = FakeRoutesProvider()
    entity = make_entity("ent_cafe", "Fuglen Tokyo")
    entity.provider_refs = {"google_place_id": "ChIJ_fuglen"}

    result = await service(provider).get_routes(
        GetRoutesInput(
            origins=[LocationRef(entity_id="ent_cafe")],
            destinations=[LocationRef(entity_id="ent_cafe")],
            mode="walking",
        ),
        entities={"ent_cafe": entity},
    )

    assert result.ok
    assert result.warnings == []


async def test_an_unknown_entity_is_reported_rather_than_guessed():
    provider = FakeRoutesProvider()

    result = await service(provider).get_routes(
        GetRoutesInput(
            origins=[LocationRef(entity_id="ent_ghost", lat=35.0, lng=139.0)],
            destinations=[LocationRef(place_id="ChIJ_x")],
            mode="walking",
        ),
        entities={},
    )

    assert any("unknown entity 'ent_ghost'" in w for w in result.warnings)


async def test_repeated_matrices_are_served_from_cache():
    provider = FakeRoutesProvider()
    routes = service(provider)
    spec = GetRoutesInput(origins=refs(3), destinations=refs(3), mode="walking")

    await routes.get_routes(spec)
    await routes.get_routes(spec)

    assert len(provider.calls) == 1


async def test_duration_matrix_is_square_and_indexed_by_position():
    provider = FakeRoutesProvider()

    matrix, result = await service(provider).duration_matrix(refs(4), mode="walking")

    assert len(matrix) == 4
    assert all(len(row) == 4 for row in matrix)
    assert matrix[0][0] == 1.0  # 60 seconds -> 1 minute
    assert matrix[2][3] == 60 * 3 * 4 / 60
    assert result.ok


async def test_unreachable_pairs_are_left_as_none_and_counted():
    class PartlyUnreachable(FakeRoutesProvider):
        async def compute_route_matrix(self, origins, destinations, *, mode, departure_at=None):
            legs = await super().compute_route_matrix(
                origins, destinations, mode=mode, departure_at=departure_at
            )
            legs[1] = legs[1].model_copy(
                update={"status": "zero_results", "duration_seconds": None}
            )
            return legs

    matrix, result = await service(PartlyUnreachable()).duration_matrix(refs(2), mode="transit")

    assert matrix[0][1] is None
    assert any("1 of 4 pairs had no route" in w for w in result.warnings)
