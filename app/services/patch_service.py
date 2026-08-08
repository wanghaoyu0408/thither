"""The single sanctioned path for mutating a TripState.

There is deliberately no way to hand back a whole replacement state: the LLM
proposes operations, and every gate below runs before anything is committed.
Patches are atomic - on any failure the caller's state object is untouched.
"""

import copy
from typing import Any

from pydantic import ValidationError

from app.models.common import utcnow
from app.models.patch import PatchError, PatchResult, PatchWarning, TripPatch
from app.models.trip import TripState
from app.services import json_pointer as jp
from app.services.constraint_service import check_hard_constraints
from app.services.integrity_service import check_integrity
from app.services.lock_service import check_locks, check_locks_removed
from app.services.rejection_service import check_rejections

# Identity and bookkeeping the agent must never write.
PROTECTED_PATHS = ("/trip_id", "/revision", "/schema_version", "/metadata/created_at")


def _is_protected(path: str) -> bool:
    return any(path == p or path.startswith(f"{p}/") for p in PROTECTED_PATHS)


def _format_validation_error(error: dict[str, Any]) -> str:
    location = "/".join(str(part) for part in error.get("loc", ()))
    return f"{location}: {error.get('msg', 'invalid')}"


def _failure(revision: int, errors: list[PatchError], warnings: list[PatchWarning]) -> PatchResult:
    return PatchResult(applied=False, revision=revision, errors=errors, warnings=warnings)


def apply_patch(state: TripState, patch: TripPatch) -> PatchResult:
    """Validate and apply a patch, returning the new state or structured errors."""
    warnings: list[PatchWarning] = []

    # 1. Revision must match, or two clients are editing the same base.
    if patch.base_revision != state.revision:
        return _failure(
            state.revision,
            [
                PatchError(
                    code="REVISION_CONFLICT",
                    message=(
                        f"patch targets revision {patch.base_revision} "
                        f"but the trip is at revision {state.revision}"
                    ),
                )
            ],
            warnings,
        )

    before = state.model_dump(mode="json")
    working = copy.deepcopy(before)

    # 2. Refuse writes to identity fields before touching anything.
    protected = [
        PatchError(
            code="PROTECTED_PATH",
            message=f"{op.path} is managed by the server and cannot be patched",
            path=op.path,
            op_index=index,
        )
        for index, op in enumerate(patch.operations)
        if _is_protected(op.path)
    ]
    if protected:
        return _failure(state.revision, protected, warnings)

    # 3. Release the locks the user explicitly authorized releasing.
    unlock_ids = set(patch.unlock_targets)
    known_lock_ids = {lock.lock_id for lock in state.locks}
    for unknown in sorted(unlock_ids - known_lock_ids):
        warnings.append(
            PatchWarning(
                code="UNKNOWN_UNLOCK_TARGET",
                message=f"unlock_targets names {unknown!r}, which is not a lock on this trip",
            )
        )
    if unlock_ids:
        working["locks"] = [lock for lock in working["locks"] if lock["lock_id"] not in unlock_ids]
    surviving_locks = [lock for lock in state.locks if lock.lock_id not in unlock_ids]

    # 4. Apply the operations to a throwaway copy.
    for index, op in enumerate(patch.operations):
        try:
            if op.op == "set":
                jp.set_value(working, op.path, op.value)
            elif op.op == "add":
                jp.add_value(working, op.path, op.value)
            else:
                jp.remove_value(working, op.path)
        except jp.PointerError as exc:
            return _failure(
                state.revision,
                [
                    PatchError(
                        code="INVALID_POINTER",
                        message=str(exc),
                        path=op.path,
                        op_index=index,
                    )
                ],
                warnings,
            )

    # 5. Schema. Re-validating the whole state catches every type error for free.
    try:
        candidate = TripState.model_validate(working)
    except ValidationError as exc:
        return _failure(
            state.revision,
            [
                PatchError(
                    code="SCHEMA_INVALID",
                    message="the patched state does not satisfy the TripState schema",
                    details=[_format_validation_error(e) for e in exc.errors()],
                )
            ],
            warnings,
        )

    after = candidate.model_dump(mode="json")

    # 6. Locks, enforced from the pre-patch state so a patch cannot delete its
    #    own obstacle.
    lock_errors = check_locks_removed(surviving_locks, after)
    lock_errors += check_locks(before, after, surviving_locks)
    if lock_errors:
        return _failure(state.revision, lock_errors, warnings)

    # 7. Rejection memory.
    rejection_errors = check_rejections(before, after, state.rejections, patch.allow_rejected)
    if rejection_errors:
        return _failure(state.revision, rejection_errors, warnings)

    # 8. Hard constraints. `not_checkable` is reported, never assumed to pass.
    violations: list[PatchError] = []
    for check in check_hard_constraints(candidate):
        if check.status == "violated":
            violations.append(
                PatchError(
                    code="CONSTRAINT_VIOLATION",
                    message=check.message or "hard constraint violated",
                    constraint_id=check.constraint_id,
                )
            )
        elif check.status == "not_checkable":
            warnings.append(
                PatchWarning(
                    code="CONSTRAINT_NOT_CHECKABLE",
                    message=check.message or "constraint could not be verified",
                    constraint_id=check.constraint_id,
                )
            )
    if violations:
        return _failure(state.revision, violations, warnings)

    # 9. Referential integrity.
    problems = check_integrity(candidate)
    if problems:
        return _failure(
            state.revision,
            [
                PatchError(
                    code="INTEGRITY_ERROR",
                    message="the patched state has broken references",
                    details=problems,
                )
            ],
            warnings,
        )

    # 10. Commit.
    candidate.revision = state.revision + 1
    candidate.metadata.updated_at = utcnow()

    return PatchResult(
        applied=True,
        revision=candidate.revision,
        state=candidate,
        warnings=warnings,
    )
