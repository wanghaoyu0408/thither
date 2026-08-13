from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

ToolErrorCode = Literal[
    "provider_unavailable",
    "rate_limited",
    "invalid_request",
    "auth_failed",
    "timeout",
    # We stopped, not them. Distinct from `rate_limited` on purpose: saying a
    # provider throttled us when it was our own ceiling would be a small lie
    # told to the one reader who most needs the truth, and it would invite a
    # retry that cannot possibly succeed.
    "budget_exhausted",
    "unknown",
]


class ToolError(BaseModel):
    """Why a tool call produced nothing.

    Spec section 12's envelope has no error field, but section 38 requires the
    agent to distinguish "no result found" from "tool failed" - otherwise an
    empty neighbourhood and a dead API look identical, and the model fills the
    silence with a guess. This is a deliberate extension.
    """

    code: ToolErrorCode
    message: str
    provider: str | None = None
    retryable: bool = False


class ToolResult[T](BaseModel):
    """Everything an external-data tool returns (spec section 12).

    `generated_at` / `expires_at` exist so price- and availability-sensitive
    results can never be quoted as current without the agent knowing their age.
    """

    query_id: str = Field(default_factory=lambda: new_id("q"))
    source: str

    generated_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None

    results: list[T] = []
    warnings: list[str] = []

    error: ToolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def found_nothing(self) -> bool:
        """True only when the call succeeded and legitimately had no matches."""
        return self.error is None and not self.results
