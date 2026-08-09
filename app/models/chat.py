from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

Role = Literal["user", "assistant"]


class ChatMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    role: Role
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    # How many earlier turns to show the model. TripState carries the facts, so
    # this is only for conversational continuity (spec section 2).
    history_turns: int = Field(default=6, ge=0, le=40)


class ToolCallSummary(BaseModel):
    name: str
    milliseconds: int
    ok: bool
    detail: str = ""


class ChatResponse(BaseModel):
    reply: str

    trip_id: str
    revision_before: int
    revision_after: int
    changed_state: bool

    tools_called: list[ToolCallSummary] = []
    patches_applied: int = 0
    patches_rejected: list[list[str]] = []

    iterations: int = 0
    # True when the agent was still working when the round cap hit. Distinct
    # from `error`: the turn did not fail, it was cut short, and what it applied
    # before that is complete.
    hit_iteration_limit: bool = False
    # True when the traveller stopped the turn. Same contract as the round cap:
    # whatever was applied before the stop is complete and stays.
    cancelled: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    error: str | None = None

    def as_log(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"reply"})
