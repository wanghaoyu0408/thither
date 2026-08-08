"""Scope enforcement for local replanning.

"Day 3 is too busy, make it easier" must change day 3 and nothing else
(spec section 29). Rather than trust the model to keep its hands still, a
scoped patch is checked by canonical before/after diff: everything outside the
target has to be byte-identical, or the patch is rejected.

The same reasoning as locks applies to the addressing: a path prefix like
`/itinerary/days/2` stops meaning "day 3" as soon as an earlier day is removed,
so the scope names the date and the diff does the work.
"""

import json
from typing import Any

from app.models.patch import PatchError, PatchScope

# Keys that may legitimately differ under any scope.
#   metadata   - the server stamps updated_at
#   validation - re-running the validator is not a trip change
_ALWAYS_MUTABLE = frozenset({"metadata", "validation", "revision"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _days_by_date(state: dict[str, Any]) -> dict[str, Any]:
    return {str(day.get("date")): day for day in (state.get("itinerary") or {}).get("days") or []}


def check_scope(
    before: dict[str, Any],
    after: dict[str, Any],
    scope: PatchScope,
) -> list[PatchError]:
    """Report everything the patch touched that its scope did not allow."""
    if scope.kind != "itinerary_day":
        return [
            PatchError(
                code="SCOPE_VIOLATION",
                message=f"unsupported scope kind {scope.kind!r}",
            )
        ]

    errors: list[PatchError] = []

    before_days = _days_by_date(before)
    after_days = _days_by_date(after)

    if scope.target_id not in before_days:
        return [
            PatchError(
                code="SCOPE_VIOLATION",
                message=(
                    f"scope targets day {scope.target_id}, which is not in the itinerary "
                    f"(days present: {sorted(before_days) or 'none'})"
                ),
            )
        ]

    # The set of days must not change, or a "day 3 only" patch could delete
    # day 4 and leave the diff below with nothing to compare.
    added = sorted(set(after_days) - set(before_days))
    removed = sorted(set(before_days) - set(after_days))
    if added or removed:
        errors.append(
            PatchError(
                code="SCOPE_VIOLATION",
                message=(
                    f"a patch scoped to day {scope.target_id} may not add or remove days "
                    f"(added: {added or 'none'}, removed: {removed or 'none'})"
                ),
            )
        )

    for day_date in sorted(set(before_days) & set(after_days) - {scope.target_id}):
        if _canonical(before_days[day_date]) != _canonical(after_days[day_date]):
            errors.append(
                PatchError(
                    code="SCOPE_VIOLATION",
                    message=(
                        f"day {day_date} changed, but this patch is scoped to day {scope.target_id}"
                    ),
                    path=f"/itinerary/days ({day_date})",
                )
            )

    # Anything on the itinerary other than its days - generated_at and any
    # field added later - is also out of bounds.
    before_itinerary = dict(before.get("itinerary") or {})
    after_itinerary = dict(after.get("itinerary") or {})
    before_itinerary.pop("days", None)
    after_itinerary.pop("days", None)
    if _canonical(before_itinerary) != _canonical(after_itinerary):
        errors.append(
            PatchError(
                code="SCOPE_VIOLATION",
                message="itinerary-level fields changed under a day-scoped patch",
                path="/itinerary",
            )
        )

    errors.extend(_check_siblings(before, after))
    return errors


def _check_siblings(before: dict[str, Any], after: dict[str, Any]) -> list[PatchError]:
    """Everything outside the itinerary must be untouched, except added entities."""
    errors: list[PatchError] = []

    for key in sorted(set(before) | set(after)):
        if key in _ALWAYS_MUTABLE or key == "itinerary":
            continue

        if key == "entities":
            errors.extend(_check_entities(before.get(key) or {}, after.get(key) or {}))
            continue

        if _canonical(before.get(key)) != _canonical(after.get(key)):
            errors.append(
                PatchError(
                    code="SCOPE_VIOLATION",
                    message=f"{key} changed under a day-scoped patch",
                    path=f"/{key}",
                )
            )
    return errors


def _check_entities(before: dict[str, Any], after: dict[str, Any]) -> list[PatchError]:
    """New places are fine; rewriting or dropping known ones is not."""
    errors: list[PatchError] = []

    for entity_id in sorted(set(before) - set(after)):
        errors.append(
            PatchError(
                code="SCOPE_VIOLATION",
                message=f"entity {entity_id!r} was removed under a day-scoped patch",
                path=f"/entities/{entity_id}",
            )
        )

    for entity_id in sorted(set(before) & set(after)):
        if _canonical(before[entity_id]) != _canonical(after[entity_id]):
            errors.append(
                PatchError(
                    code="SCOPE_VIOLATION",
                    message=(
                        f"entity {entity_id!r} was modified under a day-scoped patch; "
                        "scoped patches may add entities but not change them"
                    ),
                    path=f"/entities/{entity_id}",
                )
            )

    return errors
