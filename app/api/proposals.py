"""Answering a fork the agent put to the traveller.

The agent could always work out what to do when a search came back empty - the
reply that prompted this said, correctly, that the practical next step was to
fly into O'Hare instead. What it could not do was make that pressable. This is
the other half: one button, one action, through the same patch engine as every
other write.

The action a button performs comes from `ProposalChoice`, whose `action` is a
closed set. Nothing here executes anything the traveller could not have done
themselves through an existing route - answering just saves them finding it.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.actions import ActionResult, _commit, _load, _view
from app.api.decisions import _decision, _patch, _pointer, _refuse_if_locked, select_ops
from app.db.session import get_session
from app.models.common import utcnow
from app.models.proposal import AgentProposal, ProposalChoice
from app.models.trip import TripState

router = APIRouter(prefix="/trips/{trip_id}/proposals", tags=["proposals"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_PART_DECISION = {"flights": "flights", "lodging": "hotel"}


class AnswerRequest(BaseModel):
    choice_index: int


def _proposal(state: TripState, proposal_id: str) -> tuple[int, AgentProposal]:
    for index, proposal in enumerate(state.proposals):
        if proposal.proposal_id == proposal_id:
            return index, proposal
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no proposal {proposal_id!r} on this trip")


def _action_ops(state: TripState, choice: ProposalChoice) -> list[dict[str, Any]]:
    """What pressing this button does, beyond recording that it was pressed."""
    if choice.action == "none":
        return []

    if choice.action == "select_option":
        decision = _decision(state, choice.decision or "")
        _refuse_if_locked(state, decision, "choose a different option")
        if decision.selected_option_id == choice.option_id:
            return []
        return select_ops(decision, choice.decision or "", choice.option_id or "")

    # set_aside / resume. The reason is the traveller's answer, kept so that
    # "why are there no flights in my trip?" is answerable from stored state.
    name = _PART_DECISION.get(choice.part or "")
    if name is None:
        raise HTTPException(422, f"cannot set aside {choice.part!r}")
    decision = getattr(state.decisions, name, None)
    if decision is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"nothing has been researched for {choice.part} yet, so there is nothing to park",
        )
    reason = choice.note or choice.label if choice.action == "set_aside" else None
    return [{"op": "set", "path": f"{_pointer(name)}/set_aside_reason", "value": reason}]


@router.post("/{proposal_id}/answer", response_model=ActionResult)
async def answer_proposal(
    trip_id: str, proposal_id: str, payload: AnswerRequest, session: SessionDep
) -> ActionResult:
    """Press one of the buttons. The action and the answer land together."""
    state = await _load(session, trip_id)
    index, proposal = _proposal(state, proposal_id)

    if proposal.answered:
        # Answering twice must not spend a revision saying nothing changed -
        # and must not run the action a second time.
        return _view(state, state, applied=False, summary="already answered")

    if not 0 <= payload.choice_index < len(proposal.choices):
        raise HTTPException(422, f"choice_index must be 0..{len(proposal.choices) - 1}")
    choice = proposal.choices[payload.choice_index]

    base = f"/proposals/{index}"
    operations = [
        *_action_ops(state, choice),
        {"op": "set", "path": f"{base}/answered_at", "value": utcnow().isoformat()},
        {"op": "set", "path": f"{base}/chosen_label", "value": choice.label},
    ]

    return await _commit(
        session,
        state,
        [_patch(state, f"answered: {choice.label}"[:80], operations)],
        summary=choice.note or choice.label,
    )
