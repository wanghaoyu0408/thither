from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import (
    actions_router,
    chat_router,
    profiles_router,
    tools_router,
    trips_router,
)
from app.config import get_settings
from app.db.session import create_all, dispose_engine

WEB_ROOT = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if get_settings().auto_create_tables:
        await create_all()
    yield
    await dispose_engine()


app = FastAPI(
    title="Travel Agent",
    description=(
        "Personal group travel planning agent. M1: TripState with a validated patch "
        "engine. M2: Google Places and Routes behind replaceable providers. "
        "M3: deterministic itinerary generation, validation and scoped local replanning, "
        "driven conversationally."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(profiles_router)
app.include_router(trips_router)
app.include_router(tools_router)
app.include_router(chat_router)
# Registered after trips so the more specific /trips/{id}/items/... routes are
# matched before the catch-all trip routes.
app.include_router(actions_router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "milestone": "1"}


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    """The web UI.

    One self-contained file with no CDN, so the interface works on the same
    terms as the rest of this project: offline, with nothing fetched from
    somewhere it cannot vouch for.
    """
    return FileResponse(WEB_ROOT / "index.html")
