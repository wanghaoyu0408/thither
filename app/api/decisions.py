"""Acting on the decisions of a trip: choose, lock, reject, mark as booked.

Same contract as `app/api/actions.py`, and deliberately the same helpers -
validate, commit atomically, bump the revision, reload, *then* report. A
decision changed from this router goes through the identical gates as one
changed by the agent, including the rejection memory and the lock enforcement
that were written for the patch engine long before anything created a decision
lock.

`booked` is set here and nowhere else. It is not a status and not a lock: it is
a fact about the world, and the tools read it to stop recommending alternatives
to something already paid for.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.actions import ActionResult, SessionDep, _commit, _load, _view
from app.models.common import utcnow
from app.models.decision import Decision
from app.models.lock import LockRecord
from app.models.patch import TripPatch
from app.models.rejection import RejectionRecord
from app.models.trip import TripState
from app.services import json_pointer as jp
from app.services.decision_service import DecisionView, decision_views, visible
from app.services.explanation_service import Explanation, explain_option

router = APIRouter(prefix="/trips/{trip_id}/decisions", tags=["decisions"])


class SelectRequest(BaseModel):
    option_id: str


class DecisionLockRequest(BaseModel):
    reason: str = "the traveller settled this"


class BookedRequest(BaseModel):
    booked: bool = True
    reference: str | None = None


class RejectOptionRequest(BaseModel):
    reason: str | None = None


def _decision(state: TripState, name: str) -> Decision:
    stored = dict(state.decisions.iter_decisions())
    found = stored.get(name)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no decision {name!r} on this trip")
    return found


def _pointer(name: str) -> str:
    """JSON Pointer to a decision, singleton or keyed shortlist."""
    if "." in name:
        group, _, key = name.partition(".")
        return f"/decisions/{group}/{jp.escape(key)}"
    return f"/decisions/{name}"


def _option_index(decision: Decision, option_id: str) -> int:
    for index, option in enumerate(decision.options):
        if option.option_id == option_id:
            return index
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no option {option_id!r} on this decision")


def _lock_record(state: TripState, decision: Decision) -> LockRecord | None:
    return next(
        (
            lock
            for lock in state.locks
            if lock.target_kind == "decision" and lock.target_id == decision.decision_id
        ),
        None,
    )


def _refuse_if_locked(state: TripState, decision: Decision, verb: str) -> None:
    lock = _lock_record(state, decision)
    if lock is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this decision is locked ({lock.reason}); unlock it before trying to {verb}",
        )


def _patch(state: TripState, reason: str, operations: list[dict[str, Any]], **kwargs) -> TripPatch:
    return TripPatch(
        base_revision=state.revision,
        reason=reason,
        actor="user",
        operations=operations,
        **kwargs,
    )


# --- reading -----------------------------------------------------------------


@router.get("", response_model=list[DecisionView])
async def list_decisions(trip_id: str, session: SessionDep) -> list[DecisionView]:
    """What this trip has to decide, what it has decided, and what it refused.

    Derived on read, like validation and conflicts: a decision's relevance
    depends on the brief and its lock state lives in `state.locks`, so neither
    can be answered by reading the decision alone.
    """
    state = await _load(session, trip_id)
    return visible(decision_views(state))


# --- choosing ----------------------------------------------------------------


@router.post("/{name}/select", response_model=ActionResult)
async def select_option(
    trip_id: str, name: str, payload: SelectRequest, session: SessionDep
) -> ActionResult:
    state = await _load(session, trip_id)
    decision = _decision(state, name)
    _refuse_if_locked(state, decision, "choose a different option")

    index = _option_index(decision, payload.option_id)
    chosen = decision.options[index]
    if chosen.status == "rejected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "that option was rejected; reconsider it first if you want it back",
        )
    if decision.selected_option_id == payload.option_id:
        return _view(state, state, applied=False, summary="already selected")

    return await _commit(
        session,
        state,
        [_patch(state, f"select {name}", select_ops(decision, name, payload.option_id))],
        summary=f"selected for {name}",
    )


def select_ops(decision: Decision, name: str, option_id: str) -> list[dict[str, Any]]:
    """The operations that settle a decision on one option.

    Its own function because answering a proposal does the same thing from a
    different button, and two spellings of "choose this" would be one place for
    the demotion below to be forgotten.
    """
    index = _option_index(decision, option_id)
    base = _pointer(name)
    operations: list[dict[str, Any]] = [
        {"op": "set", "path": f"{base}/selected_option_id", "value": option_id},
        {"op": "set", "path": f"{base}/status", "value": "selected"},
        {"op": "set", "path": f"{base}/options/{index}/status", "value": "selected"},
        {"op": "set", "path": f"{base}/updated_at", "value": utcnow().isoformat()},
    ]
    # Anything previously chosen goes back to being a shortlisted option rather
    # than staying marked "selected" alongside the new one.
    for other, option in enumerate(decision.options):
        if other != index and option.status == "selected":
            operations.append(
                {"op": "set", "path": f"{base}/options/{other}/status", "value": "shortlisted"}
            )
    return operations


# --- locking -----------------------------------------------------------------


@router.post("/{name}/lock", response_model=ActionResult)
async def lock_decision(
    trip_id: str, name: str, payload: DecisionLockRequest, session: SessionDep
) -> ActionResult:
    state = await _load(session, trip_id)
    decision = _decision(state, name)
    if _lock_record(state, decision) is not None:
        return _view(state, state, applied=False, summary="already locked")

    record = LockRecord(
        target_kind="decision", target_id=decision.decision_id, reason=payload.reason
    )
    add_lock = {"op": "add", "path": "/locks/-", "value": record.model_dump(mode="json")}
    return await _commit(
        session,
        state,
        [_patch(state, f"lock {name}", [add_lock])],
        summary=f"locked {name}",
    )


@router.post("/{name}/unlock", response_model=ActionResult)
async def unlock_decision(trip_id: str, name: str, session: SessionDep) -> ActionResult:
    state = await _load(session, trip_id)
    decision = _decision(state, name)
    lock = _lock_record(state, decision)
    if lock is None:
        return _view(state, state, applied=False, summary="not locked")

    remaining = [entry for entry in state.locks if entry.lock_id != lock.lock_id]
    return await _commit(
        session,
        state,
        [
            _patch(
                state,
                f"unlock {name}",
                # A `set` of what survives, not a `remove` by index - the engine
                # strips `unlock_targets` from its working copy before the
                # operations run, so an index would address a shifted list.
                [
                    {
                        "op": "set",
                        "path": "/locks",
                        "value": [entry.model_dump(mode="json") for entry in remaining],
                    }
                ],
                unlock_targets=[lock.lock_id],
            )
        ],
        summary=f"unlocked {name}",
    )


# --- booked ------------------------------------------------------------------


@router.post("/{name}/booked", response_model=ActionResult)
async def set_booked(
    trip_id: str, name: str, payload: BookedRequest, session: SessionDep
) -> ActionResult:
    """Mark a decision as arranged outside this system, or take that back.

    The agent reads this and stops offering alternatives. Nothing else changes:
    a booked decision keeps its options and its reasoning, because "why did we
    end up on this flight?" is a question people ask *after* booking.
    """
    state = await _load(session, trip_id)
    decision = _decision(state, name)
    if decision.booked == payload.booked:
        return _view(
            state,
            state,
            applied=False,
            summary="already booked" if payload.booked else "not marked as booked",
        )

    base = _pointer(name)
    stamp: str | None = utcnow().isoformat() if payload.booked else None
    reference: str | None = payload.reference if payload.booked else None
    return await _commit(
        session,
        state,
        [
            _patch(
                state,
                f"{'booked' if payload.booked else 'unbooked'} {name}",
                [
                    {"op": "set", "path": f"{base}/booked", "value": payload.booked},
                    {"op": "set", "path": f"{base}/booked_reference", "value": reference},
                    {"op": "set", "path": f"{base}/booked_at", "value": stamp},
                ],
            )
        ],
        summary=f"{name} marked as booked" if payload.booked else f"{name} no longer booked",
    )


# --- rejecting ---------------------------------------------------------------


@router.get("/{name}/options/{option_id}/why", response_model=Explanation)
async def explain_decision_option(
    trip_id: str, name: str, option_id: str, session: SessionDep
) -> Explanation:
    """Why this option - for flights and neighbourhoods too, not just places.

    `explain` reaches a place through its entity id. A flight has none, so for
    two milestones the flight and area cards had no explanation available at all.
    """
    state = await _load(session, trip_id)
    explanation = explain_option(state, name, option_id)
    if explanation is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no option {option_id!r} on decision {name!r}"
        )
    return explanation


@router.post("/{name}/options/{option_id}/reject", response_model=ActionResult)
async def reject_option(
    trip_id: str, name: str, option_id: str, payload: RejectOptionRequest, session: SessionDep
) -> ActionResult:
    state = await _load(session, trip_id)
    decision = _decision(state, name)
    _refuse_if_locked(state, decision, "reject one of its options")

    index = _option_index(decision, option_id)
    option = decision.options[index]
    if option.status == "rejected":
        return _view(state, state, applied=False, summary="already rejected")

    base = _pointer(name)
    record = RejectionRecord(
        target_kind="decision_option",
        target_id=option_id,
        label=getattr(option.data, "area_name", None) or getattr(option.data, "name", None),
        reason=payload.reason,
    )
    operations: list[dict[str, Any]] = [
        # Both halves in one patch on purpose. The rejection gate refuses a
        # state where a rejected target is still shortlisted or selected, so
        # recording the refusal without demoting the option would be rejected
        # by the engine - correctly.
        {"op": "set", "path": f"{base}/options/{index}/status", "value": "rejected"},
        {"op": "add", "path": "/rejections/-", "value": record.model_dump(mode="json")},
    ]
    if decision.selected_option_id == option_id:
        operations.append({"op": "set", "path": f"{base}/selected_option_id", "value": None})
        operations.append({"op": "set", "path": f"{base}/status", "value": "shortlisted"})

    return await _commit(
        session,
        state,
        [_patch(state, f"reject an option of {name}", operations)],
        summary="rejected",
    )


@router.post("/{name}/options/{option_id}/reconsider", response_model=ActionResult)
async def reconsider_option(
    trip_id: str, name: str, option_id: str, session: SessionDep
) -> ActionResult:
    """Put a rejected option back in play - explicitly, which is the only way.

    Rejections are not forgotten on their own. Something the traveller said no
    to comes back when they say so and not because a later search happened to
    rank it well again.
    """
    state = await _load(session, trip_id)
    decision = _decision(state, name)
    index = _option_index(decision, option_id)

    records = [
        record
        for record in state.rejections
        if record.target_kind == "decision_option" and record.target_id == option_id
    ]
    if decision.options[index].status != "rejected" and not records:
        return _view(state, state, applied=False, summary="that option was not rejected")

    remaining = [record for record in state.rejections if record.target_id != option_id]
    base = _pointer(name)
    return await _commit(
        session,
        state,
        [
            _patch(
                state,
                f"reconsider an option of {name}",
                [
                    {"op": "set", "path": f"{base}/options/{index}/status", "value": "candidate"},
                    {
                        "op": "set",
                        "path": "/rejections",
                        "value": [record.model_dump(mode="json") for record in remaining],
                    },
                ],
            )
        ],
        summary="back in play",
    )
