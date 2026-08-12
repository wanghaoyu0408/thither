from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import (
    actions_router,
    calibration_router,
    chat_router,
    decisions_router,
    intake_router,
    learning_router,
    profiles_router,
    proposals_router,
    tools_router,
    trips_router,
)
from app.config import get_settings
from app.db.session import create_all, dispose_engine
from app.providers.base import close_shared_client

WEB_ROOT = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if get_settings().auto_create_tables:
        await create_all()
    yield
    # The HTTP pool is process-wide, so it is closed once, here - never by
    # the requests that borrow it.
    await close_shared_client()
    await dispose_engine()


app = FastAPI(
    title="问津 · Whither",
    description=(
        "Personal group travel planning agent. M1: TripState with a validated patch "
        "engine. M2: Google Places and Routes behind replaceable providers. "
        "M3: deterministic itinerary generation, validation and scoped local replanning, "
        "driven conversationally. M4: web research resolved against Google. "
        "M5: flights, airport comparison and ranking with stated trade-offs. "
        "M6: hotels, with the neighbourhood decided before anything is priced. "
        "M7: multi-traveler preferences, group scoring and named conflicts. "
        "M9: a learning layer that proposes profile updates and never applies one itself."
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
app.include_router(decisions_router)
app.include_router(intake_router)
app.include_router(learning_router)
app.include_router(calibration_router)
app.include_router(proposals_router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "milestone": "1"}


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    """The web UI.

    One file, and no key baked into it. Everything the interface needs is
    either served from this app or fetched at runtime from `/ui-config`, so the
    page can be read, cached or committed without carrying a secret.

    It loads exactly one thing from outside: Google's Maps JavaScript, and only
    when a key is configured. Without it the workspace still works - the map
    panel says why it is absent instead of leaving a hole.
    """
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/ui-config", include_in_schema=False)
async def ui_config() -> dict[str, object]:
    """What the page needs to know that is not in a trip.

    The Maps key is served here rather than templated into the HTML so the file
    on disk never contains it. That is not secrecy - a key the browser loads is
    public by construction - it is about not committing one to git or handing
    one to whoever gets a copy of the page.
    """
    settings = get_settings()
    return {
        # Only ever the dedicated browser key. The server key is never
        # published, whatever is or is not configured - the fallback that once
        # shared it with the page is gone.
        "maps_key": settings.maps_browser_key,
    }
