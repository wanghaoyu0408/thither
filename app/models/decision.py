from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import Cabin, LatLng, Money, new_id, utcnow
from app.models.flight import BaggageAllowance, FlightSlice

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
    """A priced itinerary from a flight provider.

    `live_mode` is required and has no default on purpose. A sandbox fare shown
    as a real one would be the exact confident wrongness this project exists to
    avoid, so every construction site has to state which it is, and the answer
    persists into TripState rather than living only in the session that fetched
    it.
    """

    provider: str
    offer_ref: str

    # False for provider sandbox data. Never present such an offer as a real
    # flight, a real price, or something the user could book.
    live_mode: bool

    price: Money
    price_per_person: Money | None = None

    origin: str
    destination: str

    slices: list[FlightSlice] = []

    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    duration_minutes: int | None = None
    stops: int | None = None

    airlines: list[str] = []
    cabin: Cabin = "economy"

    baggage: BaggageAllowance | None = None
    refundable: bool | None = None
    changeable: bool | None = None

    # Where to go and look. Duffel is an API, not a shop, so this is a search
    # link and is named as one - calling it a booking URL would promise
    # something the system deliberately does not do.
    search_url: str | None = None

    observed_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None

    @property
    def is_nonstop(self) -> bool:
        return self.stops == 0

    @property
    def connection_airports(self) -> list[str]:
        return [airport for slice_ in self.slices for airport in slice_.connection_airports]

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        reference = now or utcnow()
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=reference.tzinfo)
        return expires <= reference


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


class PlaceOption(BaseModel):
    """A shortlisted place.

    Deliberately holds no facts: name, rating and hours live once in
    `TripState.entities` (spec section 10), and the ranking lives in
    `DecisionOption.score`. This carries only the link and the reasoning.
    """

    entity_id: str
    # What the shortlist is for: "dinner", "cafe", "activity", "breakfast"...
    purpose: str
    why: str | None = None


SINGLETON_DECISIONS = ("destination", "flights", "hotel_area", "hotel")


class TripDecisions(BaseModel):
    """The four singleton decisions from spec section 8, plus keyed place
    shortlists - restaurants and attractions are chosen many times per trip, so
    they need a dict rather than a field."""

    destination: Decision[DestinationOption] | None = None
    flights: Decision[FlightOptionData] | None = None
    hotel_area: Decision[HotelAreaOption] | None = None
    hotel: Decision[HotelOptionData] | None = None

    # Keyed by purpose slug, e.g. "dinner_day1", "shibuya_cafes".
    place_shortlists: dict[str, Decision[PlaceOption]] = {}

    def iter_decisions(self) -> list[tuple[str, Decision]]:
        """(name, decision) for every decision that exists, shortlists included.

        Anything omitted here silently stops being integrity-checked, so the
        shortlists must be walked too.
        """
        out: list[tuple[str, Decision]] = []
        for name in SINGLETON_DECISIONS:
            value = getattr(self, name)
            if value is not None:
                out.append((name, value))
        for key, decision in self.place_shortlists.items():
            out.append((f"place_shortlists.{key}", decision))
        return out
