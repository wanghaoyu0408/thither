from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

RejectionTargetKind = Literal[
    "entity",
    "decision_option",
    "destination",
    "hotel_area",
    # A dismissed learning hypothesis, stored on the profile with
    # scope="profile". Safe to add: check_rejections never branches on kind.
    "hypothesis",
]


class RejectionRecord(BaseModel):
    """Remembers that the user said no, so the agent stops re-suggesting it.

    Scope `trip` applies to this trip only. Scope `profile` is durable across
    trips and must come from explicit user intent (spec section 9).
    """

    rejection_id: str = Field(default_factory=lambda: new_id("rej"))

    target_kind: RejectionTargetKind
    # entity_id or option_id
    target_id: str

    # Human-readable name, kept so the agent can explain "you passed on X".
    label: str | None = None

    scope: Literal["trip", "profile"] = "trip"

    reason: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
