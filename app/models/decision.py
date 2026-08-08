from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import Cabin, LatLng, Money, new_id, utcnow

OptionStatus = Literal["candidate", "shortlisted", "selected", "rejected"]
DecisionStatus = Literal["researching", "shortlisted", "selected", "locked"]


class DecisionScore(BaseModel):
    """Dimensional scores are kept so a recommendation stays explainable.

    `total` is never treated as objective truth - it is a ranking aid whose
    components must be quotable back to the user (spec section 23).
    """

    total: float
    dimensions: dict[str, float] = {}
    notes: str | None = None


class DecisionOption[T](BaseModel):
    option_id: str = Field(default_factory=lambda: new_id("opt"))

    data: T

    status: OptionStatus = "candidate"

    score: DecisionScore | None = None

    pros: list[str] = []
    cons: list[str] = []

    # Ids of stored research/tool results supporting this option.
    evidence_refs: list[str] = []


class Decision[T](BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("dec"))

    status: DecisionStatus = "researching"

    options: list[DecisionOption[T]] = []

    selected_option_id: str | None = None

    rationale: str | None = None

    updated_at: datetime = Field(default_factory=utcnow)


# --- Decision payloads -------------------------------------------------------
# Deliberately thin in M1. Each is filled out by the milestone that fetches it:
# destination/hotel area in M2-M3, flights in M5, hotels in M6.


class DestinationOption(BaseModel):
    city: str
    country: str | None = None
    region: str | None = None
    notes: str | None = None


class HotelAreaOption(BaseModel):
    """A neighborhood, decided *before* individual hotels (spec section 25)."""

    area_name: str
    center: LatLng | None = None
    # Anchor POIs that make this area convenient.
    anchor_entity_ids: list[str] = []
    notes: str | None = None


class FlightOptionData(BaseModel):
    """Expanded in M5 with normalized Duffel segments."""

    provider: str
    offer_ref: str
    price: Money
    origin: str
    destination: str
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    duration_minutes: int | None = None
    stops: int | None = None
    airlines: list[str] = []
    cabin: Cabin = "economy"
    booking_url: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None


class HotelOptionData(BaseModel):
    """Expanded in M6 with normalized Amadeus inventory."""

    provider: str | None = None
    name: str
    entity_id: str | None = None
    nightly_price: Money | None = None
    total_price: Money | None = None
    rating: float | None = None
    rating_source: str | None = None
    area_name: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)


class TripDecisions(BaseModel):
    """The four decisions V1 tracks. Rental car / activities / reservations are
    added later only if they earn their place (spec section 8)."""

    destination: Decision[DestinationOption] | None = None
    flights: Decision[FlightOptionData] | None = None
    hotel_area: Decision[HotelAreaOption] | None = None
    hotel: Decision[HotelOptionData] | None = None

    def iter_decisions(self) -> list[tuple[str, Decision]]:
        """(field_name, decision) for every decision that exists."""
        out: list[tuple[str, Decision]] = []
        for name in ("destination", "flights", "hotel_area", "hotel"):
            value = getattr(self, name)
            if value is not None:
                out.append((name, value))
        return out
