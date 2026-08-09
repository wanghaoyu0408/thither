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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import TripNotFound, TripRepository
from app.db.session import get_session
from app.models.itinerary_plan import ItineraryProposal, ReplanParams
from app.models.lock import LockRecord
from app.models.patch import PatchError, TripPatch
from app.models.rejection import RejectionRecord
from app.models.trip import TripState
from app.services import json_pointer as jp
from app.services.conflict_service import detect_conflicts, unresolved_blocking
from app.services.explanation_service import Explanation, explain, explain_item
from app.services.itinerary_diff import DayDiff, changed_days
from app.services.itinerary_service import replan_day, substitute_item
from app.services.validation_service import validate_itinerary

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


def _view(
    state: TripState,
    before: TripState,
    *,
    applied: bool,
    summary: str,
    errors: list[PatchError] | None = None,
    warnings: list[str] | None = None,
) -> ActionResult:
    """One response shape, built from the state the caller will actually see."""
    validation = validate_itinerary(state)
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
        )

    return _view(
        results[-1].state, before, applied=True, summary=summary, warnings=warnings
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
            if jp.resolve(document, operation.path) != operation.value:
                return False
        except jp.PointerError:
            return False
    return True


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

    proposal = substitute_item(state, item_id)
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
