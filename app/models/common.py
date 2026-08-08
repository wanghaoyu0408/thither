import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    """Short, readable, collision-safe identifier."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


# A 0.0-1.0 weight expressing how much a traveler cares about something.
Importance = Annotated[float, Field(ge=0.0, le=1.0)]

# How much freedom the planner has to move an itinerary item in time.
#   fixed    - a reservation; do not move
#   window   - must happen inside a bounded window (e.g. a timed museum ticket)
#   flexible - can float within the day
TimeFlexibility = Literal["fixed", "window", "flexible"]

Pace = Literal["relaxed", "balanced", "packed"]

Cabin = Literal["economy", "premium_economy", "business"]

# HH:MM, 24-hour.
ClockTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class Money(BaseModel):
    amount: float
    currency: str = "USD"


class LatLng(BaseModel):
    lat: Annotated[float, Field(ge=-90.0, le=90.0)]
    lng: Annotated[float, Field(ge=-180.0, le=180.0)]
