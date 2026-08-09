import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field


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


# What a provider actually asserted about a fact, kept as three states because
# two cannot say it. Google returns `servesVegetarianFood: false` for every
# pizzeria in Shibuya (measured live: 1/8 attested), and every pizzeria serves
# a margherita - so a provider's False usually means "not attested", never
# "confirmed absent". A bool|None collapses that distinction; this type keeps
# it, and leaves room for a future provider that genuinely can assert the
# negative.
#
#   confirmed_true   the provider positively attested it
#   confirmed_false  the provider positively denied it (no current provider can)
#   unknown          nobody said - which is NOT a denial
Attestation = Literal["confirmed_true", "confirmed_false", "unknown"]


def as_attestation(value: Any) -> Attestation:
    """Coerce legacy and provider shapes into the tri-state.

    `True` was the only trustworthy signal in the old bool|None encoding, so it
    maps to confirmed_true; `False` and `None` both meant "not attested" and map
    to unknown. Stored TripState JSON from before this type keeps validating.
    A provider that can assert the negative passes "confirmed_false" explicitly.
    """
    if value in ("confirmed_true", "confirmed_false", "unknown"):
        return value
    if value is True:
        return "confirmed_true"
    return "unknown"


# Field alias: apply the coercion wherever the tri-state is stored.
AttestationField = Annotated[Attestation, BeforeValidator(as_attestation)]


def as_attestation_map(value: Any) -> dict[str, Attestation]:
    """Same coercion for keyed attestations, e.g. accessibility options."""
    if not isinstance(value, dict):
        return {}
    return {key: as_attestation(item) for key, item in value.items()}


AttestationMap = Annotated[dict[str, Attestation], BeforeValidator(as_attestation_map)]


class Money(BaseModel):
    amount: float
    currency: str = "USD"


class LatLng(BaseModel):
    lat: Annotated[float, Field(ge=-90.0, le=90.0)]
    lng: Annotated[float, Field(ge=-180.0, le=180.0)]
