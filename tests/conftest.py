import os
from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Keep the app's startup hook away from the developer's real database.
os.environ["AUTO_CREATE_TABLES"] = "false"

from app.db.models import Base  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    BudgetSpec,
    Decision,
    DecisionOption,
    DestinationOption,
    DestinationSpec,
    ItineraryDay,
    ItineraryItem,
    Money,
    PartySpec,
    PlaceEntity,
    TripBrief,
    TripDates,
    TripDecisions,
    TripItinerary,
    TripState,
    TripTraveler,
)

TRIP_START = date(2026, 10, 3)
TRIP_END = date(2026, 10, 8)
DAY_ONE = date(2026, 10, 3)


# --- Fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    created = create_async_engine(url, future=True)
    async with created.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def session(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as open_session:
        yield open_session


@pytest_asyncio.fixture
async def client(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as open_session:
            yield open_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


# --- Builders ----------------------------------------------------------------


def make_entity(entity_id: str = "ent_cafe", name: str = "Fuglen Tokyo") -> PlaceEntity:
    return PlaceEntity(
        entity_id=entity_id,
        name=name,
        categories=["cafe"],
        lat=35.6659,
        lng=139.6979,
        rating=4.3,
        rating_count=2300,
        provider_refs={"google_place_id": f"place_{entity_id}"},
    )


def make_item(
    item_id: str = "item_dinner",
    *,
    title: str = "Dinner",
    entity_id: str | None = "ent_cafe",
    start: datetime | None = None,
    end: datetime | None = None,
    flexibility: str = "fixed",
    cost: float | None = 120.0,
) -> ItineraryItem:
    return ItineraryItem(
        item_id=item_id,
        type="restaurant",
        entity_id=entity_id,
        title=title,
        start_at=start or datetime(2026, 10, 3, 19, 0),
        end_at=end or datetime(2026, 10, 3, 21, 0),
        time_flexibility=flexibility,
        estimated_cost=Money(amount=cost) if cost is not None else None,
    )


def sample_state() -> TripState:
    """A small but complete trip: two entities, one day, two items, one decision."""
    state = TripState.new(title="Tokyo food trip", created_by="user_haoyu")
    state.brief = TripBrief(
        destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
        dates=TripDates(start=TRIP_START, end=TRIP_END),
        party=PartySpec(adults=4, rooms=2),
        budget=BudgetSpec(total_per_person=2500, hotel_per_night=250),
        priorities=["food", "city exploration"],
    )
    state.travelers = [
        TripTraveler(traveler_id="trv_a", name="Haoyu", role="organizer"),
        TripTraveler(traveler_id="trv_b", name="Alice"),
    ]
    state.entities = {
        "ent_cafe": make_entity("ent_cafe", "Fuglen Tokyo"),
        "ent_museum": make_entity("ent_museum", "Nezu Museum"),
    }
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=DAY_ONE,
                theme="Shibuya",
                items=[
                    make_item(
                        "item_museum",
                        title="Nezu Museum",
                        entity_id="ent_museum",
                        start=datetime(2026, 10, 3, 14, 0),
                        end=datetime(2026, 10, 3, 16, 0),
                        flexibility="window",
                        cost=40.0,
                    ),
                    make_item("item_dinner", title="Dinner at Fuglen", entity_id="ent_cafe"),
                ],
            )
        ]
    )
    state.decisions = TripDecisions(
        destination=Decision[DestinationOption](
            decision_id="dec_dest",
            status="selected",
            options=[
                DecisionOption[DestinationOption](
                    option_id="opt_tokyo",
                    data=DestinationOption(city="Tokyo", country="Japan"),
                    status="selected",
                ),
                DecisionOption[DestinationOption](
                    option_id="opt_osaka",
                    data=DestinationOption(city="Osaka", country="Japan"),
                    status="candidate",
                ),
            ],
            selected_option_id="opt_tokyo",
        )
    )
    return state
