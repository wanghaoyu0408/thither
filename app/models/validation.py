from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Severity = Literal["error", "warning", "info"]


class ValidationIssue(BaseModel):
    severity: Severity

    # e.g. "schedule_overlap", "travel_time_infeasible", "closed_at_visit_time"
    type: str

    item_ids: list[str] = []

    message: str

    suggested_fix: str | None = None


class ValidationState(BaseModel):
    """The validator reports; it never mutates the trip (spec section 27).

    `stale` means the trip changed since the last validation run.
    """

    status: Literal["unvalidated", "ok", "warnings", "errors", "stale"] = "unvalidated"

    issues: list[ValidationIssue] = []

    validated_revision: int | None = None
    validated_at: datetime | None = None
