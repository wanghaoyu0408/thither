"""Straight-line geometry.

Only ever a cheap pre-filter or a tiebreaker. Anything the user is told about
travel time comes from the Routes API - spec section 21 is explicit that the
model must not estimate travel times when the tool is available, and neither
should we.
"""

import math

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Mean position. Fine for city-scale clusters, wrong across the date line."""
    if not points:
        return None
    return (
        sum(lat for lat, _ in points) / len(points),
        sum(lng for _, lng in points) / len(points),
    )
