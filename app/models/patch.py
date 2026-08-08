from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.trip import TripState

PatchOpKind = Literal["set", "add", "remove"]

PatchErrorCode = Literal[
    "REVISION_CONFLICT",
    "PROTECTED_PATH",
    "INVALID_POINTER",
    "SCHEMA_INVALID",
    "LOCK_VIOLATION",
    "REJECTION_VIOLATION",
    "CONSTRAINT_VIOLATION",
    "INTEGRITY_ERROR",
    "SCOPE_VIOLATION",
]


class PatchScope(BaseModel):
    """Restricts a patch to one part of the trip.

    Local replanning is a core product promise - "Day 3 is too busy" must not
    quietly reshuffle days 1, 2, 4 and 5 (spec section 29). Scope makes that a
    server-enforced invariant rather than something the model is trusted to
    respect.

    Deliberately semantic rather than a path prefix: `/itinerary/days/2` stops
    meaning day 3 the moment an earlier day is removed. Enforcement is a
    canonical before/after diff of everything outside the target, the same
    technique locks use.

    Entities may be *added* under scope - a replan often needs a place the trip
    has not seen - but never modified or removed.
    """

    kind: Literal["itinerary_day"]
    # ISO date for kind="itinerary_day".
    target_id: str


class PatchOperation(BaseModel):
    """A single mutation, addressed by RFC 6901 JSON Pointer.

    set    - replace the value at an existing location, or create an object key
    add    - insert into an array (index, or "/-" to append), or create a key
    remove - delete an array element or object key
    """

    op: PatchOpKind
    path: str
    value: Any = None


class TripPatch(BaseModel):
    """The only sanctioned way to change a TripState.

    The LLM proposes one of these; the server validates and applies it. There
    is deliberately no way to hand back a whole replacement state.
    """

    base_revision: int = Field(ge=0)
    reason: str
    operations: list[PatchOperation] = Field(min_length=1)

    actor: Literal["user", "agent", "system"] = "agent"

    # Confines the patch to one day, enforced server-side. See PatchScope.
    scope: PatchScope | None = None

    # lock_ids the user explicitly authorized releasing in this patch.
    unlock_targets: list[str] = []

    # Previously-rejected target_ids the user explicitly asked to reconsider.
    allow_rejected: list[str] = []


class ApplyTripPatchInput(TripPatch):
    """Tool-facing shape (spec section 28). The HTTP API takes trip_id from the path."""

    trip_id: str


class PatchError(BaseModel):
    code: PatchErrorCode
    message: str

    path: str | None = None
    op_index: int | None = None
    lock_id: str | None = None
    constraint_id: str | None = None

    details: list[str] = []


class PatchWarning(BaseModel):
    code: str
    message: str
    constraint_id: str | None = None


class PatchResult(BaseModel):
    """Patches are atomic: on failure `applied` is False, `revision` is
    unchanged, and nothing was written."""

    applied: bool
    revision: int

    state: TripState | None = None

    errors: list[PatchError] = []
    warnings: list[PatchWarning] = []
