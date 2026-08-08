from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

ConstraintCategory = Literal[
    "budget",
    "flight",
    "hotel",
    "food",
    "mobility",
    "schedule",
    "activity",
    "transport",
    "other",
]

ConstraintScope = Literal[
    "trip",
    "traveler",
    "day",
    "flight",
    "hotel",
    "restaurant",
]

ConstraintSource = Literal[
    "user_explicit",
    "profile",
    "agent_inferred",
]


class TripConstraint(BaseModel):
    """A first-class requirement on the trip.

    `hard` constraints can never be traded off - a patch that violates one is
    rejected. `soft` constraints feed ranking only.
    """

    id: str = Field(default_factory=lambda: new_id("con"))

    category: ConstraintCategory
    description: str

    type: Literal["hard", "soft"]
    scope: ConstraintScope = "trip"

    traveler_id: str | None = None

    # Ranking weight for soft constraints.
    weight: float | None = None

    # Structured payload the deterministic checkers read, e.g.
    # {"max_total_per_person": 2500, "currency": "USD"}.
    # Free-text-only constraints stay `not_checkable` until a checker exists.
    params: dict[str, Any] = {}

    source: ConstraintSource
    confirmed: bool = False

    created_at: datetime = Field(default_factory=utcnow)
