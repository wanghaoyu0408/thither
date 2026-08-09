"""Itinerary validation (spec section 27).

Reports; never repairs. The agent decides what to do about what it finds, which
keeps the "why did my trip change?" answer traceable to a decision rather than
to a silent auto-fix.

Pure and synchronous. Travel times arrive as an injected lookup rather than
being fetched inside, so every check is testable offline with no network and no
API key - and so the validator can never accidentally *estimate* a duration,
which spec section 21 forbids.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from app.models.common import utcnow
from app.models.entity import PlaceEntity
from app.models.itinerary import ItineraryDay, ItineraryItem
from app.models.route import TravelMode
from app.models.trip import TripState
from app.models.validation import ValidationIssue, ValidationState
from app.services.conflict_service import detect_conflicts
from app.services.geo import haversine_km
from app.services.opening_hours import OpenState, covers_visit, describe

# A leg needs slack for finding the door, queueing, and being human about it.
TRANSFER_BUFFER_MINUTES = 5


@dataclass
class TravelLookup:
    """Known travel times between entities, keyed (from, to, mode).

    A missing pair yields None, and every check treats that as "cannot verify"
    rather than "fine" - the same discipline as the constraint checkers.
    """

    minutes: dict[tuple[str, str, str], float] = field(default_factory=dict)
    meters: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def duration(self, origin: str, destination: str, mode: TravelMode) -> float | None:
        if origin == destination:
            return 0.0
        return self.minutes.get((origin, destination, mode))

    def distance(self, origin: str, destination: str, mode: TravelMode) -> float | None:
        if origin == destination:
            return 0.0
        return self.meters.get((origin, destination, mode))


@dataclass
class ValidationLimits:
    max_daily_walking_km: float = 12.0
    max_daily_transit_minutes: int = 150
    max_items_per_day: int = 7
    # Straight-line hop that suggests the day is bouncing across the city.
    backtrack_km: float = 4.0

    @classmethod
    def for_trip(cls, state: TripState) -> "ValidationLimits":
        pace = state.brief.pace
        if pace == "relaxed":
            return cls(max_daily_walking_km=8.0, max_daily_transit_minutes=100, max_items_per_day=4)
        if pace == "packed":
            return cls(
                max_daily_walking_km=16.0, max_daily_transit_minutes=210, max_items_per_day=9
            )
        return cls()


def _timed(items: list[ItineraryItem]) -> list[ItineraryItem]:
    return sorted(
        (item for item in items if item.start_at is not None),
        key=lambda item: item.start_at,
    )


def _end_of(item: ItineraryItem) -> datetime:
    return item.end_at or item.start_at


def _mode_between(
    state: TripState, origin: PlaceEntity | None, destination: PlaceEntity | None
) -> TravelMode:
    """Walk when it is plainly walkable, otherwise assume transit."""
    if origin is None or destination is None:
        return "walking"
    km = haversine_km(origin.lat, origin.lng, destination.lat, destination.lng)
    return "walking" if km <= 1.5 else "transit"


# --- individual checks -------------------------------------------------------


def _check_overlaps(day: ItineraryDay) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    ordered = _timed(day.items)

    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start_at < _end_of(earlier):
            issues.append(
                ValidationIssue(
                    severity="error",
                    type="schedule_overlap",
                    item_ids=[earlier.item_id, later.item_id],
                    message=(
                        f"{earlier.title!r} runs to {_end_of(earlier):%H:%M} but "
                        f"{later.title!r} starts at {later.start_at:%H:%M} on {day.date}"
                    ),
                    suggested_fix=f"move {later.title!r} later or shorten {earlier.title!r}",
                )
            )
    return issues


def _check_opening_hours(day: ItineraryDay, state: TripState) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for item in day.items:
        if item.start_at is None or not item.entity_id:
            continue
        entity = state.entities.get(item.entity_id)
        if entity is None:
            continue

        result = covers_visit(entity.opening_hours, item.start_at, item.end_at)

        if result is OpenState.CLOSED:
            issues.append(
                ValidationIssue(
                    severity="error",
                    type="closed_at_visit_time",
                    item_ids=[item.item_id],
                    message=(
                        f"{entity.name} is not open for the whole of "
                        f"{item.start_at:%H:%M}-{_end_of(item):%H:%M} on {day.date} "
                        f"({describe(entity.opening_hours, item.start_at)})"
                    ),
                    suggested_fix=(
                        f"move {entity.name} to {describe(entity.opening_hours, item.start_at)}"
                    ),
                )
            )
        elif result is OpenState.UNKNOWN:
            issues.append(
                ValidationIssue(
                    severity="info",
                    type="hours_unknown",
                    item_ids=[item.item_id],
                    message=f"{entity.name} publishes no opening hours; this slot is unverified",
                    suggested_fix="check the venue's own site before relying on this slot",
                )
            )
    return issues


def _check_travel(
    day: ItineraryDay, state: TripState, travel: TravelLookup
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    ordered = _timed(day.items)

    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if not (earlier.entity_id and later.entity_id):
            continue

        mode = _mode_between(
            state, state.entities.get(earlier.entity_id), state.entities.get(later.entity_id)
        )
        minutes = travel.duration(earlier.entity_id, later.entity_id, mode)

        if minutes is None:
            issues.append(
                ValidationIssue(
                    severity="info",
                    type="travel_time_unknown",
                    item_ids=[earlier.item_id, later.item_id],
                    message=(
                        f"no route looked up between {earlier.title!r} and {later.title!r}; "
                        "feasibility unverified"
                    ),
                    suggested_fix="fetch routes for this day before trusting the schedule",
                )
            )
            continue

        gap = (later.start_at - _end_of(earlier)).total_seconds() / 60.0

        if gap < minutes:
            issues.append(
                ValidationIssue(
                    severity="error",
                    type="travel_time_infeasible",
                    item_ids=[earlier.item_id, later.item_id],
                    message=(
                        f"{gap:.0f} min between {earlier.title!r} and {later.title!r}, but the "
                        f"{mode} journey takes {minutes:.0f} min"
                    ),
                    suggested_fix=f"start {later.title!r} at least {minutes:.0f} min later",
                )
            )
        elif gap < minutes + TRANSFER_BUFFER_MINUTES:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    type="insufficient_transfer",
                    item_ids=[earlier.item_id, later.item_id],
                    message=(
                        f"only {gap - minutes:.0f} min of slack between {earlier.title!r} and "
                        f"{later.title!r}"
                    ),
                    suggested_fix="leave a few minutes more",
                )
            )
    return issues


def _check_day_load(
    day: ItineraryDay, state: TripState, travel: TravelLookup, limits: ValidationLimits
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    ordered = _timed(day.items)

    if len(day.items) > limits.max_items_per_day:
        issues.append(
            ValidationIssue(
                severity="warning",
                type="day_overloaded",
                item_ids=[item.item_id for item in day.items],
                message=(
                    f"{len(day.items)} items on {day.date}, above the "
                    f"{limits.max_items_per_day} a {state.brief.pace} pace suggests"
                ),
                suggested_fix="drop the lowest-priority item or move it to another day",
            )
        )

    walking_km = 0.0
    transit_minutes = 0.0
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if not (earlier.entity_id and later.entity_id):
            continue
        mode = _mode_between(
            state, state.entities.get(earlier.entity_id), state.entities.get(later.entity_id)
        )
        if mode == "walking":
            meters = travel.distance(earlier.entity_id, later.entity_id, mode)
            if meters is not None:
                walking_km += meters / 1000.0
        else:
            minutes = travel.duration(earlier.entity_id, later.entity_id, mode)
            if minutes is not None:
                transit_minutes += minutes

    if walking_km > limits.max_daily_walking_km:
        issues.append(
            ValidationIssue(
                severity="warning",
                type="daily_walking_exceeded",
                item_ids=[item.item_id for item in day.items],
                message=(
                    f"about {walking_km:.1f} km of walking on {day.date}, over the "
                    f"{limits.max_daily_walking_km:.0f} km comfortable limit"
                ),
                suggested_fix="cluster the day more tightly, or take transit between stops",
            )
        )

    if transit_minutes > limits.max_daily_transit_minutes:
        issues.append(
            ValidationIssue(
                severity="warning",
                type="daily_transit_excessive",
                item_ids=[item.item_id for item in day.items],
                message=(
                    f"about {transit_minutes:.0f} min on transit on {day.date}, over the "
                    f"{limits.max_daily_transit_minutes} min limit"
                ),
                suggested_fix="group stops into one neighbourhood",
            )
        )
    return issues


def _check_backtracking(
    day: ItineraryDay, state: TripState, limits: ValidationLimits
) -> list[ValidationIssue]:
    """A day that leaves an area and comes back is usually an ordering mistake."""
    ordered = [
        item for item in _timed(day.items) if item.entity_id and item.entity_id in state.entities
    ]
    if len(ordered) < 3:
        return []

    issues: list[ValidationIssue] = []
    for first, second, third in zip(ordered, ordered[1:], ordered[2:], strict=False):
        a = state.entities[first.entity_id]
        b = state.entities[second.entity_id]
        c = state.entities[third.entity_id]

        out = haversine_km(a.lat, a.lng, b.lat, b.lng)
        back = haversine_km(b.lat, b.lng, c.lat, c.lng)
        direct = haversine_km(a.lat, a.lng, c.lat, c.lng)

        if out > limits.backtrack_km and back > limits.backtrack_km and direct < out / 2:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    type="geographic_backtracking",
                    item_ids=[first.item_id, second.item_id, third.item_id],
                    message=(
                        f"{day.date} goes {a.name} -> {b.name} -> {c.name}, "
                        f"travelling {out + back:.1f} km to end {direct:.1f} km from where it "
                        "started"
                    ),
                    suggested_fix=f"visit {b.name} on a day that is already in that area",
                )
            )
    return issues


def _check_locked_items(day: ItineraryDay, state: TripState) -> list[ValidationIssue]:
    locked_ids = {lock.target_id for lock in state.locks if lock.target_kind == "itinerary_item"}
    if not locked_ids:
        return []

    issues: list[ValidationIssue] = []
    ordered = _timed(day.items)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start_at < _end_of(earlier) and (
            earlier.item_id in locked_ids or later.item_id in locked_ids
        ):
            locked = earlier if earlier.item_id in locked_ids else later
            issues.append(
                ValidationIssue(
                    severity="error",
                    type="locked_item_conflict",
                    item_ids=[earlier.item_id, later.item_id],
                    message=f"a locked item, {locked.title!r}, overlaps another item on {day.date}",
                    suggested_fix="move the unlocked item; the locked one cannot be shifted",
                )
            )
    return issues


# What each decision's options are called, and what is unreal about them when
# they come from a test environment.
_SANDBOX_NOUNS: dict[str, tuple[str, str]] = {
    "flights": ("flight", "prices and schedules"),
    "hotel": ("hotel", "prices and availability"),
}


def _check_sandbox_data(state: TripState) -> list[ValidationIssue]:
    """Flag any priced option that came from a provider's test environment.

    Written once and walked across every decision rather than per provider. The
    prompt tells the model not to present sandbox data as real; this is what
    makes that hold, because the flag persists on the stored option and a trip
    carrying test data says so however it is read, and whoever reads it next.
    """
    issues: list[ValidationIssue] = []

    for name, decision in state.decisions.iter_decisions():
        noun, unreal = _SANDBOX_NOUNS.get(name, ("provider", "prices and details"))

        sandbox = [
            option
            for option in decision.options
            # `is False`, not falsy: an option whose data has no live_mode at
            # all is not making a claim about being real, and defaulting it to
            # sandbox would cry wolf on every place shortlist.
            if option.data is not None and getattr(option.data, "live_mode", True) is False
        ]
        if not sandbox:
            continue

        selected = decision.selected_option_id
        is_selected = any(option.option_id == selected for option in sandbox)

        issues.append(
            ValidationIssue(
                severity="warning" if is_selected else "info",
                type=f"sandbox_{noun}_data",
                item_ids=[option.option_id for option in sandbox],
                message=(
                    f"{len(sandbox)} {noun} option(s) came from the provider's test "
                    f"environment. These {unreal} are not real"
                    + (" and one of them is currently selected." if is_selected else ".")
                ),
                suggested_fix=(
                    "re-run the search with a live provider token before relying on these"
                ),
            )
        )

    return issues


_CONFLICT_SEVERITY: dict[str, str] = {
    "blocking": "error",
    "material": "warning",
    "minor": "info",
}


def _check_preference_conflicts(state: TripState) -> list[ValidationIssue]:
    """Surface group disagreements in the report the agent reads every turn.

    One of three places a conflict shows up, because one is not enough: here,
    as a blocking `OpenQuestion` the group must answer, and as an integrity rule
    that stops the trip being called ready. A conflict the model has to remember
    to go looking for is a conflict that sometimes goes unmentioned.

    Each issue carries every traveller's position in its message. That is the
    point of the milestone - the failure being guarded against is a single
    averaged number standing in for people who disagree.
    """
    issues: list[ValidationIssue] = []

    for conflict in detect_conflicts(state):
        positions = "; ".join(
            f"{name}: {stance}" for name, stance in sorted(conflict.positions.items())
        )
        issues.append(
            ValidationIssue(
                severity=_CONFLICT_SEVERITY[conflict.severity],
                type="preference_conflict",
                item_ids=conflict.affects,
                message=f"{conflict.summary} ({positions})" if positions else conflict.summary,
                suggested_fix=(
                    conflict.resolution_options[0] if conflict.resolution_options else None
                ),
            )
        )

    return issues


def _check_trip_wide(state: TripState) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    seen: dict[str, list[str]] = {}
    for _day, item in state.itinerary.iter_items():
        if item.entity_id:
            seen.setdefault(item.entity_id, []).append(item.item_id)

    for entity_id, item_ids in sorted(seen.items()):
        if len(item_ids) > 1:
            name = state.entities[entity_id].name if entity_id in state.entities else entity_id
            issues.append(
                ValidationIssue(
                    severity="warning",
                    type="duplicate_place",
                    item_ids=item_ids,
                    message=f"{name} appears {len(item_ids)} times in the trip",
                    suggested_fix="drop one, or keep it deliberately if the repeat is wanted",
                )
            )

    budget = state.brief.budget.total_per_person
    if budget is not None:
        costs = [
            item.estimated_cost for _d, item in state.itinerary.iter_items() if item.estimated_cost
        ]
        currencies = {cost.currency for cost in costs}
        if costs and len(currencies) == 1:
            spent = sum(cost.amount for cost in costs)
            ceiling = budget * state.brief.party.size
            if spent > ceiling:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        type="budget_exceeded",
                        item_ids=[],
                        message=(
                            f"itinerary costs {spent:.0f} {costs[0].currency} against a "
                            f"{ceiling:.0f} ceiling"
                        ),
                        suggested_fix="swap a high-cost item for something cheaper",
                    )
                )

    issues.extend(_check_sandbox_data(state))
    issues.extend(_check_preference_conflicts(state))

    if state.itinerary.days:
        issues.append(
            ValidationIssue(
                severity="info",
                type="holiday_coverage_unchecked",
                item_ids=[],
                message=(
                    "opening hours come from Google's regular weekly schedule, which does not "
                    "cover public holidays or seasonal closures"
                ),
                suggested_fix="check venues individually if the trip spans a public holiday",
            )
        )
    return issues


# --- entry point -------------------------------------------------------------


def validate_itinerary(
    state: TripState,
    *,
    travel: TravelLookup | None = None,
    target_date: date | None = None,
    limits: ValidationLimits | None = None,
) -> ValidationState:
    """Check the itinerary, or one day of it, and report what is wrong."""
    travel = travel or TravelLookup()
    limits = limits or ValidationLimits.for_trip(state)

    days = [day for day in state.itinerary.days if target_date is None or day.date == target_date]

    issues: list[ValidationIssue] = []
    for day in days:
        issues.extend(_check_overlaps(day))
        issues.extend(_check_locked_items(day, state))
        issues.extend(_check_opening_hours(day, state))
        issues.extend(_check_travel(day, state, travel))
        issues.extend(_check_day_load(day, state, travel, limits))
        issues.extend(_check_backtracking(day, state, limits))

    if target_date is None:
        issues.extend(_check_trip_wide(state))

    severities = {issue.severity for issue in issues}
    if "error" in severities:
        status = "errors"
    elif "warning" in severities:
        status = "warnings"
    else:
        status = "ok"

    return ValidationState(
        status=status,
        issues=issues,
        validated_revision=state.revision,
        validated_at=utcnow(),
    )


def build_travel_lookup(legs, entity_ids: list[str], mode: TravelMode) -> TravelLookup:
    """Turn a RouteLeg list from route_service into a validator lookup."""
    lookup = TravelLookup()
    for leg in legs:
        if leg.status != "ok":
            continue
        if leg.origin_index >= len(entity_ids) or leg.destination_index >= len(entity_ids):
            continue
        key = (entity_ids[leg.origin_index], entity_ids[leg.destination_index], mode)
        if leg.duration_seconds is not None:
            lookup.minutes[key] = leg.duration_seconds / 60.0
        if leg.distance_meters is not None:
            lookup.meters[key] = float(leg.distance_meters)
    return lookup


def merge_lookups(*lookups: TravelLookup) -> TravelLookup:
    merged = TravelLookup()
    for lookup in lookups:
        merged.minutes.update(lookup.minutes)
        merged.meters.update(lookup.meters)
    return merged
