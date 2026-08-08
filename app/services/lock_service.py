"""Lock enforcement.

A lock names a stable id, not a JSON path, so it keeps protecting the same
object when array indices shift. Enforcement is a before/after diff of the
target's serialized content: whichever pointer a patch used to reach the
object, a change to it is caught.
"""

import json
from typing import Any

from app.models.lock import LockRecord
from app.models.patch import PatchError

# (target_kind, target_id)
LockKey = tuple[str, str]


def collect_lock_targets(state: dict[str, Any]) -> dict[LockKey, Any]:
    """Index every lockable object in a serialized TripState by (kind, id)."""
    targets: dict[LockKey, Any] = {}

    for decision in (state.get("decisions") or {}).values():
        if isinstance(decision, dict) and decision.get("decision_id"):
            targets[("decision", decision["decision_id"])] = decision

    for day in (state.get("itinerary") or {}).get("days") or []:
        targets[("itinerary_day", str(day.get("date")))] = day
        for item in day.get("items") or []:
            if item.get("item_id"):
                targets[("itinerary_item", item["item_id"])] = item

    for entity_id, entity in (state.get("entities") or {}).items():
        targets[("entity", entity_id)] = entity

    for constraint in state.get("constraints") or []:
        if constraint.get("id"):
            targets[("constraint", constraint["id"])] = constraint

    return targets


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def check_locks(
    before: dict[str, Any],
    after: dict[str, Any],
    locks: list[LockRecord],
) -> list[PatchError]:
    """Report every lock whose target was removed or modified.

    `locks` must be the locks that survive the patch (the trip's locks minus
    the ones the user explicitly released), taken from the *pre-patch* state -
    reading them from the post-patch state would let a patch delete a lock and
    then edit what it protected.
    """
    before_targets = collect_lock_targets(before)
    after_targets = collect_lock_targets(after)

    errors: list[PatchError] = []
    for lock in locks:
        key = (lock.target_kind, lock.target_id)
        original = before_targets.get(key)
        if original is None:
            # Lock was already dangling; integrity_service reports that separately.
            continue

        updated = after_targets.get(key)
        if updated is None:
            errors.append(
                PatchError(
                    code="LOCK_VIOLATION",
                    message=(
                        f"locked {lock.target_kind} {lock.target_id!r} was removed "
                        f"(locked because: {lock.reason})"
                    ),
                    lock_id=lock.lock_id,
                )
            )
            continue

        if _canonical(original) != _canonical(updated):
            errors.append(
                PatchError(
                    code="LOCK_VIOLATION",
                    message=(
                        f"locked {lock.target_kind} {lock.target_id!r} was modified "
                        f"(locked because: {lock.reason})"
                    ),
                    lock_id=lock.lock_id,
                )
            )

    return errors


def check_locks_removed(
    surviving: list[LockRecord],
    after: dict[str, Any],
) -> list[PatchError]:
    """Reject patches that delete a lock record without an explicit unlock.

    Without this, a patch could `remove /locks/0` and leave the target itself
    untouched - dropping the protection silently and clearing the way for the
    next patch.
    """
    remaining_ids = {lock.get("lock_id") for lock in after.get("locks") or []}
    return [
        PatchError(
            code="LOCK_VIOLATION",
            message=(
                f"lock {lock.lock_id!r} on {lock.target_kind} {lock.target_id!r} was removed "
                "without naming it in unlock_targets"
            ),
            lock_id=lock.lock_id,
        )
        for lock in surviving
        if lock.lock_id not in remaining_ids
    ]
