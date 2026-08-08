from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import TripNotFound, TripRepository
from app.db.session import get_session
from app.models.patch import PatchResult, TripPatch
from app.models.trip import TripBrief, TripState, TripSummary, TripTraveler

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
