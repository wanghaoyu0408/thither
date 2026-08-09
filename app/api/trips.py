from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import TripNotFound, TripRepository
from app.db.session import get_session
from app.models.patch import PatchResult, TripPatch
from app.models.trip import TripBrief, TripState, TripSummary, TripTraveler
from app.services.conflict_service import detect_conflicts, unresolved_blocking
from app.services.validation_service import validate_itinerary

router = APIRouter(prefix="/trips", tags=["trips"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class CreateTripRequest(BaseModel):
    title: str | None = None
    created_by: str | None = None
    brief: TripBrief | None = None
    travelers: list[TripTraveler] = []


def _repo(session: AsyncSession) -> TripRepository:
    return TripRepository(session)


async def _load(session: AsyncSession, trip_id: str) -> TripState:
    try:
        return await _repo(session).get(trip_id)
    except TripNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"trip {trip_id} not found") from None


@router.post("", response_model=TripState, status_code=status.HTTP_201_CREATED)
async def create_trip(payload: CreateTripRequest, session: SessionDep) -> TripState:
    state = TripState.new(
        title=payload.title,
        created_by=payload.created_by,
        brief=payload.brief,
        travelers=payload.travelers,
    )
    return await _repo(session).create(state)


@router.get("", response_model=list[TripSummary])
async def list_trips(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TripSummary]:
    states = await _repo(session).list_trips(limit=limit, offset=offset)
    return [TripSummary.from_state(state) for state in states]


@router.get("/{trip_id}", response_model=TripState)
async def get_trip(trip_id: str, session: SessionDep) -> TripState:
    return await _load(session, trip_id)


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip(trip_id: str, session: SessionDep) -> Response:
    """Remove a trip for good. There is no undo; the UI confirms before calling."""
    try:
        await _repo(session).delete(trip_id)
    except TripNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"trip {trip_id} not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{trip_id}/patch", response_model=PatchResult)
async def patch_trip(
    trip_id: str,
    patch: TripPatch,
    response: Response,
    session: SessionDep,
) -> PatchResult:
    """Apply a validated patch.

    A rejected patch still returns a PatchResult body carrying structured
    errors - the agent needs the error code and pointer to correct itself.
    """
    try:
        result = await _repo(session).apply_patch(trip_id, patch)
    except TripNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"trip {trip_id} not found") from None

    if not result.applied:
        conflicted = any(error.code == "REVISION_CONFLICT" for error in result.errors)
        # Literal 422: the Starlette constant was renamed across versions.
        response.status_code = status.HTTP_409_CONFLICT if conflicted else 422
    return result


# --- Debug endpoints (spec section 32) ---------------------------------------


@router.get("/{trip_id}/state", response_model=TripState)
async def get_trip_state(trip_id: str, session: SessionDep) -> TripState:
    return await _load(session, trip_id)


@router.get("/{trip_id}/overview")
async def get_trip_overview(trip_id: str, session: SessionDep) -> dict[str, Any]:
    """The trip plus everything derived from it, in one call.

    Validation and preference conflicts are computed at read time rather than
    stored (so they can never drift from the state behind them), which means a
    client cannot get them by reading the trip. Rather than have the UI
    reimplement either rule, they are derived here - one round trip, one source
    of truth.
    """
    state = await _load(session, trip_id)
    validation = validate_itinerary(state)

    return {
        "trip": state.model_dump(mode="json"),
        "validation": {
            "status": validation.status,
            "issues": [issue.model_dump(mode="json") for issue in validation.issues],
        },
        "conflicts": [
            conflict.model_dump(mode="json") for conflict in detect_conflicts(state)
        ],
        # What the trip is not allowed to claim yet, and why.
        "blocking": [
            conflict.model_dump(mode="json") for conflict in unresolved_blocking(state)
        ],
    }


@router.get("/{trip_id}/events")
async def get_trip_events(
    trip_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[dict[str, Any]]:
    try:
        return await _repo(session).list_events(trip_id, limit=limit)
    except TripNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"trip {trip_id} not found") from None
