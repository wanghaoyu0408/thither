"""Debug endpoints for the external-data tools.

These exist so Milestone 2 is pokeable from /docs the same way Milestone 1 was.
They are read-only: nothing here writes to a trip. In M3 the agent calls the
same services directly, and any resulting change goes through apply_trip_patch
like every other mutation.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session, get_sessionmaker
from app.models.place import GetPlaceDetailsInput, PlaceSummary, SearchPlacesInput
from app.models.route import GetRoutesInput, RouteLeg
from app.models.tool import ToolResult
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
