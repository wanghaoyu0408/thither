"""The pieces of an intake: what to plan, and what we still need to ask.

These live apart from `TripBrief` so `app/models/trip.py` can import them and
compose `TripIntake` around a real `TripBrief` snapshot without a cycle.

Intake questions are deliberately *not* `OpenQuestion`. That model carries the
blocking preference conflicts, and `unresolved_blocking` settles one by looking
for a conflict id inside the answer text - semantics an intake answer would
break the moment someone typed a sentence containing one.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

IntakeStatus = Literal["collecting", "awaiting_confirmation", "confirmed"]

# What the traveller wants done about one part of the trip.
#   plan             - go and research it
#   already_arranged - booked or otherwise sorted; plan around it, never shop
#   not_needed       - does not apply to this trip
#   unknown          - nobody has said, and we must not guess
ScopeState = Literal["plan", "already_arranged", "not_needed", "unknown"]

QuestionKind = Literal["single_choice", "multi_choice", "text", "dates"]

# The parts that cost a provider call to shop for, and are therefore the ones
# whose scope has to be settled before research starts. The rest are what this
# product does by default.
SHOPPABLE = ("flights", "lodging")

SCOPE_LABELS: dict[str, str] = {
    "flights": "Flights",
    "lodging": "Hotel",
    "rental_car": "Rental car",
    "activities": "Activities",
    "restaurants": "Restaurants",
    "daily_routes": "Daily routes",
}


class PlanScope(BaseModel):
    """What the agent has been asked to do, part by part.

    `flights` and `lodging` start `unknown` on purpose: whether someone has
    already booked them is a fact about their trip, and assuming either way
    means either wasted searches or a plan with no way to get there.

    The rest default to `plan` because that is what this product is for. That
    is a product default, not a claim about the traveller - and any of them can
    be turned off.
    """

    flights: ScopeState = "unknown"
    lodging: ScopeState = "unknown"
    rental_car: ScopeState = "unknown"

    activities: ScopeState = "plan"
    restaurants: ScopeState = "plan"
    daily_routes: ScopeState = "plan"

    def state_of(self, part: str) -> ScopeState:
        return getattr(self, part, "unknown")

    def parts(self, *states: ScopeState) -> list[str]:
        """Which parts are in any of these states, in declaration order."""
        return [name for name in SCOPE_LABELS if self.state_of(name) in states]


class ClarificationChoice(BaseModel):
    value: str
    label: str
    description: str | None = None


class ClarificationQuestion(BaseModel):
    """One thing we need to know, asked once.

    Structured where a structure helps, but `answer` is always available: a
    multiple-choice question whose options do not fit is worse than a plain one,
    so every question can be answered in words.
    """

    question_id: str = Field(default_factory=lambda: new_id("clq"))

    question: str
    kind: QuestionKind = "text"

    # Which outstanding requirement this is for - "dates", "scope.flights".
    # A question that does not name one we are waiting on is not asked.
    requirement_id: str | None = None

    # The brief path it fills, e.g. "/brief/dates". For showing the answer next
    # to what it changed; never used to write blindly.
    fills: str | None = None

    choices: list[ClarificationChoice] = []

    # Chosen values, for the structured kinds.
    values: list[str] = []
    # What they actually typed, whether or not they also picked something.
    answer: str | None = None

    asked_at: datetime = Field(default_factory=utcnow)
    answered_at: datetime | None = None

    @property
    def answered(self) -> bool:
        return self.answered_at is not None

    def summary(self) -> str:
        """The answer as one line, whichever way it was given."""
        if self.answer and self.values:
            return f"{', '.join(self.values)} - {self.answer}"
        if self.values:
            return ", ".join(self.values)
        return self.answer or ""
