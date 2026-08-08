from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import ProfileNotFound, ProfileRepository
from app.db.session import get_session
from app.models.traveler import TravelerProfile

router = APIRouter(prefix="/profiles", tags=["profiles"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("", response_model=TravelerProfile, status_code=status.HTTP_201_CREATED)
async def create_profile(profile: TravelerProfile, session: SessionDep) -> TravelerProfile:
    return await ProfileRepository(session).create(profile)


@router.get("", response_model=list[TravelerProfile])
async def list_profiles(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TravelerProfile]:
    return await ProfileRepository(session).list_profiles(limit=limit, offset=offset)


@router.get("/{profile_id}", response_model=TravelerProfile)
async def get_profile(profile_id: str, session: SessionDep) -> TravelerProfile:
    try:
        return await ProfileRepository(session).get(profile_id)
    except ProfileNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"profile {profile_id} not found") from None


@router.patch("/{profile_id}", response_model=TravelerProfile)
async def update_profile(
    profile_id: str,
    changes: Annotated[dict[str, Any], Body()],
    session: SessionDep,
) -> TravelerProfile:
    try:
        return await ProfileRepository(session).update(profile_id, changes)
    except ProfileNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"profile {profile_id} not found") from None
    except ValueError as exc:
        # Literal 422: the Starlette constant was renamed across versions.
        raise HTTPException(422, str(exc)) from None
