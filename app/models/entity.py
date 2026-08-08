from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow


class PlaceEntity(BaseModel):
    """A real-world place, stored once and referenced by id from the itinerary.

    Keeping facts here rather than inline in itinerary items means a place's
    hours or rating are refreshed in exactly one location.
    """

    entity_type: Literal["place"] = "place"

    entity_id: str = Field(default_factory=lambda: new_id("ent"))

    # e.g. {"google_place_id": "ChIJ..."}
    provider_refs: dict[str, str] = {}

    name: str
    categories: list[str] = []

    address: str | None = None

    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)

    rating: float | None = None
    rating_count: int | None = None
    price_level: int | None = None

    opening_hours: dict[str, Any] | None = None

    website_url: str | None = None
    maps_url: str | None = None

    facts_updated_at: datetime = Field(default_factory=utcnow)


# Becomes a discriminated union on `entity_type` when hotel/flight entities
# arrive in M5/M6. Existing stored JSON keeps validating because every place
# already carries entity_type="place".
TripEntity = PlaceEntity
