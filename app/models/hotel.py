from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import Money

# What is being measured, not just who measured it. A star category and a
# user rating are different *kinds* of number: one is a facility class awarded
# by an inspector, the other is what guests thought. Collapsing them into a
# single "rating" field is how "5-star" ends up compared against "4.3/5".
RatingType = Literal["star_category", "user_rating", "location_score"]

RatingSource = Literal["google_hotels", "google_places", "other"]


class HotelRating(BaseModel):
    value: float
    scale_max: float = 5.0

    type: RatingType
    source: RatingSource

    # Only meaningful for a user rating. A star category has no reviewers.
    review_count: int | None = None

    def describe(self) -> str:
        """Rendered so the kind is never lost in the telling."""
        where = self.source.replace("_", " ").title()
        if self.type == "star_category":
            return f"{self.value:g}-star ({where})"
        if self.type == "location_score":
            return f"location {self.value:g}/{self.scale_max:g} ({where})"
        if self.review_count:
            return f"{self.value:g}/{self.scale_max:g} from {self.review_count:,} reviews ({where})"
        return f"{self.value:g}/{self.scale_max:g} ({where})"


class HotelPriceQuote(BaseModel):
    """One vendor's price for one hotel.

    Google Hotels lists the same property at several booking sites at different
    rates, so there is no single "the price". The source travels with the
    number, and the link is somewhere to look - never a booking action.
    """

    source: str
    nightly: Money | None = None
    total: Money | None = None
    link: str | None = None


class SearchHotelsInput(BaseModel):
    """Spec section 16.

    An area is required. Searching a whole city for hotels is the step spec
    section 25 explicitly puts *after* choosing a neighbourhood, and the service
    refuses rather than quietly doing it out of order.
    """

    city: str | None = None
    area_name: str | None = None
    area_lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    area_lng: float | None = Field(default=None, ge=-180.0, le=180.0)

    check_in: date
    check_out: date

    adults: int = Field(default=2, ge=1, le=16)
    children: int = Field(default=0, ge=0, le=16)
    rooms: int = Field(default=1, ge=1, le=8)

    max_nightly_price: float | None = None
    min_rating: float | None = Field(default=None, ge=0.0, le=5.0)
    min_star_category: float | None = Field(default=None, ge=0.0, le=5.0)

    currency: str = "USD"
    limit: int = Field(default=20, ge=1, le=60)

    # Skipping the area decision has to be a decision itself, and be recorded.
    bypass_area_decision: bool = False
    bypass_reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "SearchHotelsInput":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if self.bypass_area_decision and not self.bypass_reason:
            raise ValueError(
                "bypass_area_decision requires bypass_reason: skipping the neighbourhood "
                "step is allowed, but not silently"
            )
        return self

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def has_location(self) -> bool:
        return self.area_lat is not None and self.area_lng is not None

    @property
    def query_text(self) -> str:
        """Free-text location for providers that take one.

        The area alone is not enough - "Shinjuku" without "Tokyo" is a gamble on
        the provider guessing the right country.
        """
        parts = [part for part in (self.area_name, self.city) if part]
        return " ".join(dict.fromkeys(parts)) or "hotels"


class HotelAreaCandidate(BaseModel):
    """A neighbourhood being considered, before it becomes a decision option."""

    area_name: str
    lat: float
    lng: float

    # Where it came from: clustered out of what the trip already plans to do, or
    # named by the agent and then geocoded.
    origin: Literal["trip_cluster", "suggested"] = "suggested"

    anchor_entity_ids: list[str] = []
    notes: str | None = None
