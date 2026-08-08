"""Google Routes API.

    POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix
    POST https://routes.googleapis.com/directions/v2:computeRoutes

The matrix endpoint streams a JSON array of elements, each carrying its own
`originIndex` / `destinationIndex` - results are not ordered, so they must be
placed by index rather than by arrival order. Elements whose `status` is
non-empty, or that carry `condition: ROUTE_NOT_FOUND`, are unreachable pairs
rather than failures of the call.

**Transit coverage is regional.** Both endpoints answer TRANSIT queries in the
US and the UK and return no route at all in Japan - Google does not license
Japanese rail data for its routing APIs. That is a data gap, not an error: the
call succeeds and every pair comes back `ROUTE_NOT_FOUND`. Callers that need a
comparison rather than a fact handle it by falling back to another mode and
saying which one they used; see `hotel_area_service.measure_travel`.
"""

from datetime import datetime
from typing import Any

import httpx

from app.models.route import GOOGLE_TRAVEL_MODE, LocationRef, RouteLeg, TravelMode
from app.providers.base import request_json

PROVIDER = "google_routes"
BASE_URL = "https://routes.googleapis.com"

MATRIX_FIELD_MASK = "originIndex,destinationIndex,duration,distanceMeters,status,condition"
ROUTE_FIELD_MASK = "routes.duration,routes.distanceMeters,routes.legs.duration"


def waypoint(ref: LocationRef) -> dict[str, Any]:
    """A LocationRef as a Routes API waypoint.

    place_id wins when present: it is unambiguous, and it is the only piece of
    Google content we are allowed to keep indefinitely.
    """
    if ref.place_id:
        return {"placeId": ref.place_id}
    if ref.lat is not None and ref.lng is not None:
        return {"location": {"latLng": {"latitude": ref.lat, "longitude": ref.lng}}}
    if ref.address:
        return {"address": ref.address}
    raise ValueError(f"LocationRef cannot be turned into a waypoint: {ref!r}")


def _parse_duration(value: Any) -> int | None:
    """Google returns protobuf durations as strings like '1234s'."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    text = str(value).strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return int(float(text))
    except ValueError:
        return None


class GoogleRoutesProvider:
    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._client = client

    def _headers(self, field_mask: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": field_mask,
        }

    async def compute_route_matrix(
        self,
        origins: list[LocationRef],
        destinations: list[LocationRef],
        *,
        mode: TravelMode,
        departure_at: datetime | None = None,
    ) -> list[RouteLeg]:
        """One matrix call. Callers must keep elements under the mode's cap."""
        google_mode = GOOGLE_TRAVEL_MODE[mode]

        body: dict[str, Any] = {
            "origins": [{"waypoint": waypoint(ref)} for ref in origins],
            "destinations": [{"waypoint": waypoint(ref)} for ref in destinations],
            "travelMode": google_mode,
        }
        if departure_at is not None:
            body["departureTime"] = departure_at.isoformat().replace("+00:00", "Z")
        if google_mode == "DRIVE":
            body["routingPreference"] = "TRAFFIC_AWARE"

        payload = await request_json(
            self._client,
            "POST",
            f"{BASE_URL}/distanceMatrix/v2:computeRouteMatrix",
            provider=PROVIDER,
            headers=self._headers(MATRIX_FIELD_MASK),
            json_body=body,
        )

        # The endpoint streams a bare JSON array; request_json hands back
        # whatever it decoded.
        elements = payload if isinstance(payload, list) else payload.get("elements", [])

        legs: list[RouteLeg] = []
        for element in elements:
            origin_index = element.get("originIndex", 0)
            destination_index = element.get("destinationIndex", 0)

            failed = bool(element.get("status"))
            unreachable = element.get("condition") == "ROUTE_NOT_FOUND"

            legs.append(
                RouteLeg(
                    origin_index=origin_index,
                    destination_index=destination_index,
                    origin_label=origins[origin_index].describe()
                    if origin_index < len(origins)
                    else None,
                    destination_label=destinations[destination_index].describe()
                    if destination_index < len(destinations)
                    else None,
                    mode=mode,
                    distance_meters=element.get("distanceMeters"),
                    duration_seconds=_parse_duration(element.get("duration")),
                    status="not_found" if failed else ("zero_results" if unreachable else "ok"),
                )
            )
        return legs

    async def compute_route(
        self,
        origin: LocationRef,
        destination: LocationRef,
        *,
        mode: TravelMode,
        departure_at: datetime | None = None,
    ) -> RouteLeg:
        google_mode = GOOGLE_TRAVEL_MODE[mode]

        # Note the shape: computeRoutes takes a Waypoint directly, while
        # computeRouteMatrix wraps each one in {"waypoint": ...}. Sending the
        # matrix shape here is rejected with "Unknown name 'waypoint'".
        body: dict[str, Any] = {
            "origin": waypoint(origin),
            "destination": waypoint(destination),
            "travelMode": google_mode,
        }
        if departure_at is not None:
            body["departureTime"] = departure_at.isoformat().replace("+00:00", "Z")
        if google_mode == "DRIVE":
            body["routingPreference"] = "TRAFFIC_AWARE"

        payload = await request_json(
            self._client,
            "POST",
            f"{BASE_URL}/directions/v2:computeRoutes",
            provider=PROVIDER,
            headers=self._headers(ROUTE_FIELD_MASK),
            json_body=body,
        )

        routes = payload.get("routes") or []
        if not routes:
            return RouteLeg(
                origin_index=0,
                destination_index=0,
                origin_label=origin.describe(),
                destination_label=destination.describe(),
                mode=mode,
                status="zero_results",
            )

        best = routes[0]
        return RouteLeg(
            origin_index=0,
            destination_index=0,
            origin_label=origin.describe(),
            destination_label=destination.describe(),
            mode=mode,
            distance_meters=best.get("distanceMeters"),
            duration_seconds=_parse_duration(best.get("duration")),
        )
