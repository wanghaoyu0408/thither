"""What a day actually costs, computed from what was measured.

`DaySummary` has existed since M3 and nothing ever built one, so a day could be
four stops and an hour of driving and the interface had no way to say so.

Everything here is arithmetic over stored figures. Nothing is estimated to fill
a gap: a leg whose route was not measured adds nothing to the total and one to
the count of legs, so the reader can tell "58 minutes of driving" from "58
minutes of driving that we know about". That distinction is the whole point -
an unmeasured leg silently counted as zero makes a hard day look easy, which is
the one direction this must never be wrong in.

Pure and synchronous, like the validator it sits beside. It never fetches.
"""

from app.models.common import Money, utcnow
from app.models.itinerary import DaySummary, ItineraryDay
from app.models.trip import TripState
from app.models.validation import ValidationIssue
from app.services.validation_service import TravelLookup, mode_between


def _timed(items):
    return sorted([item for item in items if item.start_at], key=lambda item: item.start_at)


def summarize_day(
    day: ItineraryDay,
    state: TripState,
    travel: TravelLookup | None = None,
    *,
    issues: list[ValidationIssue] | None = None,
    pace: str | None = None,
) -> DaySummary:
    """The day's load, measured leg by leg.

    `pace` is the one the day was actually arranged to, which is not always the
    trip's: replanning one day at a relaxed pace leaves the brief alone, and
    reporting the brief's pace would describe a day that no longer exists.
    """
    travel = travel or TravelLookup()
    locked = {
        lock.target_id for lock in state.locks if lock.target_kind == "itinerary_item"
    }

    summary = DaySummary(
        stops=len(day.items),
        pace=pace or state.brief.pace,
        locked_items=sum(1 for item in day.items if item.item_id in locked),
        measured_at=utcnow(),
    )

    walking_minutes = 0.0
    walking_km = 0.0
    driving_minutes = 0.0
    transit_minutes = 0.0

    ordered = _timed(day.items)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if not (earlier.entity_id and later.entity_id):
            continue
        summary.legs_total += 1
        mode = mode_between(
            state, state.entities.get(earlier.entity_id), state.entities.get(later.entity_id)
        )
        minutes = travel.duration(earlier.entity_id, later.entity_id, mode)
        if minutes is None:
            # Not measured, so it counts toward the total number of legs and
            # toward none of the durations.
            continue
        summary.legs_measured += 1
        if mode == "walking":
            walking_minutes += minutes
            meters = travel.distance(earlier.entity_id, later.entity_id, mode)
            if meters is not None:
                walking_km += meters / 1000.0
        elif mode == "driving":
            driving_minutes += minutes
        else:
            transit_minutes += minutes

    summary.walking_minutes = round(walking_minutes, 1) if walking_minutes else None
    summary.total_walking_km = round(walking_km, 2) if walking_km else None
    summary.driving_minutes = round(driving_minutes, 1) if driving_minutes else None
    summary.total_transit_minutes = round(transit_minutes) if transit_minutes else None

    summary.items_total = len(day.items)
    priced = [item.estimated_cost for item in day.items if item.estimated_cost]
    summary.items_priced = len(priced)
    currencies = {cost.currency for cost in priced}
    if priced and len(currencies) == 1:
        summary.estimated_cost = Money(
            amount=round(sum(cost.amount for cost in priced), 2), currency=priced[0].currency
        )
    elif len(currencies) > 1:
        # Adding two currencies together would invent an exchange rate.
        summary.notes = f"costs are in {len(currencies)} currencies and were not added up"
        summary.items_priced = 0

    if issues is not None:
        on_this_day = {item.item_id for item in day.items}
        summary.unresolved_issues = sum(
            1
            for issue in issues
            if issue.severity != "info"
            and (not issue.item_ids or on_this_day.intersection(issue.item_ids))
        )

    return summary


def describe(summary: DaySummary) -> list[str]:
    """The day in the words the interface uses, partials labelled as partials."""
    lines: list[str] = [f"{summary.stops} stop" + ("s" if summary.stops != 1 else "")]

    partial = " (partial)" if summary.travel_is_partial else ""
    if summary.driving_minutes:
        lines.append(f"{summary.driving_minutes:.0f} min driving{partial}")
    if summary.total_transit_minutes:
        lines.append(f"{summary.total_transit_minutes:.0f} min transit{partial}")
    if summary.total_walking_km:
        lines.append(f"{summary.total_walking_km:.1f} km walking{partial}")
    if summary.legs_total and not summary.legs_measured:
        lines.append("travel times not measured")

    if summary.estimated_cost:
        amount = f"{summary.estimated_cost.amount:,.0f} {summary.estimated_cost.currency}"
        # Never "total" while anything on the day carries no price.
        lines.append(
            f"~{amount} partial estimate" if summary.cost_is_partial else f"{amount} total"
        )

    if summary.pace:
        lines.append(f"{summary.pace.capitalize()} pace")
    if summary.locked_items:
        lines.append(f"{summary.locked_items} locked")
    return lines
