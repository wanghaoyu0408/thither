from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import Cabin, ClockTime


class BaggageAllowance(BaseModel):
    """What the fare includes. None means the airline did not say, not zero."""

    checked: int | None = None
    cabin: int | None = None


class FlightSegment(BaseModel):
    """One flight. A leg with a connection is two of these."""

    origin: str
    destination: str

    # Local wall-clock at each airport, as the airline publishes them.
    departing_at: datetime
    arriving_at: datetime

    marketing_carrier: str
    marketing_carrier_name: str | None = None
    operating_carrier: str | None = None

    flight_number: str | None = None
    aircraft: str | None = None

    duration_minutes: int | None = None
    # Fuel or crew stops that do not involve changing aircraft. Rare, and they
    # still cost the traveller time, so they count toward the stop total.
    technical_stops: int = 0


class FlightSlice(BaseModel):
    """One direction of travel. A return trip has two."""

    origin: str
    destination: str

    departing_at: datetime
    arriving_at: datetime

    duration_minutes: int | None = None
    segments: list[FlightSegment] = []

    @property
    def stops(self) -> int:
        """Duffel gives no stop count; it is the connections plus any tech stops."""
        if not self.segments:
            return 0
        return len(self.segments) - 1 + sum(segment.technical_stops for segment in self.segments)

    @property
    def is_nonstop(self) -> bool:
        return self.stops == 0

    @property
    def connection_airports(self) -> list[str]:
        return [segment.destination for segment in self.segments[:-1]]


class SearchFlightsInput(BaseModel):
    """Spec section 15.

    `origins` and `destinations` are plural because comparing SFO, SJC and OAK
    is the point. A Duffel slice holds exactly one of each, so the service fans
    this out into one request per pair.
    """

    origins: list[str] = Field(min_length=1)
    destinations: list[str] = Field(min_length=1)

    departure_date: date
    return_date: date | None = None

    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)

    cabin: Cabin = "economy"
    max_stops: int | None = Field(default=None, ge=0, le=3)

    earliest_departure: ClockTime | None = None
    latest_departure: ClockTime | None = None

    max_price_per_person: float | None = None
    currency: str = "USD"

    limit: int = Field(default=30, ge=1, le=100)

    @model_validator(mode="after")
    def _check_dates(self) -> "SearchFlightsInput":
        if self.return_date and self.return_date < self.departure_date:
            raise ValueError("return_date is before departure_date")
        return self

    @property
    def pair_count(self) -> int:
        return len(self.origins) * len(self.destinations)

    @property
    def passengers(self) -> int:
        return self.adults + self.children


class SearchAirportsInput(BaseModel):
    """Spec section 14."""

    location: str = Field(min_length=1)
    max_ground_travel_minutes: int | None = Field(default=None, ge=1)

    radius_km: float = Field(default=120.0, gt=0, le=500)
    limit: int = Field(default=6, ge=1, le=20)

    # Ground travel comes from the Routes API. Turning it off saves quota and
    # loses the only honest basis for comparing nearby airports.
    with_ground_travel: bool = True

    # Secondary and general-aviation fields are marked as having scheduled
    # service in the source data even when no airline serves them, so they are
    # included only when no major airport is in range - or on request.
    include_secondary_airports: bool = False


class AirportOption(BaseModel):
    iata: str
    icao: str | None = None

    name: str
    city: str | None = None
    country: str | None = None

    lat: float
    lng: float

    # Straight line: a cheap filter, never presented as travel time.
    distance_km: float | None = None

    # Real driving minutes from the Routes API, or None when it was not looked
    # up. Never estimated - spec section 21.
    ground_travel_minutes: float | None = None
    ground_travel_source: Literal["routes_api", "not_looked_up", "unavailable"] = "not_looked_up"

    def describe(self) -> str:
        where = f" ({self.city})" if self.city else ""
        if self.ground_travel_minutes is not None:
            return f"{self.iata}{where}, {self.ground_travel_minutes:.0f} min drive"
        return f"{self.iata}{where}"
