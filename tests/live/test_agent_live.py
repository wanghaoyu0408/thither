"""The acceptance criterion as actually specified: a real conversation.

    .\\.venv\\Scripts\\python.exe -m pytest -m live --override-ini addopts=

Skipped unless both keys are set. Assertions are about the *mechanism* - a
five-day itinerary appears, day 3 shrinks, no other day moves - never about
particular restaurants, which change with the model's mood and Google's index.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.runner import AgentRunner
from app.config import get_settings
from app.db.repository import TripRepository
from app.models.trip import (
    BudgetSpec,
    DestinationSpec,
    PartySpec,
    TripBrief,
    TripDates,
    TripState,
    TripTraveler,
)
from app.providers.openai_llm import OpenAIClient
from app.services.toolbox import Toolbox

settings = get_settings()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (settings.openai_api_key and settings.google_maps_api_key),
        reason="needs OPENAI_API_KEY and GOOGLE_MAPS_API_KEY",
    ),
]

START = date(2026, 10, 3)


def tokyo_trip() -> TripState:
    return TripState.new(
        title="Tokyo food trip",
        created_by="user_haoyu",
        brief=TripBrief(
            destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
            timezone="Asia/Tokyo",
            dates=TripDates(start=START, end=START + timedelta(days=4)),
            party=PartySpec(adults=4, rooms=2),
            budget=BudgetSpec(total_per_person=2500),
            priorities=["food"],
            pace="balanced",
        ),
        travelers=[TripTraveler(traveler_id="trv_a", name="Haoyu", role="organizer")],
    )


def day_snapshot(state: TripState) -> dict:
    return {
        day.date: [(item.item_id, str(item.start_at)) for item in day.items]
        for day in state.itinerary.days
    }


@pytest.fixture
async def agent(session, engine):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with Toolbox(settings, sessions) as toolbox:
        yield AgentRunner(
            OpenAIClient(settings.openai_api_key, settings.openai_model),
            toolbox,
            session,
            settings=settings,
        )


async def test_the_two_turn_acceptance_conversation(agent, session):
    repo = TripRepository(session)
    state = await repo.create(tokyo_trip())

    # --- "Plan 5 days in Tokyo." -------------------------------------
    first = await agent.run(state, "Plan 5 days in Tokyo.")

    assert first.error is None, first.error
    assert first.reply, "the agent said nothing"
    # Named explicitly: a truncated turn used to surface here as the mystifying
    # "no itinerary was produced", which points at the planner rather than at
    # the round budget that actually ran out.
    assert not first.hit_iteration_limit, (
        f"the agent was cut off after {first.iterations} rounds and "
        f"{len(first.tools)} tool calls; raise agent_max_iterations rather than "
        f"reading this as a planning failure"
    )

    state = await repo.get(state.trip_id)
    assert state.itinerary.days, "no itinerary was produced"
    assert len(state.itinerary.days) == 5, f"expected 5 days, got {len(state.itinerary.days)}"
    assert first.changed_state is True

    scheduled = [item for day in state.itinerary.days for item in day.items]
    assert scheduled, "the itinerary is empty"
    # Everything scheduled must be a real place the tools found.
    assert all(item.entity_id in state.entities for item in scheduled if item.entity_id)

    before = day_snapshot(state)
    day_three = state.itinerary.days[2].date
    busy_before = len(state.itinerary.days[2].items)

    # --- "Day 3 is too busy. Make it easier." ------------------------
    second = await agent.run(state, f"Day 3 ({day_three}) is too busy. Make it easier.")

    assert second.error is None, second.error
    state = await repo.get(state.trip_id)
    after = day_snapshot(state)

    changed = [d for d in before if before[d] != after.get(d, [])]
    assert changed == [day_three], f"expected only {day_three} to change, but these did: {changed}"

    busy_after = len(state.itinerary.days[2].items)
    assert busy_after <= busy_before, f"day 3 grew from {busy_before} to {busy_after}"


async def test_the_agent_refuses_to_book(agent, session):
    state = await TripRepository(session).create(tokyo_trip())

    run = await agent.run(state, "Great - go ahead and book the first restaurant for us.")

    assert run.error is None
    assert not any(tool.name.startswith("book") for tool in run.tools)
    assert run.reply
