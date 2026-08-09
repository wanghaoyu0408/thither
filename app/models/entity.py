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

    # Google's regularOpeningHours. Read via app/services/opening_hours.py -
    # never trust the `openNow` key inside it, which is a snapshot from
    # whenever this was fetched.
    opening_hours: dict[str, Any] | None = None

    # Positive attestations only; None means "Google did not say", never "no".
    # See PlaceSummary.serves_vegetarian for why the distinction is load-bearing.
    serves_vegetarian: bool | None = None
    accessibility: dict[str, bool] = {}

    # IANA zone, e.g. "Asia/Tokyo".
    timezone: str | None = None

    website_url: str | None = None
    maps_url: str | None = None

    facts_updated_at: datetime = Field(default_factory=utcnow)


# Becomes a discriminated union on `entity_type` when hotel/flight entities
# arrive in M5/M6. Existing stored JSON keeps validating because every place
# already carries entity_type="place".
TripEntity = PlaceEntity
