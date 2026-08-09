from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.arrival import ArrivalContext
from app.models.common import Pace, new_id, utcnow
from app.models.constraint import TripConstraint
from app.models.decision import TripDecisions
from app.models.entity import TripEntity
from app.models.evidence import EvidenceRecord
from app.models.group import TravelerPreferences
from app.models.intake import ClarificationQuestion, IntakeStatus, PlanScope
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
    """When the trip is, or the window it has to fall in.

    Two ways to be specified, and the difference matters. `start`/`end` are the
    decided dates. `earliest`/`latest` plus `duration_nights` describe a trip
    that is real but not yet pinned - "four days sometime in October" - which
    used to have nowhere to live, so it was either invented into concrete dates
    or lost.
    """

    start: date | None = None
    end: date | None = None
    flexible_days: int = Field(default=0, ge=0)

    # The window a not-yet-dated trip must fall inside.
    earliest: date | None = None
    latest: date | None = None
    duration_nights: int | None = Field(default=None, ge=1)

    @property
    def decided(self) -> bool:
        return self.start is not None and self.end is not None

    @property
    def bounded(self) -> bool:
        """Enough to search on without pretending to know the dates."""
        return self.duration_nights is not None and (
            self.earliest is not None or self.latest is not None
        )

    @model_validator(mode="after")
    def _check_order(self) -> "TripDates":
        if self.start and self.end and self.end < self.start:
            raise ValueError("trip end date is before start date")
        if self.earliest and self.latest and self.latest < self.earliest:
            raise ValueError("trip window ends before it starts")
        return self


class PartySpec(BaseModel):
    """Who is going, or nothing at all.

    Every count is optional and defaults to None rather than to a number. An
    unknown party size used to default to one adult, which made "we have not
    said yet" indistinguishable from "I am travelling alone" - and the flight
    search then priced it for one. A count nobody gave is not a count.
    """

    adults: int | None = Field(default=None, ge=1)
    children: int | None = Field(default=None, ge=0)
    rooms: int | None = Field(default=None, ge=1)

    @property
    def size(self) -> int | None:
        """Total travellers, or None while the adult count is unknown."""
        if self.adults is None:
            return None
        return self.adults + (self.children or 0)


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

    # What the traveller has asked us to plan, and what is already arranged.
    # Part of the brief rather than of the intake record, because it stays true
    # after confirmation and is what stops the agent shopping for a flight
    # somebody already holds a ticket for.
    scope: PlanScope = PlanScope()


class TripTraveler(BaseModel):
    traveler_id: str = Field(default_factory=lambda: new_id("trv"))

    # Links to a stored TravelerProfile, when one exists.
    profile_id: str | None = None

    name: str
    role: Literal["organizer", "member"] = "member"

    # Trip-specific overrides, layered *over* the profile. Dotted or nested,
    # e.g. {"flight": {"price_importance": 0.9}}. Authoritative: what someone
    # says about this trip beats what their profile says in general.
    profile_overrides: dict[str, Any] = {}

    # The resolved snapshot planning actually reads. Never the live profile row:
    # a trip has to stay explainable after the profile behind it has moved on.
    # Filled by preference_service.resolve; None means nobody has resolved this
    # traveler yet, and the rankers fall back to neutral defaults saying so.
    preferences: TravelerPreferences | None = None


class OpenQuestion(BaseModel):
    question_id: str = Field(default_factory=lambda: new_id("q"))
    question: str
    blocking: bool = False
    asked_at: datetime | None = None
    answered: bool = False
    answer: str | None = None


class TripIntake(BaseModel):
    """Whether we know enough to start, and whether the traveller has said so.

    A separate axis from `TripStatus`, which is about where the trip is in its
    life. A trip can be `draft` and still have a confirmed brief.

    The snapshot exists so "what did I agree to?" has an answer that later edits
    cannot quietly rewrite. It is taken at confirmation and never updated: if the
    brief moves far enough to matter, intake reopens and a new one is taken.
    """

    status: IntakeStatus = "collecting"

    questions: list[ClarificationQuestion] = []

    confirmed_brief: "TripBrief | None" = None
    confirmed_revision: int | None = None
    confirmed_at: datetime | None = None

    @property
    def unanswered(self) -> list[ClarificationQuestion]:
        return [question for question in self.questions if not question.answered]


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

    # What we still need to ask before researching, and whether the traveller
    # has confirmed the brief. Gate lives in app/services/intake_service.py.
    intake: TripIntake = TripIntake()

    # How you reach each place and where you leave the car, keyed by entity.
    # A registry like `entities` and `evidence`: facts about places, shared by
    # every day that visits one, and additive under a day-scoped patch.
    arrival: dict[str, ArrivalContext] = {}

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
