"""Building and locally replanning itineraries (spec sections 26 and 29).

Deterministic on purpose. Spec section 26 forbids asking the model to emit a
whole detailed itinerary in one generation, and section 44 puts arithmetic,
sorting, routing and validation on the code side of the line. The model chooses
areas, themes and what to sacrifice; everything below is ordinary scheduling.

The pipeline follows section 26:

    cluster -> daily skeleton -> fixed anchors -> meals -> activities
            -> routes -> validate -> repair -> propose

Nothing here mutates a trip. Callers get an `ItineraryProposal` and apply it
through the patch engine like every other change.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from app.models.common import Pace, new_id
from app.models.entity import PlaceEntity
from app.models.itinerary import ItemType, ItineraryDay, ItineraryItem
from app.models.itinerary_plan import (
    ItineraryProposal,
    PlannedDay,
    PlannedItem,
    PlanParams,
    ReplanParams,
)
from app.models.patch import PatchOperation, PatchScope
from app.models.trip import TripState
from app.services.clustering import cluster_places, label_clusters
from app.services.geo import haversine_km
from app.services.opening_hours import OpenState, covers_visit, describe
from app.services.validation_service import (
    TravelLookup,
    ValidationLimits,
    validate_itinerary,
)

SlotKind = Literal["meal", "cafe", "activity"]

# Google place types that decide what a place is good for.
_MEAL_TYPES = {"restaurant", "meal_takeaway", "meal_delivery", "food"}
_CAFE_TYPES = {"cafe", "coffee_shop", "bakery", "tea_house"}


@dataclass(frozen=True)
class Slot:
    kind: SlotKind
    start: time
    minutes: int

    def window(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, self.start)
        return start, start + timedelta(minutes=self.minutes)


# Day templates by pace. Ordered, and deliberately unambitious at the relaxed
# end - "make it easier" has to mean something concrete.
_TEMPLATES: dict[Pace, list[Slot]] = {
    "relaxed": [
        Slot("activity", time(11, 0), 90),
        Slot("meal", time(13, 0), 75),
        Slot("cafe", time(15, 30), 60),
        Slot("meal", time(19, 0), 105),
    ],
    "balanced": [
        Slot("activity", time(10, 0), 90),
        Slot("meal", time(12, 30), 75),
        Slot("activity", time(14, 30), 90),
        Slot("cafe", time(16, 30), 45),
        Slot("meal", time(19, 0), 105),
    ],
    "packed": [
        Slot("activity", time(9, 0), 90),
        Slot("cafe", time(11, 0), 45),
        Slot("meal", time(12, 30), 75),
        Slot("activity", time(14, 30), 90),
        Slot("activity", time(16, 30), 90),
        Slot("meal", time(19, 0), 105),
        Slot("activity", time(21, 0), 90),
    ],
}


def slot_kind_for(entity: PlaceEntity) -> SlotKind:
    categories = {category.lower() for category in entity.categories}
    if categories & _CAFE_TYPES:
        return "cafe"
    if categories & _MEAL_TYPES:
        return "meal"
    return "activity"


def item_type_for(kind: SlotKind) -> ItemType:
    return "restaurant" if kind in ("meal", "cafe") else "activity"


def _locked_item_ids(state: TripState) -> set[str]:
    return {lock.target_id for lock in state.locks if lock.target_kind == "itinerary_item"}


def _trip_dates(state: TripState, requested: int | None) -> list[date]:
    start = state.brief.dates.start
    end = state.brief.dates.end
    if start is None:
        return []
    if requested:
        count = requested
    elif end is not None:
        count = (end - start).days + 1
    else:
        count = 1
    return [start + timedelta(days=offset) for offset in range(max(count, 1))]


def _openness(entity: PlaceEntity, slot: Slot, day: date) -> OpenState:
    start, end = slot.window(day)
    return covers_visit(entity.opening_hours, start, end)


def _area_names(candidates: list[PlaceEntity], areas: list[str]) -> dict[str, str]:
    """Match suggested area names against each place's real address.

    Uses the address Google returned rather than trusting position, so a label
    only sticks when the places genuinely are there.
    """
    if not areas:
        return {}

    names: dict[str, str] = {}
    for entity in candidates:
        # Address only. A restaurant called "Shinjuku-tei" that sits in Ginza is
        # in Ginza, and matching on the name would label the whole day wrong.
        if not entity.address:
            continue
        haystack = entity.address.lower()
        for area in areas:
            if area.lower() in haystack:
                names[entity.entity_id] = area
                break
    return names


def _fits(entity: PlaceEntity, slot: Slot, day: date) -> tuple[int, float]:
    """Sort key: confirmed-open before unknown, then better rated.

    Places known to be *closed* are filtered out upstream rather than ranked
    last - scheduling a venue we can see is shut would be exactly the kind of
    confident wrongness this project exists to avoid, and the validator would
    only report it after the fact.
    """
    rank = {OpenState.OPEN: 0, OpenState.UNKNOWN: 1, OpenState.CLOSED: 2}[
        _openness(entity, slot, day)
    ]
    return rank, -(entity.rating or 0.0)


def _schedule_day(
    day: date,
    slots: list[Slot],
    candidates: list[PlaceEntity],
    *,
    used: set[str],
) -> list[ItineraryItem]:
    """Fill a day's slots, walking outward from wherever the day starts.

    Choosing the nearest suitable place for each next slot is what keeps a day
    from bouncing across the city; the validator checks the result rather than
    trusting it.
    """
    items: list[ItineraryItem] = []
    previous: PlaceEntity | None = None

    for slot in slots:
        pool = [
            entity
            for entity in candidates
            if entity.entity_id not in used
            and slot_kind_for(entity) == slot.kind
            # Leave the slot empty rather than book somewhere we can see is shut.
            and _openness(entity, slot, day) is not OpenState.CLOSED
        ]
        if not pool:
            continue

        def sort_key(
            entity: PlaceEntity, slot: Slot = slot, previous: PlaceEntity | None = previous
        ):
            openness, rating = _fits(entity, slot, day)
            proximity = (
                haversine_km(previous.lat, previous.lng, entity.lat, entity.lng)
                if previous is not None
                else 0.0
            )
            return (openness, round(proximity, 1), rating, entity.entity_id)

        chosen = min(pool, key=sort_key)
        used.add(chosen.entity_id)
        previous = chosen

        start, end = slot.window(day)
        items.append(
            ItineraryItem(
                item_id=new_id("item"),
                type=item_type_for(slot.kind),
                entity_id=chosen.entity_id,
                title=chosen.name,
                start_at=start,
                end_at=end,
                time_flexibility="flexible",
                status="proposed",
            )
        )

    return items


def _planned_day(day: ItineraryDay, state: TripState, locked: set[str]) -> PlannedDay:
    return PlannedDay(
        date=day.date,
        theme=day.theme,
        items=[
            PlannedItem(
                item_id=item.item_id,
                title=item.title,
                type=item.type,
                start_at=item.start_at.isoformat() if item.start_at else None,
                end_at=item.end_at.isoformat() if item.end_at else None,
                entity_id=item.entity_id,
                locked=item.item_id in locked,
                opening_hours=(
                    describe(state.entities[item.entity_id].opening_hours, item.start_at)
                    if item.entity_id in state.entities and item.start_at
                    else None
                ),
            )
            for item in day.items
        ],
    )


def build_itinerary(
    state: TripState,
    *,
    params: PlanParams | None = None,
    candidate_ids: list[str] | None = None,
    travel: TravelLookup | None = None,
) -> ItineraryProposal:
    """Lay out the whole trip. Returns a proposal; applies nothing."""
    params = params or PlanParams()
    pace: Pace = params.intensity or state.brief.pace

    days = _trip_dates(state, params.days)
    warnings: list[str] = []

    if not days:
        return ItineraryProposal(
            trip_id=state.trip_id,
            base_revision=state.revision,
            summary="The trip has no start date, so there is nothing to lay out yet.",
            warnings=["brief.dates.start is not set"],
        )

    pool_ids = candidate_ids if candidate_ids is not None else list(state.entities)
    candidates = [state.entities[eid] for eid in pool_ids if eid in state.entities]

    if not candidates:
        return ItineraryProposal(
            trip_id=state.trip_id,
            base_revision=state.revision,
            summary="No places are stored for this trip yet, so there is nothing to schedule.",
            warnings=["search for places before generating an itinerary"],
        )

    clusters = cluster_places(
        {entity.entity_id: (entity.lat, entity.lng) for entity in candidates}, len(days)
    )
    # Name each day after where its places actually are. Zipping the caller's
    # area list against day position looks right and is wrong: clusters are
    # ordered west-to-east, which has nothing to do with the order the areas
    # were suggested in, so a Shibuya day ends up labelled "Asakusa".
    label_clusters(clusters, _area_names(candidates, params.areas))

    slots = _TEMPLATES[pace]
    used: set[str] = set()
    new_days: list[ItineraryDay] = []

    for index, day_date in enumerate(days):
        cluster = clusters[index] if index < len(clusters) else None
        day_candidates = (
            [state.entities[eid] for eid in cluster.entity_ids] if cluster else list(candidates)
        )
        # Fall back to the whole pool when a cluster cannot fill the day, rather
        # than emitting an empty day.
        items = _schedule_day(day_date, slots, day_candidates, used=used)
        if not items:
            items = _schedule_day(day_date, slots, candidates, used=used)

        # No label beats a wrong label: an unnamed day is honest, a day called
        # "Asakusa" that is entirely in Shibuya is not.
        theme = cluster.label if cluster else None
        new_days.append(
            ItineraryDay(
                date=day_date,
                theme=theme,
                area_ids=[theme] if theme else [],
                items=items,
            )
        )

    thin = [day.date.isoformat() for day in new_days if len(day.items) < 2]
    if thin:
        warnings.append(
            f"only sparse options for {', '.join(thin)}; search more places for those days"
        )

    candidate_state = state.model_copy(deep=True)
    candidate_state.itinerary.days = new_days
    validation = validate_itinerary(candidate_state, travel=travel)

    operations = [
        PatchOperation(
            op="set",
            path="/itinerary/days",
            value=[day.model_dump(mode="json") for day in new_days],
        )
    ]

    locked = _locked_item_ids(state)
    return ItineraryProposal(
        trip_id=state.trip_id,
        base_revision=state.revision,
        operations=operations,
        days=[_planned_day(day, candidate_state, locked) for day in new_days],
        days_changed=[day.date for day in new_days],
        summary=(
            f"{len(new_days)} days at a {pace} pace, "
            f"{sum(len(day.items) for day in new_days)} items drawn from "
            f"{len(candidates)} stored places."
        ),
        validation=validation,
        warnings=warnings,
    )


def substitute_item(
    state: TripState,
    item_id: str,
    *,
    travel: TravelLookup | None = None,
) -> ItineraryProposal:
    """Swap one scheduled place for the best alternative the trip already knows.

    `replan_day` cannot do this: it drops, keeps and re-times, but nothing in it
    pulls a fresh candidate into a day, so dropping an item leaves a gap rather
    than a substitution.

    The substitute is chosen the same way the builder chooses anything - places
    the trip has already researched, filtered to the same slot kind, never one
    already scheduled or rejected, ranked by `_fits` so a venue confirmed open
    in that window beats one whose hours nobody knows.

    When nothing qualifies it returns an empty proposal explaining why. Leaving
    a hole in the day, or inventing a place to fill it, would both be worse than
    saying "there is nothing here to swap in".
    """
    locked = _locked_item_ids(state)

    found = next(
        ((day, item) for day, item in state.itinerary.iter_items() if item.item_id == item_id),
        None,
    )
    if found is None:
        return ItineraryProposal(
            trip_id=state.trip_id,
            base_revision=state.revision,
            summary=f"no item {item_id!r} on this itinerary.",
            warnings=[f"unknown item {item_id!r}"],
        )

    day, item = found
    if item_id in locked:
        return ItineraryProposal(
            trip_id=state.trip_id,
            base_revision=state.revision,
            summary=f"{item.title} is locked.",
            warnings=[
                f"{item.title!r} is locked; unlock it first if you want it replaced"
            ],
        )

    current = state.entities.get(item.entity_id or "")
    kind: SlotKind = slot_kind_for(current) if current else "activity"
    minutes = (
        int((item.end_at - item.start_at).total_seconds() // 60)
        if item.start_at and item.end_at
        else 90
    )
    slot = Slot(kind, (item.start_at or datetime.combine(day.date, time(10, 0))).time(), minutes)

    scheduled = {other.entity_id for _d, other in state.itinerary.iter_items() if other.entity_id}
    rejected = {
        rejection.target_id
        for rejection in state.rejections
        if rejection.target_kind == "entity"
    }

    candidates = [
        entity
        for entity in state.entities.values()
        if entity.entity_id not in scheduled
        and entity.entity_id not in rejected
        and slot_kind_for(entity) == kind
        # Known-closed is filtered, not ranked last: swapping in a venue we can
        # see is shut would replace one problem with another.
        and _openness(entity, slot, day.date) != OpenState.CLOSED
    ]

    if not candidates:
        return ItineraryProposal(
            trip_id=state.trip_id,
            base_revision=state.revision,
            summary=f"nothing to swap in for {item.title}.",
            warnings=[
                f"no alternative among the {len(state.entities)} place(s) this trip knows is "
                f"a {kind} that is free and open at {slot.start.strftime('%H:%M')}. "
                f"Ask the agent to research more options for this slot."
            ],
        )

    best = min(candidates, key=lambda entity: (*_fits(entity, slot, day.date), entity.entity_id))
    start, end = slot.window(day.date)
    replacement = ItineraryItem(
        type=item_type_for(kind),
        entity_id=best.entity_id,
        title=best.name,
        start_at=start,
        end_at=end,
        time_flexibility=item.time_flexibility,
        status="proposed",
    )

    items = [replacement if other.item_id == item_id else other for other in day.items]
    day_index = next(
        index for index, existing in enumerate(state.itinerary.days) if existing.date == day.date
    )

    candidate_state = state.model_copy(deep=True)
    candidate_state.itinerary.days[day_index] = day.model_copy(update={"items": items})
    validation = validate_itinerary(candidate_state, travel=travel, target_date=day.date)

    return ItineraryProposal(
        trip_id=state.trip_id,
        base_revision=state.revision,
        scope=PatchScope(kind="itinerary_day", target_id=day.date.isoformat()),
        operations=[
            PatchOperation(
                op="set",
                path=f"/itinerary/days/{day_index}/items",
                value=[entry.model_dump(mode="json") for entry in items],
            )
        ],
        days=[_planned_day(candidate_state.itinerary.days[day_index], candidate_state, locked)],
        days_changed=[day.date],
        summary=f"{item.title} replaced with {best.name}",
        validation=validation,
        warnings=[
            f"{len(candidates) - 1} other alternative(s) were available"
            if len(candidates) > 1
            else "this was the only alternative the trip knows"
        ],
    )


def _drop_order(item: ItineraryItem, locked: set[str]) -> tuple:
    """Which items go first when a day has to shed load.

    Locked items never move. Reservations outrank casual stops, meals outrank
    activities, and item_id breaks ties so the choice is reproducible.
    """
    return (
        item.item_id in locked,
        bool(item.reservation_booked),
        bool(item.reservation_required),
        item.status == "locked",
        item.type == "restaurant",
        item.start_at is not None and item.start_at.hour >= 18,
        item.item_id,
    )


def _open_where_it_is(item: ItineraryItem, state: TripState) -> bool:
    """Is the item already at a time its venue is open?

    Unknown counts as yes: plenty of places publish no hours, and refusing to
    keep them would quietly empty the itinerary.
    """
    entity = state.entities.get(item.entity_id) if item.entity_id else None
    if entity is None or item.start_at is None:
        return True
    return covers_visit(entity.opening_hours, item.start_at, item.end_at) is not OpenState.CLOSED


def _slot_for(
    item: ItineraryItem,
    state: TripState,
    available: list[Slot],
    day: date,
) -> Slot | None:
    """Pick a slot for an item being re-timed.

    Re-timing has to respect the same two rules the builder does, or a replan
    silently degrades the day it was asked to improve: a restaurant does not
    belong in a sightseeing slot, and nothing belongs in a slot where the venue
    is shut. Ties break toward the item's original time, so a relaxed day still
    looks like the day the user was shown.
    """
    if not available:
        return None

    entity = state.entities.get(item.entity_id) if item.entity_id else None
    wanted: SlotKind = "activity" if item.type != "restaurant" else "meal"
    original = item.start_at

    def rank(slot: Slot) -> tuple:
        if entity is not None and _openness(entity, slot, day) is OpenState.CLOSED:
            return (2, 0, 0)
        kind_penalty = (
            0 if slot.kind == wanted else (0 if wanted == "meal" and slot.kind == "cafe" else 1)
        )
        drift = (
            abs((datetime.combine(day, slot.start) - original).total_seconds())
            if original is not None
            else 0.0
        )
        return (0, kind_penalty, drift)

    best = min(available, key=rank)
    return None if rank(best)[0] == 2 else best


def replan_day(
    state: TripState,
    target_date: date,
    *,
    params: ReplanParams | None = None,
    travel: TravelLookup | None = None,
) -> ItineraryProposal:
    """Rework a single day, leaving every other day untouched (spec section 29).

    The returned proposal carries a `scope`, so the patch engine rejects it
    outright if it somehow reaches beyond the target day.
    """
    params = params or ReplanParams()
    locked = _locked_item_ids(state)

    day = next((d for d in state.itinerary.days if d.date == target_date), None)
    if day is None:
        return ItineraryProposal(
            trip_id=state.trip_id,
            base_revision=state.revision,
            summary=f"{target_date} is not in this itinerary.",
            warnings=[
                f"no day {target_date}; days are "
                f"{[d.date.isoformat() for d in state.itinerary.days]}"
            ],
        )

    pace: Pace = params.intensity or state.brief.pace
    limits = ValidationLimits.for_trip(
        state.model_copy(update={"brief": state.brief.model_copy(update={"pace": pace})})
    )
    target_count = params.max_items or limits.max_items_per_day

    keep_ids = locked | set(params.keep_item_ids)
    drop_ids = set(params.drop_item_ids) - locked
    if set(params.drop_item_ids) & locked:
        warnings_locked = sorted(set(params.drop_item_ids) & locked)
    else:
        warnings_locked = []

    surviving = [item for item in day.items if item.item_id not in drop_ids]

    # Shed the least-defensible items until the day fits the requested pace.
    ordered = sorted(surviving, key=lambda item: _drop_order(item, keep_ids))
    while len(ordered) > target_count and ordered and ordered[0].item_id not in keep_ids:
        ordered.pop(0)

    kept = sorted(
        ordered,
        key=lambda item: item.start_at or datetime.combine(target_date, time(0, 0)),
    )

    # Re-time the unlocked survivors onto the pace's template; locked items keep
    # the slot they were booked into.
    movable = [item for item in kept if item.item_id not in keep_ids]
    fixed = [item for item in kept if item.item_id in keep_ids]
    taken = {(item.start_at, item.end_at) for item in fixed}

    retimed: list[ItineraryItem] = list(fixed)
    available = [slot for slot in _TEMPLATES[pace] if slot.window(target_date) not in taken]

    unplaceable: list[str] = []
    for item in movable:
        slot = _slot_for(item, state, available, target_date)
        if slot is None:
            # No open slot is free. Keeping the item is only defensible if it is
            # already at a time the venue is open - leaving it parked on a
            # closed slot would be the same confident wrongness the builder
            # refuses to produce, just arrived at by inertia.
            if _open_where_it_is(item, state):
                retimed.append(item)
            else:
                unplaceable.append(item.title)
            continue
        available.remove(slot)
        start, end = slot.window(target_date)
        retimed.append(item.model_copy(update={"start_at": start, "end_at": end}))

    retimed.sort(key=lambda item: item.start_at or datetime.combine(target_date, time(0, 0)))
    new_day = day.model_copy(update={"items": retimed})

    candidate_state = state.model_copy(deep=True)
    day_index = next(
        index for index, d in enumerate(candidate_state.itinerary.days) if d.date == target_date
    )
    candidate_state.itinerary.days[day_index] = new_day
    validation = validate_itinerary(candidate_state, travel=travel, target_date=target_date)

    removed = len(day.items) - len(retimed)
    warnings = []
    if warnings_locked:
        warnings.append(
            f"refused to drop locked items {warnings_locked}; unlock them first if that is intended"
        )
    if unplaceable:
        warnings.append(
            f"dropped {', '.join(unplaceable)}: no free slot on this day when they are open. "
            "Search for an alternative, or move them to another day."
        )

    return ItineraryProposal(
        trip_id=state.trip_id,
        base_revision=state.revision,
        scope=PatchScope(kind="itinerary_day", target_id=target_date.isoformat()),
        operations=[
            PatchOperation(
                op="set",
                path=f"/itinerary/days/{day_index}/items",
                value=[item.model_dump(mode="json") for item in retimed],
            )
        ],
        days=[_planned_day(new_day, candidate_state, locked)],
        days_changed=[target_date],
        summary=(
            f"{target_date} goes from {len(day.items)} to {len(retimed)} items at a {pace} pace"
            + (f", dropping {removed}" if removed > 0 else "")
            + (f"; {len(fixed)} locked item(s) untouched" if fixed else "")
        ),
        validation=validation,
        warnings=warnings,
    )
