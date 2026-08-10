"""The consent surface of the learning layer, and the reflection intake.

Two rules hold everything here together:

  * **The only path from a hypothesis into a profile is `accept`.** It is
    revision-guarded, it writes the value and its provenance in the same
    update, and nothing else in the codebase writes `learned` fields.
  * **A "no" is durable.** `dismiss` appends a profile-scope RejectionRecord
    keyed by the hypothesis's content-hash id, so however much evidence
    arrives later, the same hypothesis derives as dismissed.

Reflection lives here too because its output is learning evidence: the one
post-trip questionnaire, answered once, written through the trip patch
engine so it is audited and cannot be re-asked.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.actions import ActionResult, _commit, _load, _note_signals
from app.config import get_settings
from app.db.repository import (
    LearningRepository,
    ProfileNotFound,
    ProfileRepository,
    ProfileRevisionConflict,
)
from app.db.session import get_session
from app.models.learning import PreferenceHypothesis, ReflectionItem, TripReflection
from app.models.patch import TripPatch
from app.models.rejection import RejectionRecord
from app.services.intake_service import today_at
from app.services.learning_service import (
    derive_hypotheses,
    profile_changes_for,
    remove_changes_for,
    signals_for_reflection,
)

router = APIRouter(tags=["learning"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --- reflection --------------------------------------------------------------


class ReflectionRequest(BaseModel):
    days_too_busy: list[date] = []
    loved: list[str] = []
    skipped_item_ids: list[str] = []
    notes: str | None = None
    answered_by: str


@router.post("/trips/{trip_id}/reflection", response_model=ActionResult)
async def submit_reflection(
    trip_id: str, payload: ReflectionRequest, session: SessionDep
) -> ActionResult:
    """The post-trip questionnaire. Answered once, after the trip has ended."""
    state = await _load(session, trip_id)

    if state.reflection is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this trip has already been reflected on - it is answered once",
        )
    end = state.brief.dates.end if state.brief.dates else None
    if end is None or today_at(state) <= end:
        raise HTTPException(status.HTTP_409_CONFLICT, "the trip is not over yet")

    traveler = next(
        (t for t in state.travelers if t.traveler_id == payload.answered_by), None
    )
    if traveler is None:
        known = [t.traveler_id for t in state.travelers]
        raise HTTPException(422, f"answered_by must be one of {known}")

    trip_days = {day.date for day in state.itinerary.days}
    unknown_days = [d.isoformat() for d in payload.days_too_busy if d not in trip_days]
    if unknown_days:
        raise HTTPException(422, f"not days of this trip: {unknown_days}")

    items = {item.item_id: item for _day, item in state.itinerary.iter_items()}
    unknown_items = [i for i in payload.skipped_item_ids if i not in items]
    if unknown_items:
        raise HTTPException(422, f"no such items: {unknown_items}")

    reflection = TripReflection(
        days_too_busy=payload.days_too_busy,
        loved=payload.loved,
        # Labels denormalized now, while the items still exist to be named.
        skipped=[
            ReflectionItem(item_id=item_id, label=items[item_id].title)
            for item_id in payload.skipped_item_ids
        ],
        notes=payload.notes,
        answered_by=payload.answered_by,
    )

    result = await _commit(
        session,
        state,
        [
            TripPatch(
                base_revision=state.revision,
                reason="post-trip reflection",
                actor="user",
                operations=[
                    {
                        "op": "set",
                        "path": "/reflection",
                        "value": reflection.model_dump(mode="json"),
                    }
                ],
            )
        ],
        summary="reflection recorded",
    )

    # Signals belong to whoever answered - never to a guess (M9 attribution).
    if result.applied:
        if traveler.profile_id:
            await _note_signals(
                session, list(signals_for_reflection(state, reflection, traveler.profile_id))
            )
        else:
            result.warnings.append(
                f"{traveler.name} has no stored profile, so nothing was learned from this"
            )
    return result


# --- the consent surface -----------------------------------------------------


async def _derived(
    session: AsyncSession, profile_id: str
) -> tuple[Any, list[PreferenceHypothesis]]:
    profiles = ProfileRepository(session)
    try:
        profile = await profiles.get(profile_id)
    except ProfileNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no profile {profile_id}") from None
    signals = await LearningRepository(session).list_for_profile(profile_id)
    hypotheses = derive_hypotheses(profile, signals, settings=get_settings())
    return profile, hypotheses


@router.get("/profiles/{profile_id}/learning")
async def get_learning(profile_id: str, session: SessionDep) -> dict[str, Any]:
    """Everything learned, suspected and declined - with the evidence.

    Derived on every read: same signals, same answer. The evidence lines are
    rendered server-side from stored context, so what "Why?" shows is
    provably what was persisted.
    """
    profile, hypotheses = await _derived(session, profile_id)
    return {
        "profile_id": profile_id,
        "profile_revision": profile.revision,
        "hypotheses": [h.model_dump(mode="json") for h in hypotheses],
        "learned": {
            path: record.model_dump(mode="json") for path, record in profile.learned.items()
        },
        "dismissed": [
            record.model_dump(mode="json")
            for record in profile.learning_rejections
            if record.target_kind == "hypothesis"
        ],
    }


class AcceptRequest(BaseModel):
    expected_revision: int
    # Present when the traveller adjusted the proposed value on the way in.
    value: Any | None = None


class DismissRequest(BaseModel):
    expected_revision: int
    reason: str | None = None


class RemoveRequest(BaseModel):
    expected_revision: int


def _find_hypothesis(
    hypotheses: list[PreferenceHypothesis], hypothesis_id: str
) -> PreferenceHypothesis:
    found = next((h for h in hypotheses if h.hypothesis_id == hypothesis_id), None)
    if found is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no such pattern is supported by the current evidence",
        )
    return found


@router.post("/profiles/{profile_id}/learning/{hypothesis_id}/accept")
async def accept_hypothesis(
    profile_id: str, hypothesis_id: str, payload: AcceptRequest, session: SessionDep
) -> dict[str, Any]:
    """The one path learning takes into a profile. The traveller drives it."""
    profile, hypotheses = await _derived(session, profile_id)
    hypothesis = _find_hypothesis(hypotheses, hypothesis_id)

    if hypothesis.status == "dismissed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "you said no to this; that answer stands"
        )
    if hypothesis.status == "emerging":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "not enough evidence yet - this needs more than one trip behind it",
        )
    edited = payload.value is not None
    if hypothesis.status == "applied" and not edited:
        raise HTTPException(status.HTTP_409_CONFLICT, "already in the profile")

    value = payload.value if edited else hypothesis.proposed_value
    section, field = hypothesis.field_path.split(".", 1)
    before = getattr(getattr(profile, section), field)

    try:
        changes, provenance = profile_changes_for(
            profile, hypothesis, value, value_source="edited" if edited else "proposed"
        )
    except Exception as exc:  # pydantic validation of the new value
        raise HTTPException(422, f"not a valid value for {hypothesis.field_path}: {exc}") from None

    try:
        updated = await ProfileRepository(session).update(
            profile_id, changes, expected_revision=payload.expected_revision
        )
    except ProfileRevisionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return {
        "profile": updated.model_dump(mode="json"),
        "change": {"path": hypothesis.field_path, "before": before, "after": value},
        "provenance": provenance.model_dump(mode="json"),
    }


@router.post("/profiles/{profile_id}/learning/{hypothesis_id}/dismiss")
async def dismiss_hypothesis(
    profile_id: str, hypothesis_id: str, payload: DismissRequest, session: SessionDep
) -> dict[str, Any]:
    """"Not really." Durable: the same hypothesis never proposes itself again."""
    profile, hypotheses = await _derived(session, profile_id)
    hypothesis = _find_hypothesis(hypotheses, hypothesis_id)

    if hypothesis.status == "dismissed":
        return {"dismissed": hypothesis_id, "already": True}

    record = RejectionRecord(
        target_kind="hypothesis",
        target_id=hypothesis_id,
        label=hypothesis.summary,
        scope="profile",
        reason=payload.reason,
    )
    rejections = [r.model_dump(mode="json") for r in profile.learning_rejections]
    rejections.append(record.model_dump(mode="json"))

    try:
        await ProfileRepository(session).update(
            profile_id,
            {"learning_rejections": rejections},
            expected_revision=payload.expected_revision,
        )
    except ProfileRevisionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return {"dismissed": hypothesis_id, "already": False}


@router.post("/profiles/{profile_id}/learning/{hypothesis_id}/remove")
async def remove_learned(
    profile_id: str, hypothesis_id: str, payload: RemoveRequest, session: SessionDep
) -> dict[str, Any]:
    """Take a learned value back out, and keep it from returning.

    Reverts to the pre-learning value (not the model default) and appends the
    dismissal in the same update - without it, the untouched evidence would
    re-propose the removed value on the very next read. No trip snapshot is
    touched, ever.
    """
    profile, _hypotheses = await _derived(session, profile_id)

    field_path = next(
        (
            path
            for path, record in profile.learned.items()
            if record.hypothesis_id == hypothesis_id
        ),
        None,
    )
    if field_path is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "nothing learned under that hypothesis"
        )

    changes = remove_changes_for(profile, field_path)

    provenance = profile.learned[field_path]
    record = RejectionRecord(
        target_kind="hypothesis",
        target_id=hypothesis_id,
        label=provenance.summary,
        scope="profile",
        reason="removed from the profile",
    )
    rejections = [r.model_dump(mode="json") for r in profile.learning_rejections]
    if not any(r["target_id"] == hypothesis_id for r in rejections):
        rejections.append(record.model_dump(mode="json"))
    changes["learning_rejections"] = rejections

    try:
        updated = await ProfileRepository(session).update(
            profile_id, changes, expected_revision=payload.expected_revision
        )
    except ProfileRevisionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return {"removed": field_path, "profile": updated.model_dump(mode="json")}
