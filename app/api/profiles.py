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
    """Create a profile, refusing an id that is already taken.

    A duplicate used to reach the database as an unhandled IntegrityError and
    come back as a bare `500 Internal Server Error` with an empty body - so
    the very first call of `scripts/demo_milestone1.py` crashed on every run
    after the first, and the client was told nothing about why. A taken id is
    an ordinary answer to an ordinary request, and 409 is what it is called.
    """
    repo = ProfileRepository(session)
    try:
        await repo.get(profile.profile_id)
    except ProfileNotFound:
        return await repo.create(profile)
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        f"profile {profile.profile_id} already exists; PATCH it to change it",
    )


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
