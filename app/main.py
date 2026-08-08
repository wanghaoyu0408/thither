from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import profiles_router, tools_router, trips_router
from app.config import get_settings
from app.db.session import create_all, dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if get_settings().auto_create_tables:
        await create_all()
    yield
    await dispose_engine()


app = FastAPI(
    title="Travel Agent",
    description=(
        "Personal group travel planning agent. Milestone 1: TripState core with a "
        "validated patch engine. Milestone 2: Google Places and Routes behind "
        "replaceable providers."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(profiles_router)
app.include_router(trips_router)
app.include_router(tools_router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "milestone": "1"}
