"""Direct actions on a trip: the buttons, without a language model in the way.

Clicking "Move" should not cost an LLM turn and four minutes. These endpoints
build the same validated `TripPatch`es the agent builds, through the same
engine, and hand back the same guarantees - so a button and a sentence reach the
identical machinery, and neither can bypass a gate the other honours.

Every mutating route in here obeys one shape:

    validate -> atomic commit -> revision bumped -> reload -> only then success

`TripRepository.apply_patches` provides that (INVARIANTS.md section 3), and each
response carries the *reloaded* trip plus a structured diff of what actually
changed. Nothing here reports a change it has not read back, and nothing asks
the caller to guess what moved.

Judgement stays with the agent. `replace` picks from places the trip already
researched and says so plainly when there is nothing to pick; it does not invent
a venue, and it does not quietly leave a hole.
"""

from datetime import date as date_type
from datetime import datetime, time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import TripNotFound, TripRepository
from app.db.session import get_session, get_sessionmaker
from app.models.itinerary_plan import ItineraryProposal, ReplanParams
from app.models.lock import LockRecord
from app.models.patch import PatchError, TripPatch
from app.models.rejection import RejectionRecord
from app.models.route import LocationRef
from app.models.trip import TripState
from app.services import json_pointer as jp
from app.services.conflict_service import detect_conflicts, unresolved_blocking
from app.services.explanation_service import Explanation, explain, explain_item
from app.services.intake_service import today_at
from app.services.itinerary_diff import DayDiff, changed_days
from app.services.itinerary_service import replan_day, substitute_item
from app.services.toolbox import MissingCredentials, Toolbox
from app.services.validation_service import TravelLookup, mode_between, validate_itinerary

router = APIRouter(prefix="/trips/{trip_id}", tags=["actions"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ActionResult(BaseModel):
    """What happened, always read back from the store.

    `applied=False` with a populated `errors` is a normal outcome, not an
    exception: a locked item refusing to move is the system working. The trip
    comes back either way so the caller re-renders from truth rather than
    rolling back a guess.
    """

    applied: bool
    revision: int
    summary: str = ""

    diff: list[DayDiff] = []
    errors: list[PatchError] = []
    warnings: list[str] = []

    trip: TripState
    validation: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []


class LockRequest(BaseModel):
    reason: str = "the traveller asked to keep this"


class MoveRequest(BaseModel):
    to_date: date_type | None = None
    # HH:MM, the wall-clock the itinerary is authored in.
    to_time: str | None = None


class ReplaceRequest(BaseModel):
    note: str | None = None


class RejectRequest(BaseModel):
    reason: str | None = None


class ReplanRequest(BaseModel):
    intensity: str | None = None
    max_items: int | None = None
    keep_item_ids: list[str] = []
    drop_item_ids: list[str] = []


async def _load(session: AsyncSession, trip_id: str) -> TripState:
    try:
        return await TripRepository(session).get(trip_id)
    except TripNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"trip {trip_id} not found") from None


async def _measure(state: TripState, days) -> TravelLookup:
    """Real travel times for these days, or an empty lookup if we cannot get them.

    Buttons used to validate against nothing at all, so every action reported
    `travel_time_unknown` for every pair and every day summary it wrote said the
    travel was unmeasured. The agent had real numbers the whole time; only the
    HTTP path was blind.
    """
    try:
        toolbox = Toolbox(sessions=get_sessionmaker())
    except MissingCredentials:
        return TravelLookup()
    async with toolbox:
        return await toolbox.routes.measure_days(state, days)


def _view(
    state: TripState,
    before: TripState,
    *,
    applied: bool,
    summary: str,
    errors: list[PatchError] | None = None,
    warnings: list[str] | None = None,
    travel: TravelLookup | None = None,
) -> ActionResult:
    """One response shape, built from the state the caller will actually see."""
    # With no lookup the validator reports every pair as `travel_time_unknown`,
    # which is true but useless: it was unknown because nobody looked.
    validation = validate_itinerary(state, travel=travel)
    return ActionResult(
        applied=applied,
        revision=state.revision,
        summary=summary,
        diff=changed_days(before, state),
        errors=errors or [],
        warnings=warnings or [],
        trip=state,
        validation={
            "status": validation.status,
            "issues": [issue.model_dump(mode="json") for issue in validation.issues],
        },
        conflicts=[conflict.model_dump(mode="json") for conflict in detect_conflicts(state)],
        blocking=[conflict.model_dump(mode="json") for conflict in unresolved_blocking(state)],
    )


async def _commit(
    session: AsyncSession,
    before: TripState,
    patches: list[TripPatch],
    *,
    summary: str,
    warnings: list[str] | None = None,
) -> ActionResult:
    """Apply as one unit, then report from the reloaded row."""
    results = await TripRepository(session).apply_patches(before.trip_id, patches)

    failed = next((result for result in results if not result.applied), None)
    if failed is not None:
        # Nothing was written. Re-read anyway so the caller re-renders from the
        # store rather than from whatever it had in hand.
        current = await _load(session, before.trip_id)
        return _view(
            current,
            before,
            applied=False,
            summary="nothing was changed",
            errors=failed.errors,
            warnings=warnings,
            travel=await _measure(current, current.itinerary.days),
        )

    committed = results[-1].state
    return _view(
        committed,
        before,
        applied=True,
        summary=summary,
        warnings=warnings,
        travel=await _measure(committed, committed.itinerary.days),
    )


def _find_item(state: TripState, item_id: str):
    for day, item in state.itinerary.iter_items():
        if item.item_id == item_id:
            return day, item
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no item {item_id} on this trip")


def _day_index(state: TripState, when: date_type) -> int:
    for index, day in enumerate(state.itinerary.days):
        if day.date == when:
            return index
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no day {when} on this trip")


def _lock_for(state: TripState, item_id: str) -> LockRecord | None:
    return next(
        (
            lock
            for lock in state.locks
            if lock.target_kind == "itinerary_item" and lock.target_id == item_id
        ),
        None,
    )


def _refuse_if_locked(state: TripState, item_id: str, verb: str) -> None:
    lock = _lock_for(state, item_id)
    if lock is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this item is locked ({lock.reason}); unlock it before trying to {verb} it",
        )


def _is_noop(state: TripState, proposal: ItineraryProposal) -> bool:
    """Whether applying this proposal would leave the trip exactly as it is.

    `ItineraryProposal.is_empty` only asks whether there are operations; this
    asks whether they would do anything. A day that is already relaxed produces
    a full `set` of identical items, and applying it burns a revision on a
    change nobody made.
    """
    document = state.model_dump(mode="json")
    for operation in proposal.operations:
        try:
            current = jp.resolve(document, operation.path)
        except jp.PointerError:
            return False
        if _comparable(operation.path, current) != _comparable(operation.path, operation.value):
            return False
    return True


def _comparable(path: str, value: Any) -> Any:
    """The part of a value that says what the trip *is*.

    A day summary records when it was measured. Re-measuring an unchanged day
    produces a new timestamp and an otherwise identical summary, and comparing
    that stamp would make every repeat replan look like a change - which is the
    exact bug this guard was added to fix.
    """
    if path.endswith("/summary") and isinstance(value, dict):
        return {key: item for key, item in value.items() if key != "measured_at"}
    return value


def _proposal_patches(
    state: TripState, proposal: ItineraryProposal, reason: str
) -> list[TripPatch]:
    return [
        TripPatch(
            base_revision=state.revision,
            reason=reason,
            actor="user",
            operations=proposal.operations,
            scope=proposal.scope,
        )
    ]


class DayRouteLeg(BaseModel):
    """One hop between consecutive stops, as the road actually runs."""

    from_item_id: str
    to_item_id: str
    from_title: str
    to_title: str
    mode: str
    minutes: float | None = None
    meters: int | None = None
    # Google's encoded path. None means we could not get one, and the caller
    # must not fall back to a straight line without saying that is what it is.
    polyline: str | None = None
    # Why there is no path, kept apart the way this project keeps them apart
    # everywhere else: "Google knows of no route" and "the lookup failed" are
    # different facts and only one of them is about the trip.
    status: Literal["ok", "no_route", "lookup_failed"] = "ok"


class DayRoute(BaseModel):
    date: date_type
    legs: list[DayRouteLeg] = []
    # What the caller may claim about the line it draws.
    geometry: Literal["route", "straight_line", "none"] = "none"
    note: str | None = None


@router.get("/days/{when}/route", response_model=DayRoute)
async def day_route(trip_id: str, when: date_type, session: SessionDep) -> DayRoute:
    """The real path between a day's stops, for drawing.

    Fetched for the view and not persisted: route geometry is provider content
    with a short shelf life, and the trip has no business holding a copy of the
    road. The mode is whichever one this trip actually travels in, so a driving
    trip gets driving directions rather than a walking line.
    """
    state = await _load(session, trip_id)
    day = state.itinerary.days[_day_index(state, when)]

    stops = [item for item in day.items if item.entity_id in state.entities]
    if len(stops) < 2:
        return DayRoute(date=when, note="not enough stops to draw a route")

    try:
        toolbox = Toolbox(sessions=get_sessionmaker())
    except MissingCredentials:
        return DayRoute(
            date=when,
            geometry="straight_line",
            note="no routing key configured, so the line is straight, not a route",
        )

    legs: list[DayRouteLeg] = []
    async with toolbox:
        for earlier, later in zip(stops, stops[1:], strict=False):
            origin, destination = state.entities[earlier.entity_id], state.entities[later.entity_id]
            mode = mode_between(state, origin, destination)
            leg = await toolbox.routes.path_between(
                LocationRef(entity_id=origin.entity_id, lat=origin.lat, lng=origin.lng),
                LocationRef(
                    entity_id=destination.entity_id, lat=destination.lat, lng=destination.lng
                ),
                mode=mode,
            )
            if leg is None:
                status = "lookup_failed"
            elif leg.polyline:
                status = "ok"
            else:
                status = "no_route"
            legs.append(
                DayRouteLeg(
                    from_item_id=earlier.item_id,
                    to_item_id=later.item_id,
                    from_title=earlier.title,
                    to_title=later.title,
                    mode=mode,
                    minutes=leg.duration_minutes if leg else None,
                    meters=leg.distance_meters if leg else None,
                    polyline=leg.polyline if leg else None,
                    status=status,
                )
            )

    drawn = [leg for leg in legs if leg.status == "ok"]
    missing = [leg for leg in legs if leg.status == "no_route"]
    failed = [leg for leg in legs if leg.status == "lookup_failed"]

    reasons = []
    if missing:
        reasons.append(f"{len(missing)} with no {missing[0].mode} route Google knows of")
    if failed:
        reasons.append(f"{len(failed)} the routing lookup could not answer")
    return DayRoute(
        date=when,
        legs=legs,
        geometry="route" if drawn else "straight_line",
        note=(
            None
            if not reasons
            else f"of {len(legs)} legs, " + " and ".join(reasons) + "; those stay straight lines"
        ),
    )


@router.post("/weather", response_model=ActionResult)
async def refresh_weather(trip_id: str, session: SessionDep) -> ActionResult:
    """Fetch weather for every day and store it on the days.

    Stored rather than fetched on read: validation is pure and a GET should not
    make a network call. `observed_at` travels with each context, so an old
    forecast can be shown as old rather than passed off as current.
    """
    state = await _load(session, trip_id)
    days = state.itinerary.days
    if not days:
        return _view(state, state, applied=False, summary="no days to look up")

    anchor = _anchor(state)
    if anchor is None:
        return _view(
            state,
            state,
            applied=False,
            summary="this trip has no place to look weather up for yet",
        )

    try:
        toolbox = Toolbox(sessions=get_sessionmaker())
    except MissingCredentials:
        return _view(state, state, applied=False, summary="weather is not configured")

    async with toolbox:
        contexts = await toolbox.weather.context_for(
            [day.date for day in days],
            lat=anchor[0],
            lng=anchor[1],
            today=today_at(state),
        )

    operations = [
        {
            "op": "set",
            "path": f"/itinerary/days/{index}/weather",
            "value": contexts[day.date].model_dump(mode="json"),
        }
        for index, day in enumerate(days)
        if day.date in contexts
    ]
    kinds = {contexts[day.date].kind for day in days if day.date in contexts}
    return await _commit(
        session,
        state,
        [
            TripPatch(
                base_revision=state.revision,
                reason="weather refreshed",
                actor="user",
                operations=operations,
            )
        ],
        summary=f"weather updated ({', '.join(sorted(kinds))})",
    )


def _anchor(state: TripState) -> tuple[float, float] | None:
    """A point to look weather up at: the first place the trip actually knows.

    Weather is looked up once per trip rather than per stop. A five-stop day on
    one island shares its weather, and asking five times would spend five calls
    to produce the same answer.
    """
    for _day, item in state.itinerary.iter_items():
        entity = state.entities.get(item.entity_id) if item.entity_id else None
        if entity is not None:
            return entity.lat, entity.lng
    for entity in state.entities.values():
        return entity.lat, entity.lng
    return None


# --- locking -----------------------------------------------------------------


@router.post("/items/{item_id}/lock", response_model=ActionResult)
async def lock_item(
    trip_id: str, item_id: str, payload: LockRequest, session: SessionDep
) -> ActionResult:
    state = await _load(session, trip_id)
    _find_item(state, item_id)

    if _lock_for(state, item_id) is not None:
        return _view(state, state, applied=False, summary="already locked")

    record = LockRecord(target_kind="itinerary_item", target_id=item_id, reason=payload.reason)
    return await _commit(
        session,
        state,
        [
            TripPatch(
                base_revision=state.revision,
                reason=f"lock {item_id}",
                actor="user",
                operations=[
                    {"op": "add", "path": "/locks/-", "value": record.model_dump(mode="json")}
                ],
            )
        ],
        summary="locked",
    )


@router.post("/items/{item_id}/unlock", response_model=ActionResult)
async def unlock_item(trip_id: str, item_id: str, session: SessionDep) -> ActionResult:
    state = await _load(session, trip_id)
    lock = _lock_for(state, item_id)
    if lock is None:
        return _view(state, state, applied=False, summary="not locked")

    remaining = [entry for entry in state.locks if entry.lock_id != lock.lock_id]
    return await _commit(
        session,
        state,
        [
            TripPatch(
                base_revision=state.revision,
                reason=f"unlock {item_id}",
                actor="user",
                # A `set` of what survives, not a `remove` by index: the engine
                # strips anything named in `unlock_targets` from its working
                # copy *before* operations run, so an index-based remove would
                # be addressing a list that has already shifted under it.
                operations=[
                    {
                        "op": "set",
                        "path": "/locks",
                        "value": [entry.model_dump(mode="json") for entry in remaining],
                    }
                ],
                # Releasing a lock requires naming it, or a patch could drop its
                # own obstacle and then edit freely.
                unlock_targets=[lock.lock_id],
            )
        ],
        summary="unlocked",
    )


# --- moving, removing, replacing ---------------------------------------------


@router.post("/items/{item_id}/move", response_model=ActionResult)
async def move_item(
    trip_id: str, item_id: str, payload: MoveRequest, session: SessionDep
) -> ActionResult:
    state = await _load(session, trip_id)
    _refuse_if_locked(state, item_id, "move")
    day, item = _find_item(state, item_id)

    if payload.to_date is None and payload.to_time is None:
        raise HTTPException(422, "give a to_date, a to_time, or both")

    target_date = payload.to_date or day.date
    target_index = _day_index(state, target_date)

    if payload.to_time:
        try:
            clock = time.fromisoformat(payload.to_time)
        except ValueError:
            raise HTTPException(422, f"to_time must be HH:MM, got {payload.to_time!r}") from None
    else:
        clock = (item.start_at or datetime.combine(target_date, time(10, 0))).time()

    length = (
        item.end_at - item.start_at
        if item.start_at and item.end_at
        else datetime.combine(target_date, time(11, 0)) - datetime.combine(target_date, time(10, 0))
    )
    start = datetime.combine(target_date, clock)
    moved = item.model_copy(update={"start_at": start, "end_at": start + length})

    source_index = _day_index(state, day.date)
    operations: list[dict[str, Any]] = []

    if target_date == day.date:
        items = sorted(
            [moved if other.item_id == item_id else other for other in day.items],
            key=lambda entry: entry.start_at or start,
        )
        operations.append(
            {
                "op": "set",
                "path": f"/itinerary/days/{source_index}/items",
                "value": [entry.model_dump(mode="json") for entry in items],
            }
        )
        # One day changed, so the patch may honestly claim day scope.
        scope = {"kind": "itinerary_day", "target_id": day.date.isoformat()}
    else:
        remaining = [other for other in day.items if other.item_id != item_id]
        arriving = sorted(
            [*state.itinerary.days[target_index].items, moved],
            key=lambda entry: entry.start_at or start,
        )
        operations += [
            {
                "op": "set",
                "path": f"/itinerary/days/{source_index}/items",
                "value": [entry.model_dump(mode="json") for entry in remaining],
            },
            {
                "op": "set",
                "path": f"/itinerary/days/{target_index}/items",
                "value": [entry.model_dump(mode="json") for entry in arriving],
            },
        ]
        # Two days changed. Claiming day scope here is exactly what check_scope
        # exists to catch, so the patch claims none.
        scope = None

    return await _commit(
        session,
        state,
        [
            TripPatch(
                base_revision=state.revision,
                reason=f"move {item.title}",
                actor="user",
                operations=operations,
                scope=scope,
            )
        ],
        summary=f"moved {item.title}",
    )


@router.delete("/items/{item_id}", response_model=ActionResult)
async def remove_item(trip_id: str, item_id: str, session: SessionDep) -> ActionResult:
    state = await _load(session, trip_id)
    _refuse_if_locked(state, item_id, "remove")
    day, item = _find_item(state, item_id)

    index = _day_index(state, day.date)
    remaining = [entry for entry in day.items if entry.item_id != item_id]

    return await _commit(
        session,
        state,
        [
            TripPatch(
                base_revision=state.revision,
                reason=f"remove {item.title}",
                actor="user",
                operations=[
                    {
                        "op": "set",
                        "path": f"/itinerary/days/{index}/items",
                        "value": [entry.model_dump(mode="json") for entry in remaining],
                    }
                ],
                scope={"kind": "itinerary_day", "target_id": day.date.isoformat()},
            )
        ],
        summary=f"removed {item.title}",
    )


@router.post("/items/{item_id}/replace", response_model=ActionResult)
async def replace_item(
    trip_id: str, item_id: str, payload: ReplaceRequest, session: SessionDep
) -> ActionResult:
    """Swap in the best alternative the trip already researched.

    Deliberately not a synthesized chat message: the intent is structured, so it
    stays structured. When nothing qualifies the answer is the reason, not a
    substitute nobody vouched for.
    """
    state = await _load(session, trip_id)
    _refuse_if_locked(state, item_id, "replace")
    _find_item(state, item_id)

    day, _ = _find_item(state, item_id)
    proposal = substitute_item(state, item_id, travel=await _measure(state, [day]))
    if proposal.is_empty:
        return _view(
            state,
            state,
            applied=False,
            summary=proposal.summary,
            warnings=proposal.warnings,
        )

    return await _commit(
        session,
        state,
        _proposal_patches(state, proposal, f"replace item {item_id}"),
        summary=proposal.summary,
        warnings=proposal.warnings,
    )


@router.post("/entities/{entity_id}/reject", response_model=ActionResult)
async def reject_entity(
    trip_id: str, entity_id: str, payload: RejectRequest, session: SessionDep
) -> ActionResult:
    """"Not interested": remember it, and take it off the plan.

    Both halves matter. The record stops it being re-suggested; unscheduling it
    stops the user from having said no to something still sitting in their day.
    """
    state = await _load(session, trip_id)
    entity = state.entities.get(entity_id)
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no place {entity_id} on this trip")

    locked = {
        lock.target_id for lock in state.locks if lock.target_kind == "itinerary_item"
    }
    scheduled = [
        (day, item)
        for day, item in state.itinerary.iter_items()
        if item.entity_id == entity_id
    ]
    if any(item.item_id in locked for _day, item in scheduled):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this place is locked into the itinerary; unlock it before rejecting it",
        )

    record = RejectionRecord(
        target_kind="entity",
        target_id=entity_id,
        label=entity.name,
        reason=payload.reason,
    )
    operations: list[dict[str, Any]] = [
        {"op": "add", "path": "/rejections/-", "value": record.model_dump(mode="json")}
    ]

    for day in {day.date for day, _item in scheduled}:
        index = _day_index(state, day)
        remaining = [
            entry
            for entry in state.itinerary.days[index].items
            if entry.entity_id != entity_id
        ]
        operations.append(
            {
                "op": "set",
                "path": f"/itinerary/days/{index}/items",
                "value": [entry.model_dump(mode="json") for entry in remaining],
            }
        )

    return await _commit(
        session,
        state,
        [
            TripPatch(
                base_revision=state.revision,
                reason=f"not interested in {entity.name}",
                actor="user",
                operations=operations,
            )
        ],
        summary=f"rejected {entity.name}",
    )


@router.post("/days/{when}/replan", response_model=ActionResult)
async def replan_one_day(
    trip_id: str, when: date_type, payload: ReplanRequest, session: SessionDep
) -> ActionResult:
    state = await _load(session, trip_id)
    _day_index(state, when)

    proposal = replan_day(
        state,
        when,
        travel=await _measure(state, [state.itinerary.days[_day_index(state, when)]]),
        params=ReplanParams(
            intensity=payload.intensity,
            max_items=payload.max_items,
            keep_item_ids=payload.keep_item_ids,
            drop_item_ids=payload.drop_item_ids,
        ),
    )
    if proposal.is_empty or _is_noop(state, proposal):
        # A replan that arrives at the day it started from should not spend a
        # revision saying so. Committing it would be honest about the patch and
        # misleading about the trip: the number advances, the diff is empty, and
        # the user is left wondering what they missed.
        return _view(
            state,
            state,
            applied=False,
            summary=proposal.summary if proposal.is_empty else "nothing needed changing",
            warnings=proposal.warnings,
        )

    return await _commit(
        session,
        state,
        _proposal_patches(state, proposal, f"replan {when}"),
        summary=proposal.summary,
        warnings=proposal.warnings,
    )


# --- explanations ------------------------------------------------------------


@router.get("/items/{item_id}/why", response_model=Explanation)
async def why_item(trip_id: str, item_id: str, session: SessionDep) -> Explanation:
    state = await _load(session, trip_id)
    explanation = explain_item(state, item_id)
    if explanation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no item {item_id} on this trip")
    return explanation


@router.get("/entities/{entity_id}/why", response_model=Explanation)
async def why_entity(trip_id: str, entity_id: str, session: SessionDep) -> Explanation:
    return explain(await _load(session, trip_id), entity_id)
