from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

LockTargetKind = Literal[
    "decision",
    "itinerary_item",
    "itinerary_day",
    "entity",
    "constraint",
]


class LockRecord(BaseModel):
    """Marks something the agent must not change without explicit user consent.

    Locks reference a *stable id*, never a JSON path: a path-based lock breaks
    the moment an array index shifts, and would silently stop protecting the
    thing it was created for.

    Enforcement is a before/after diff of the target's serialized content, so
    it catches mutation regardless of which pointer the patch used to reach it.
    Releasing a lock requires naming its `lock_id` in `TripPatch.unlock_targets`.
    """

    lock_id: str = Field(default_factory=lambda: new_id("lock"))

    target_kind: LockTargetKind
    # decision_id | item_id | ISO date | entity_id | constraint id
    target_id: str

    reason: str

    locked_by: Literal["user", "agent"] = "user"

    created_at: datetime = Field(default_factory=utcnow)
