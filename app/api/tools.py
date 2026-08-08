"""Debug endpoints for the external-data tools.

These exist so Milestone 2 is pokeable from /docs the same way Milestone 1 was.
They are read-only: nothing here writes to a trip. In M3 the agent calls the
same services directly, and any resulting change goes through apply_trip_patch
like every other mutation.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import TripNotFound, TripRepository
from app.db.session import get_session, get_sessionmaker
from app.models.decision import HotelOptionData
from app.models.hotel import SearchHotelsInput
from app.models.place import GetPlaceDetailsInput, PlaceSummary, SearchPlacesInput
from app.models.route import GetRoutesInput, RouteLeg, TravelMode
from app.models.tool import ToolResult
from app.services.hotel_area_service import RankedArea
from app.services.toolbox import MissingCredentials, Toolbox

router = APIRouter(prefix="/tools", tags=["tools"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _toolbox() -> Toolbox:
    try:
        return Toolbox(sessions=get_sessionmaker())
    except MissingCredentials as exc:
        raise HTTPException(503, str(exc)) from None


@router.post("/search_places", response_model=ToolResult[PlaceSummary])
async def search_places(spec: SearchPlacesInput) -> ToolResult[PlaceSummary]:
    async with await _toolbox() as toolbox:
        return await toolbox.places.search_places(spec)


@router.post("/place_details", response_model=ToolResult[PlaceSummary])
async def place_details(spec: GetPlaceDetailsInput) -> ToolResult[PlaceSummary]:
    async with await _toolbox() as toolbox:
        return await toolbox.places.get_place_details(spec)


@router.post("/get_routes", response_model=ToolResult[RouteLeg])
async def get_routes(spec: GetRoutesInput) -> ToolResult[RouteLeg]:
    async with await _toolbox() as toolbox:
        return await toolbox.routes.get_routes(spec)


@router.post("/trips/{trip_id}/hotel_areas", response_model=ToolResult[RankedArea])
async def recommend_hotel_areas(
    trip_id: str,
    session: SessionDep,
    suggested_areas: list[str] | None = None,
    mode: TravelMode = "transit",
) -> ToolResult[RankedArea]:
    """Rank neighbourhoods for one stored trip.

    Takes a trip id rather than a free-standing request because an area is only
    convenient relative to somewhere: the anchors come from what this trip has
    already scheduled or shortlisted.
    """
    try:
        state = await TripRepository(session).get(trip_id)
    except TripNotFound:
        raise HTTPException(404, f"no trip {trip_id!r}") from None

    async with await _toolbox() as toolbox:
        return await toolbox.hotel_areas.recommend_areas(
            state, suggested_areas=suggested_areas, mode=mode
        )


@router.post("/trips/{trip_id}/hotels", response_model=ToolResult[HotelOptionData])
async def search_hotels(
    trip_id: str, spec: SearchHotelsInput, session: SessionDep
) -> ToolResult[HotelOptionData]:
    """Search hotels for one stored trip.

    Refuses without a resolved area, the same as the agent tool - the ordering
    is enforced in the service, so every caller gets it whether or not they
    remembered.
    """
    try:
        state = await TripRepository(session).get(trip_id)
    except TripNotFound:
        raise HTTPException(404, f"no trip {trip_id!r}") from None

    async with await _toolbox() as toolbox:
        if toolbox.hotels is None:
            raise HTTPException(503, "hotel search is not configured (no SERPAPI_API_KEY)")
        return await toolbox.hotels.search_hotels(spec, state=state)
