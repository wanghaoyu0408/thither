"""Geographic clustering, so a day does not zigzag across the city.

Deliberately simple and deterministic: farthest-point seeding then Lloyd
iterations over straight-line distance, with every tie broken by entity id so
the same input always produces the same days. A trip that reshuffles itself on
re-run would be impossible to reason about, and impossible to test.

Straight-line distance is only ever used to *group*. What the user is told
about travel time always comes from the Routes API (spec section 21).
"""

from dataclasses import dataclass, field

from app.services.geo import centroid, haversine_km

Point = tuple[float, float]


@dataclass
class Cluster:
    index: int
    center: Point
    entity_ids: list[str] = field(default_factory=list)
    label: str | None = None

    @property
    def size(self) -> int:
        return len(self.entity_ids)

    def spread_km(self, points: dict[str, Point]) -> float:
        """Farthest member from the centre - how tight this day actually is."""
        if not self.entity_ids:
            return 0.0
        return max(
            haversine_km(self.center[0], self.center[1], *points[entity_id])
            for entity_id in self.entity_ids
        )


def _seed(points: dict[str, Point], k: int) -> list[Point]:
    """Farthest-point seeding from a deterministic start."""
    ordered = sorted(points)
    seeds: list[Point] = [points[ordered[0]]]

    while len(seeds) < k:
        best_id, best_distance = None, -1.0
        for entity_id in ordered:
            lat, lng = points[entity_id]
            nearest = min(haversine_km(lat, lng, seed[0], seed[1]) for seed in seeds)
            # Strictly greater keeps the earliest id on a tie.
            if nearest > best_distance:
                best_id, best_distance = entity_id, nearest
        if best_id is None:
            break
        seeds.append(points[best_id])

    return seeds


def _assign(points: dict[str, Point], centres: list[Point]) -> list[list[str]]:
    groups: list[list[str]] = [[] for _ in centres]
    for entity_id in sorted(points):
        lat, lng = points[entity_id]
        distances = [haversine_km(lat, lng, c[0], c[1]) for c in centres]
        groups[distances.index(min(distances))].append(entity_id)
    return groups


def _refill_empty(groups: list[list[str]], points: dict[str, Point], centres: list[Point]) -> None:
    """Give an empty cluster the member the biggest cluster wants least.

    An empty cluster means a day with nothing on it, so it is worth breaking
    strict nearest-centre assignment to avoid.
    """
    for group in groups:
        if group:
            continue
        donor = max(range(len(groups)), key=lambda i: (len(groups[i]), -i))
        if len(groups[donor]) < 2:
            continue
        centre = centres[donor]
        outlier = max(
            groups[donor],
            key=lambda entity_id: (
                haversine_km(*points[entity_id], centre[0], centre[1]),
                entity_id,
            ),
        )
        groups[donor].remove(outlier)
        group.append(outlier)


def cluster_places(
    points: dict[str, Point],
    k: int,
    *,
    max_iterations: int = 20,
) -> list[Cluster]:
    """Group places into `k` geographically coherent sets."""
    if not points or k <= 0:
        return []

    k = min(k, len(points))
    centres = _seed(points, k)

    groups: list[list[str]] = []
    for _ in range(max_iterations):
        groups = _assign(points, centres)
        _refill_empty(groups, points, centres)

        moved = False
        for index, group in enumerate(groups):
            if not group:
                continue
            recomputed = centroid([points[entity_id] for entity_id in group])
            if recomputed and recomputed != centres[index]:
                centres[index] = recomputed
                moved = True
        if not moved:
            break

    clusters = [
        Cluster(index=index, center=centres[index], entity_ids=sorted(group))
        for index, group in enumerate(groups)
    ]
    # West to east, so day order follows a sensible geographic sweep and is
    # stable between runs.
    clusters.sort(key=lambda cluster: (cluster.center[1], cluster.center[0]))
    for position, cluster in enumerate(clusters):
        cluster.index = position
    return clusters


def label_clusters(clusters: list[Cluster], area_names: dict[str, str]) -> None:
    """Name each cluster after whichever area most of its members belong to."""
    for cluster in clusters:
        counts: dict[str, int] = {}
        for entity_id in cluster.entity_ids:
            name = area_names.get(entity_id)
            if name:
                counts[name] = counts.get(name, 0) + 1
        if counts:
            cluster.label = max(sorted(counts), key=lambda name: counts[name])
