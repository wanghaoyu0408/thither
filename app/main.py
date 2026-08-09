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
        "maps_key": settings.maps_browser_key,
        # The page shows this as a warning. A shared key means anyone reading
        # the page can also spend the Places and Routes budget.
        "maps_key_shared_with_server": settings.maps_key_is_shared_with_the_server,
    }
