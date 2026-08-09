from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.models.common import AttestationField, AttestationMap


class PlaceFieldSet(StrEnum):
    """How much to ask Google for, which is also how much it costs.

    Places API (New) bills by the most expensive field in the mask, so the tier
    is an explicit per-call decision rather than a constant:

        IDS      Essentials  - id only
        BASIC    Pro         - + name, address, location, types, maps url
        RANKING  Enterprise  - + rating, rating count, price level
        FULL     Enterprise  - + opening hours, website

    Ranking a candidate list needs rating and rating count, so a search cannot
    be done at Pro tier and still be rankable. The saving that spec section 18
    is really after comes from fetching FULL for only the 3-5 shortlisted
    places, not for all 20 candidates.
    """

    IDS = "ids"
    BASIC = "basic"
    RANKING = "ranking"
    FULL = "full"


class SearchPlacesInput(BaseModel):
    """One search covering restaurants, cafes, bars, museums, parks and shops.

    Spec section 13: deliberately no per-category tool.
    """

    query: str | None = None

    # Google place types, e.g. ["restaurant"], ["cafe"], ["museum"].
    categories: list[str] = []

    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)
    radius_meters: int | None = Field(default=None, gt=0, le=50_000)

    min_rating: float | None = Field(default=None, ge=0.0, le=5.0)
    price_levels: list[int] | None = None

    open_at: datetime | None = None

    limit: int = Field(default=20, ge=1, le=60)

    field_set: PlaceFieldSet = PlaceFieldSet.RANKING

    language: str | None = None
    region: str | None = None


class GetPlaceDetailsInput(BaseModel):
    """Detail fetch for an already-shortlisted set (spec section 18)."""

    place_ids: list[str] = Field(min_length=1)
    field_set: PlaceFieldSet = PlaceFieldSet.FULL
    language: str | None = None


class PlaceSummary(BaseModel):
    """A normalized Google place.

    One model serves search and details; which fields are populated depends on
    the `PlaceFieldSet` used, so `rating is None` means "not requested or not
    published", never "zero".

    `opening_hours is None` is common and load-bearing: many small venues
    publish none. It means "unknown", never "closed" - the M3 validator has to
    keep those apart rather than assert a place is shut.
    """

    place_id: str

    name: str | None = None
    address: str | None = None

    lat: float | None = None
    lng: float | None = None

    categories: list[str] = []
    primary_type: str | None = None

    rating: float | None = None
    rating_count: int | None = None
    # Normalized to 0-4; Google returns PRICE_LEVEL_* enum strings.
    price_level: int | None = None

    opening_hours: dict[str, Any] | None = None

    # Tri-state on purpose: Google attests the positive case only, so its False
    # is "not attested", never "confirmed absent" - see Attestation in
    # app/models/common.py for the measured evidence. Old bool|None values in
    # cached JSON are coerced (True -> confirmed_true, False/None -> unknown).
    serves_vegetarian: AttestationField = "unknown"
    parking_options: dict[str, Any] | None = None

    # Same asymmetry, per accessibility option.
    accessibility: AttestationMap = {}

    website_url: str | None = None
    maps_url: str | None = None

    business_status: str | None = None

    # IANA zone id from the Places timeZone field.
    timezone: str | None = None

    def is_operational(self) -> bool:
        return self.business_status in (None, "OPERATIONAL")
