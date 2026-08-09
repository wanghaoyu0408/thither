from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import Cabin, LatLng, Money, new_id, utcnow
from app.models.flight import BaggageAllowance, FlightSlice
from app.models.group import GroupScore
from app.models.hotel import HotelPriceQuote, HotelRating

OptionStatus = Literal["candidate", "shortlisted", "selected", "rejected"]
DecisionStatus = Literal["researching", "shortlisted", "selected", "locked"]


class DecisionScore(BaseModel):
    """Dimensional scores are kept so a recommendation stays explainable.

    `total` is never treated as objective truth - it is a ranking aid whose
    components must be quotable back to the user (spec section 23).
    """

    total: float
    dimensions: dict[str, float] = {}
    # Share of the intended weight that had data behind it. 1.0 means every
    # dimension was known; a low figure means the total rests on very little,
    # which is worth knowing before quoting it.
    coverage: float = 1.0
    notes: str | None = None


class DecisionOption[T](BaseModel):
    option_id: str = Field(default_factory=lambda: new_id("opt"))

    data: T

    status: OptionStatus = "candidate"

    score: DecisionScore | None = None

    # How the group as a whole scores this, kept beside `score` rather than
    # replacing it: the dimensional breakdown says *why* an option is good, and
    # this says *for whom*. Neither answers the other's question.
    group_score: GroupScore | None = None

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

    # Set when the preferences this was decided on have since changed. Flagged,
    # never rebuilt: whether a decision is worth redoing is the traveller's call,
    # and silently rescoring a settled choice would be the "why did my trip
    # change?" problem this project exists to avoid.
    stale_reason: str | None = None

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
    """A priced hotel from a hotel provider.

    `ratings` is a list rather than the single `rating` + `rating_source` pair
    this started as. One float is how a five-star category ends up compared
    against a 4.3-from-2,300-reviews as though they measured the same thing
    (spec section 16); a list of typed, sourced ratings makes that impossible to
    do by accident.

    `quotes` is a list for the same reason: Google Hotels lists one property at
    several vendors at different rates, so "the price" does not exist.
    """

    provider: str
    offer_ref: str | None = None

    # False for a provider's test environment. Same flag flights carry, and no
    # default for the same reason: forgetting to say must not be possible.
    live_mode: bool

    name: str
    entity_id: str | None = None

    lat: float | None = None
    lng: float | None = None

    area_name: str | None = None
    # Recorded when the neighbourhood step was deliberately skipped, so the
    # trip keeps the trace of that decision.
    area_bypass_reason: str | None = None

    # What ranking and display use. Once vendor quotes exist this is the
    # cheapest one that can be attributed to a named source.
    nightly_price: Money | None = None
    total_price: Money | None = None

    # The provider's own headline rate, kept separately because nobody can say
    # who is offering it. Google Hotels' "from $70" can come from a paid
    # placement that no listed booking site matches, so it is never quietly
    # treated as a price someone could go and pay.
    headline_nightly: Money | None = None

    quotes: list[HotelPriceQuote] = []

    ratings: list[HotelRating] = []

    room_description: str | None = None
    board_type: str | None = None
    amenities: list[str] = []

    refundable: bool | None = None
    free_cancellation_until: datetime | None = None

    # Real travel minutes to the trip's anchor places, keyed by entity_id.
    # Never a straight-line estimate (spec section 21).
    route_minutes: dict[str, float] = {}
    # How those minutes were travelled. Carried because Google has no transit
    # data in some countries, so a figure asked for as transit may have been
    # measured by car - and thirty minutes' drive is not thirty minutes' train.
    route_mode: str | None = None
    distance_to_area_km: float | None = None

    # Somewhere to look. No provider under this interface can book.
    search_url: str | None = None

    observed_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None

    def rating_of(self, kind: str) -> HotelRating | None:
        return next((rating for rating in self.ratings if rating.type == kind), None)

    @property
    def user_rating(self) -> HotelRating | None:
        return self.rating_of("user_rating")

    @property
    def star_category(self) -> HotelRating | None:
        return self.rating_of("star_category")

    @property
    def cheapest_quote(self) -> HotelPriceQuote | None:
        priced = [quote for quote in self.quotes if quote.nightly is not None]
        return min(priced, key=lambda quote: quote.nightly.amount) if priced else None

    def headline_gap(self) -> float | None:
        """How much cheaper the unattributed headline is than any real quote.

        `None` when there is nothing to compare. A positive number means the
        advertised rate is not matched by any booking site we can name, which
        the traveller should hear rather than discover at checkout.
        """
        cheapest = self.cheapest_quote
        if self.headline_nightly is None or cheapest is None or cheapest.nightly is None:
            return None
        return round(cheapest.nightly.amount - self.headline_nightly.amount, 2)

    def mean_route_minutes(self) -> float | None:
        if not self.route_minutes:
            return None
        return sum(self.route_minutes.values()) / len(self.route_minutes)


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
