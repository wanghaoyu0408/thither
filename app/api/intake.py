"""Answering the intake questions, and confirming the brief.

Confirmation is a persisted state transition, not something read out of the
conversation. It happens here, through the same
validate -> commit -> reload -> report contract as every other write, and it is
the traveller's to make: no tool the agent can call sets `confirmed`.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.actions import ActionResult, SessionDep, _commit, _load, _view
from app.models.common import utcnow
from app.models.patch import TripPatch
from app.models.trip import TripState
from app.services.intake_service import IntakeView, intake_view, missing_blocking

router = APIRouter(prefix="/trips/{trip_id}/intake", tags=["intake"])


class Answer(BaseModel):
    question_id: str
    # Chosen options, for the structured kinds.
    values: list[str] = []
    # What they typed. Always accepted, whatever the question's kind: a
    # multiple-choice question whose options do not fit is worse than none.
    text: str | None = None


class AnswerRequest(BaseModel):
    answers: list[Answer]


class ReopenRequest(BaseModel):
    reason: str


def _patch(state: TripState, reason: str, operations: list[dict[str, Any]]) -> TripPatch:
    return TripPatch(
        base_revision=state.revision, reason=reason, actor="user", operations=operations
    )


@router.get("", response_model=IntakeView)
async def read_intake(trip_id: str, session: SessionDep) -> IntakeView:
    """The brief as a person reads it, plus whatever is still unanswered."""
    return intake_view(await _load(session, trip_id))


@router.post("/answers", response_model=ActionResult)
async def answer_questions(
    trip_id: str, payload: AnswerRequest, session: SessionDep
) -> ActionResult:
    state = await _load(session, trip_id)
    by_id = {
        question.question_id: index for index, question in enumerate(state.intake.questions)
    }

    operations: list[dict[str, Any]] = []
    answered = 0
    for answer in payload.answers:
        index = by_id.get(answer.question_id)
        if index is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no question {answer.question_id!r} on this trip"
            )
        if not answer.values and not (answer.text or "").strip():
            continue
        base = f"/intake/questions/{index}"
        operations.append({"op": "set", "path": f"{base}/values", "value": answer.values})
        operations.append({"op": "set", "path": f"{base}/answer", "value": answer.text})
        operations.append(
            {"op": "set", "path": f"{base}/answered_at", "value": utcnow().isoformat()}
        )
        answered += 1

    if not operations:
        return _view(state, state, applied=False, summary="nothing was answered")

    return await _commit(
        session,
        state,
        [_patch(state, f"answered {answered} intake question(s)", operations)],
        summary=f"answered {answered} question(s)",
    )


@router.post("/confirm", response_model=ActionResult)
async def confirm_brief(trip_id: str, session: SessionDep) -> ActionResult:
    """Start planning.

    Refuses while something blocking is still missing, so "confirmed" never
    means "confirmed except for the parts we do not know". The brief is
    snapshotted here: later edits change the trip, not the record of what was
    agreed.
    """
    state = await _load(session, trip_id)
    if state.intake.status == "confirmed":
        return _view(state, state, applied=False, summary="already confirmed")

    gaps = missing_blocking(state)
    if gaps:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "still missing: " + "; ".join(gap.why for gap in gaps),
        )

    return await _commit(
        session,
        state,
        [
            _patch(
                state,
                "brief confirmed",
                [
                    {"op": "set", "path": "/intake/status", "value": "confirmed"},
                    {
                        "op": "set",
                        "path": "/intake/confirmed_brief",
                        "value": state.brief.model_dump(mode="json"),
                    },
                    # The revision this was agreed at, so a later "what changed
                    # since we agreed?" has something to measure against.
                    {
                        "op": "set",
                        "path": "/intake/confirmed_revision",
                        "value": state.revision,
                    },
                    {
                        "op": "set",
                        "path": "/intake/confirmed_at",
                        "value": utcnow().isoformat(),
                    },
                ],
            )
        ],
        summary="brief confirmed - planning can start",
    )


@router.post("/reopen", response_model=ActionResult)
async def reopen_intake(
    trip_id: str, payload: ReopenRequest, session: SessionDep
) -> ActionResult:
    """Go back to collecting, for a change that genuinely reopens the question.

    Not for every edit. Correcting a traveller count or a priority is an edit;
    this is for a change that makes the confirmed brief wrong - and it requires
    a reason, because reopening throws planning back to the start.
    """
    state = await _load(session, trip_id)
    if state.intake.status == "collecting":
        return _view(state, state, applied=False, summary="intake is already open")

    return await _commit(
        session,
        state,
        [
            _patch(
                state,
                f"intake reopened: {payload.reason}",
                [{"op": "set", "path": "/intake/status", "value": "collecting"}],
            )
        ],
        summary="intake reopened",
    )
