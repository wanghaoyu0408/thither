from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

TravelMode = Literal["walking", "driving", "transit", "bicycling"]

# Google's own names, used when talking to the Routes API.
GOOGLE_TRAVEL_MODE: dict[str, str] = {
    "walking": "WALK",
    "driving": "DRIVE",
    "transit": "TRANSIT",
    "bicycling": "BICYCLE",
}

# computeRouteMatrix element caps (origins x destinations). Transit is far
# tighter than the rest, so route_service chunks per mode rather than assuming
# one number.
MAX_MATRIX_ELEMENTS: dict[str, int] = {
    "TRANSIT": 100,
    "DRIVE": 625,
    "WALK": 625,
    "BICYCLE": 625,
}


class LocationRef(BaseModel):
    """A waypoint, addressed however the caller happens to know it.

    `place_id` is preferred: it is exact, and it is the one piece of Google
    content that may be stored indefinitely.
    """

    entity_id: str | None = None
    place_id: str | None = None
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lng: float | None = Field(default=None, ge=-180.0, le=180.0)
    address: str | None = None

    # Carried through to the result so output is readable without a lookup.
    label: str | None = None

    @model_validator(mode="after")
    def _needs_an_address(self) -> "LocationRef":
        has_coords = self.lat is not None and self.lng is not None
        if not (self.entity_id or self.place_id or has_coords or self.address):
            raise ValueError("LocationRef needs one of: entity_id, place_id, lat+lng, or address")
        return self

    def describe(self) -> str:
        return self.label or self.place_id or self.address or f"{self.lat},{self.lng}"


class GetRoutesInput(BaseModel):
    origins: list[LocationRef] = Field(min_length=1)
    destinations: list[LocationRef] = Field(min_length=1)

    mode: TravelMode = "walking"

    # Matters for transit, which is timetable-dependent.
    departure_at: datetime | None = None

    @property
    def element_count(self) -> int:
        return len(self.origins) * len(self.destinations)


class RouteLeg(BaseModel):
    """One origin-destination pair. Indices refer to the input lists."""

    origin_index: int
    destination_index: int

    origin_label: str | None = None
    destination_label: str | None = None

    mode: TravelMode

    distance_meters: int | None = None
    duration_seconds: int | None = None

    status: Literal["ok", "not_found", "zero_results"] = "ok"

    @property
    def duration_minutes(self) -> float | None:
        return None if self.duration_seconds is None else self.duration_seconds / 60.0
