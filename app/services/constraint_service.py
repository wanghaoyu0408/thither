"""Deterministic hard-constraint checking.

Honesty about coverage is the point here. Most constraint categories need data
that does not exist until a later milestone (live flight times, hotel
inventory, route durations), so a checker that silently returned "ok" for them
would be worse than useless - it would look like the constraint was enforced.

Checkers therefore return one of `ok`, `violated` or `not_checkable`, and
`not_checkable` is surfaced to the caller as a warning.
"""

from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel

from app.models.constraint import TripConstraint
from app.models.trip import TripState

CheckStatus = Literal["ok", "violated", "not_checkable"]


class ConstraintCheckResult(BaseModel):
    constraint_id: str
    status: CheckStatus
    message: str | None = None


class ConstraintChecker(Protocol):
    def __call__(self, constraint: TripConstraint, state: TripState) -> ConstraintCheckResult: ...


def _result(constraint: TripConstraint, status: CheckStatus, message: str) -> ConstraintCheckResult:
    return ConstraintCheckResult(constraint_id=constraint.id, status=status, message=message)


def _as_naive(value: datetime) -> datetime:
    """Normalize for comparison so aware and naive values never collide.

    Itinerary times are authored as local wall-clock. Real per-destination
    timezone handling arrives in M3 alongside routes and opening hours.
    """
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def check_budget(constraint: TripConstraint, state: TripState) -> ConstraintCheckResult:
    """Total itinerary cost against a per-person ceiling.

    Reads the constraint's own `params` first, falling back to the trip brief.
    """
    cap = constraint.params.get("max_total_per_person", state.brief.budget.total_per_person)
    if cap is None:
        return _result(constraint, "not_checkable", "no per-person budget ceiling recorded")

    currency = constraint.params.get("currency") or state.brief.budget.currency

    costs = [item.estimated_cost for _, item in state.itinerary.iter_items() if item.estimated_cost]
    if not costs:
        return _result(constraint, "not_checkable", "no itinerary items carry an estimated cost")

    foreign = sorted({c.currency for c in costs} - {currency})
    if foreign:
        return _result(
            constraint,
            "not_checkable",
            f"itinerary mixes currencies {foreign} with {currency}; FX conversion not implemented",
        )

    spent = sum(c.amount for c in costs)
    ceiling = float(cap) * state.brief.party.size
    if spent > ceiling:
        return _result(
            constraint,
            "violated",
            f"itinerary totals {spent:.2f} {currency}, over the "
            f"{ceiling:.2f} {currency} ceiling ({cap} per person x {state.brief.party.size})",
        )
    return _result(
        constraint,
        "ok",
        f"itinerary totals {spent:.2f} of {ceiling:.2f} {currency}",
    )


def check_schedule(constraint: TripConstraint, state: TripState) -> ConstraintCheckResult:
    """Fixed-time items must not overlap and must sit on the day they are filed under."""
    problems: list[str] = []
    checked_any = False

    for day in state.itinerary.days:
        fixed = [
            item
            for item in day.items
            if item.time_flexibility == "fixed" and item.start_at and item.end_at
        ]
        if not fixed:
            continue
        checked_any = True

        for item in fixed:
            start = _as_naive(item.start_at)
            if start.date() != day.date:
                problems.append(
                    f"{item.title!r} starts {start.date()} but is filed under {day.date}"
                )

        ordered = sorted(fixed, key=lambda i: _as_naive(i.start_at))
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if _as_naive(later.start_at) < _as_naive(earlier.end_at):
                problems.append(f"{earlier.title!r} and {later.title!r} overlap on {day.date}")

    if not checked_any:
        return _result(constraint, "not_checkable", "no fixed-time itinerary items to check")
    if problems:
        return _result(constraint, "violated", "; ".join(problems))
    return _result(constraint, "ok", "no fixed-time conflicts")


# Categories absent from this registry report `not_checkable` until the
# milestone that supplies their data lands (flights M5, hotels M6, mobility and
# transport M3 via the routes API, food M4).
CHECKERS: dict[str, ConstraintChecker] = {
    "budget": check_budget,
    "schedule": check_schedule,
}


def check_hard_constraints(state: TripState) -> list[ConstraintCheckResult]:
    results: list[ConstraintCheckResult] = []
    for constraint in state.constraints:
        if constraint.type != "hard":
            continue
        checker = CHECKERS.get(constraint.category)
        if checker is None:
            results.append(
                _result(
                    constraint,
                    "not_checkable",
                    f"no deterministic checker for category {constraint.category!r} yet",
                )
            )
            continue
        results.append(checker(constraint, state))
    return results
