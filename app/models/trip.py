from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import Pace, new_id, utcnow
from app.models.constraint import TripConstraint
from app.models.decision import TripDecisions
from app.models.entity import TripEntity
from app.models.evidence import EvidenceRecord
from app.models.itinerary import TripItinerary
from app.models.lock import LockRecord
from app.models.rejection import RejectionRecord
from app.models.validation import ValidationState

TripStatus = Literal[
    "draft",
    "planning",
    "ready",
    "in_trip",
    "completed",
    "archived",
]


class OriginSpec(BaseModel):
    city: str | None = None
    airport_codes: list[str] = []


class DestinationSpec(BaseModel):
    """Destination may legitimately be unknown - deciding it is part of the job."""

    city: str | None = None
    country: str | None = None
    region: str | None = None
    flexible: bool = True


class TripDates(BaseModel):
    start: date | None = None
    end: date | None = None
    flexible_days: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> "TripDates":
        if self.start and self.end and self.end < self.start:
            raise ValueError("trip end date is before start date")
        return self


class PartySpec(BaseModel):
    adults: int = Field(default=1, ge=1)
    children: int = Field(default=0, ge=0)
    rooms: int = Field(default=1, ge=1)

    @property
    def size(self) -> int:
        return self.adults + self.children


class BudgetSpec(BaseModel):
    total_per_person: float | None = None
    hotel_per_night: float | None = None
    flight_per_person: float | None = None
    currency: str = "USD"


class TripBrief(BaseModel):
    """This trip only. Long-term taste lives in TravelerProfile."""

    origin: OriginSpec = OriginSpec()
    destination: DestinationSpec = DestinationSpec()

    # IANA zone at the destination, e.g. "Asia/Tokyo". Itinerary datetimes are
    # naive wall-clock in this zone - which is how opening hours are published
    # and how people think about a trip. Taken from the Places timeZone field.
    timezone: str | None = None

    dates: TripDates = TripDates()
    party: PartySpec = PartySpec()
    budget: BudgetSpec = BudgetSpec()

    priorities: list[str] = []
    pace: Pace = "balanced"
    notes: str | None = None


class TripTraveler(BaseModel):
    traveler_id: str = Field(default_factory=lambda: new_id("trv"))

    # Links to a stored TravelerProfile, when one exists.
    profile_id: str | None = None

    name: str
    role: Literal["organizer", "member"] = "member"

    # Trip-specific preference overrides layered on the profile (M7).
    profile_overrides: dict[str, Any] = {}


class OpenQuestion(BaseModel):
    question_id: str = Field(default_factory=lambda: new_id("q"))
    question: str
    blocking: bool = False
    asked_at: datetime | None = None
    answered: bool = False
    answer: str | None = None


class Assumption(BaseModel):
    """Something the agent decided on the user's behalf, surfaced for correction."""

    assumption_id: str = Field(default_factory=lambda: new_id("asm"))
    statement: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    confirmed: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class TripMetadata(BaseModel):
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
    title: str | None = None


class TripState(BaseModel):
    """The source of truth for a trip. Conversation history is not.

    Every mutation goes through `apply_patch`, which enforces revision
    matching, locks, rejections, hard constraints and referential integrity.
    Nothing writes to this model directly.
    """

    schema_version: str = "1.0"

    trip_id: str = Field(default_factory=lambda: new_id("trip"))
    revision: int = 0

    status: TripStatus = "draft"

    brief: TripBrief = TripBrief()

    travelers: list[TripTraveler] = []
    constraints: list[TripConstraint] = []

    decisions: TripDecisions = TripDecisions()

    # entity_id -> entity. Itinerary items reference these by id.
    # Facts only: what Google asserts about a place. Community opinion lives in
    # `evidence`, so the two can never be mistaken for each other.
    entities: dict[str, TripEntity] = {}

    # evidence_id -> record. What people said, and where they said it, kept for
    # as long as the recommendation it justifies. DecisionOption.evidence_refs
    # points in here.
    evidence: dict[str, EvidenceRecord] = {}

    itinerary: TripItinerary = TripItinerary()

    locks: list[LockRecord] = []
    rejections: list[RejectionRecord] = []

    open_questions: list[OpenQuestion] = []
    assumptions: list[Assumption] = []

    validation: ValidationState = ValidationState()

    metadata: TripMetadata = TripMetadata()

    @classmethod
    def new(
        cls,
        *,
        title: str | None = None,
        created_by: str | None = None,
        brief: TripBrief | None = None,
        travelers: list[TripTraveler] | None = None,
    ) -> "TripState":
        now = utcnow()
        return cls(
            brief=brief or TripBrief(),
            travelers=travelers or [],
            metadata=TripMetadata(
                created_at=now,
                updated_at=now,
                created_by=created_by,
                title=title,
            ),
        )


class TripSummary(BaseModel):
    """Compact projection for list endpoints."""

    trip_id: str
    title: str | None = None
    status: TripStatus
    revision: int
    destination: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    traveler_count: int = 0
    updated_at: datetime

    @classmethod
    def from_state(cls, state: TripState) -> "TripSummary":
        return cls(
            trip_id=state.trip_id,
            title=state.metadata.title,
            status=state.status,
            revision=state.revision,
            destination=state.brief.destination.city or state.brief.destination.country,
            start_date=state.brief.dates.start,
            end_date=state.brief.dates.end,
            traveler_count=len(state.travelers),
            updated_at=state.metadata.updated_at,
        )
