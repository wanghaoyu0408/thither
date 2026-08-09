"""The live Tokyo acceptance, reported from the database rather than from memory.

    .\\.venv\\Scripts\\python.exe scripts\\verify_persistence.py

Runs the two-turn conversation the M3 acceptance uses - "Plan 5 days in Tokyo",
then "Day 3 is too busy" - and then *reloads the trip from SQLite* and validates
the persisted state. Everything reported at the end comes from that reload, not
from what the runner believed it wrote.

That distinction is the point (INVARIANTS.md section 3): a revision reported
without being read back is a claim, not a fact.

Needs GOOGLE_MAPS_API_KEY and OPENAI_API_KEY.
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.runner import AgentRunner
from app.config import get_settings
from app.db.models import Base
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
from app.services.conflict_service import detect_conflicts
from app.services.toolbox import Toolbox
from app.services.validation_service import validate_itinerary

# Japanese place names come back from Google, and Windows pipes stdout as cp1252.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

START = date.today() + timedelta(days=60)
DB_PATH = Path("verify_persistence.db")


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")


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


def report(state: TripState, label: str) -> None:
    result = validate_itinerary(state)
    print(f"    {label}")
    print(f"       revision:   {state.revision}")
    print(f"       status:     {state.status}")
    print(f"       days:       {len(state.itinerary.days)}")
    print(f"       entities:   {len(state.entities)}")
    print(f"       validation: {result.status}")

    by_severity: dict[str, int] = {}
    for issue in result.issues:
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
    print(f"       issues:     {by_severity or 'none'}")
    for issue in result.issues[:8]:
        print(f"         [{issue.severity}] {issue.type}: {issue.message[:88]}")

    conflicts = detect_conflicts(state)
    if conflicts:
        print(f"       conflicts:  {len(conflicts)}")
        for conflict in conflicts[:4]:
            print(f"         [{conflict.severity}] {conflict.kind}: {conflict.summary[:80]}")


async def main() -> int:
    settings = get_settings()
    if not (settings.google_maps_api_key and settings.openai_api_key):
        print("\nNeeds GOOGLE_MAPS_API_KEY and OPENAI_API_KEY in .env\n")
        return 1

    DB_PATH.unlink(missing_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH.as_posix()}", future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with sessions() as session, Toolbox(settings, sessions) as toolbox:
            repository = TripRepository(session)
            state = await repository.create(tokyo_trip())
            runner = AgentRunner(
                OpenAIClient(settings.openai_api_key, settings.openai_model),
                toolbox,
                session,
                settings=settings,
            )

            banner('1. "Plan 5 days in Tokyo."')
            first = await runner.run(state, "Plan 5 days in Tokyo.")
            print(f"    tools:     {[record.name for record in first.tools]}")
            print(f"    reported:  revision {first.revision_after}")
            print(f"    cut short: {first.hit_iteration_limit}")
            if first.error:
                print(f"    error:     {first.error}")

            # The check that matters: what the runner reported against what the
            # database holds. These are only equal because apply_patches
            # re-reads the row after committing.
            persisted = await repository.get(state.trip_id)
            agrees = persisted.revision == first.revision_after
            print(f"    persisted: revision {persisted.revision}")
            print(f"    agreement: {'OK' if agrees else 'MISMATCH'}")

            banner("2. Persisted state after turn 1")
            report(persisted, "reloaded from SQLite")

            if not persisted.itinerary.days:
                print("\n    No itinerary was persisted; stopping before the replan.")
                return 1

            day_three = persisted.itinerary.days[2].date
            before = {
                day.date: [(item.item_id, str(item.start_at)) for item in day.items]
                for day in persisted.itinerary.days
            }

            banner(f'3. "Day 3 ({day_three}) is too busy. Make it easier."')
            second = await runner.run(
                persisted, f"Day 3 ({day_three}) is too busy. Make it easier."
            )
            print(f"    tools:     {[record.name for record in second.tools]}")
            print(f"    reported:  revision {second.revision_after}")
            if second.error:
                print(f"    error:     {second.error}")

            final = await repository.get(state.trip_id)
            agrees = final.revision == second.revision_after
            print(f"    persisted: revision {final.revision}")
            print(f"    agreement: {'OK' if agrees else 'MISMATCH'}")

            after = {
                day.date: [(item.item_id, str(item.start_at)) for item in day.items]
                for day in final.itinerary.days
            }
            changed = [day for day in before if before[day] != after.get(day, [])]
            print(f"    days changed: {[str(day) for day in changed]}")
            print(f"    only day 3:   {'OK' if changed in ([day_three], []) else 'NO'}")

            banner("4. Final persisted state")
            report(final, "reloaded from SQLite")

            events = await repository.list_events(state.trip_id)
            applied = [event for event in events if event["event_type"] == "patch_applied"]
            print(f"\n       audit:      {len(applied)} patch(es) recorded")
            print(f"       revisions:  {[event['revision'] for event in applied]}")

            banner("Result")
            print(f"    Final persisted revision: {final.revision}")
            print(f"    Validation:               {validate_itinerary(final).status}")
            print(
                "\n    Every figure above was read back out of the database after commit,\n"
                "    not taken from what the runner believed it had written."
            )
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
