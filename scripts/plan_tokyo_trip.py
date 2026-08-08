"""Milestone 3 acceptance: the two-turn conversation, end to end.

    .\\.venv\\Scripts\\python.exe scripts\\plan_tokyo_trip.py
    .\\.venv\\Scripts\\python.exe scripts\\plan_tokyo_trip.py --trip-id trip_xxx

Turn 1:  "Plan 5 days in Tokyo."
Turn 2:  "Day 3 is too busy. Make it easier."

Prints the tools each turn called, the validation report, and a per-day
before/after diff - so "only Day 3 changed" is something you can see rather
than something the script asserts at you.

Needs GOOGLE_MAPS_API_KEY and OPENAI_API_KEY in .env.
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.runner import AgentRun, AgentRunner
from app.config import get_settings
from app.db.models import Base
from app.db.repository import TripNotFound, TripRepository
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
from app.services.toolbox import MissingCredentials, Toolbox

START = date(2026, 10, 3)


def banner(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'-' * 74}")


def new_trip() -> TripState:
    return TripState.new(
        title="Tokyo food trip",
        created_by="user_haoyu",
        brief=TripBrief(
            destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
            timezone="Asia/Tokyo",
            dates=TripDates(start=START, end=START + timedelta(days=4)),
            party=PartySpec(adults=4, rooms=2),
            budget=BudgetSpec(total_per_person=2500, hotel_per_night=250),
            priorities=["food", "city exploration"],
            pace="balanced",
        ),
        travelers=[
            TripTraveler(traveler_id="trv_a", name="Haoyu", role="organizer"),
            TripTraveler(traveler_id="trv_b", name="Alice"),
            TripTraveler(traveler_id="trv_c", name="Bo"),
            TripTraveler(traveler_id="trv_d", name="Dee"),
        ],
    )


def snapshot(state: TripState) -> dict:
    return {
        day.date: [(item.item_id, item.title, str(item.start_at)) for item in day.items]
        for day in state.itinerary.days
    }


def show_run(run: AgentRun) -> None:
    for tool in run.tools:
        mark = "ok " if tool.ok else "ERR"
        detail = f"  {tool.detail}" if tool.detail else ""
        print(f"    [{mark}] {tool.name:<20} {tool.milliseconds:>5} ms{detail}")
    print(
        f"    revision {run.revision_before} -> {run.revision_after}"
        f"   iterations {run.iterations}"
        f"   tokens {run.input_tokens}/{run.output_tokens}"
    )
    if run.error:
        print(f"    error: {run.error}")
    print(f"\n    {run.reply}\n")


def show_itinerary(state: TripState) -> None:
    for day in state.itinerary.days:
        locked_ids = {
            lock.target_id for lock in state.locks if lock.target_kind == "itinerary_item"
        }
        print(f"    {day.date}  {day.theme or ''}")
        for item in day.items:
            when = f"{item.start_at:%H:%M}" if item.start_at else "--:--"
            mark = " [locked]" if item.item_id in locked_ids else ""
            print(f"        {when}  {item.title}{mark}")
        if not day.items:
            print("        (nothing scheduled)")


async def main(trip_id: str | None) -> int:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("GOOGLE_MAPS_API_KEY", settings.google_maps_api_key),
            ("OPENAI_API_KEY", settings.openai_api_key),
        )
        if not value
    ]
    if missing:
        print(f"\nMissing from .env: {', '.join(missing)}\n")
        return 1

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    try:
        async with Toolbox(settings, sessions) as toolbox, sessions() as session:
            repo = TripRepository(session)
            runner = AgentRunner(
                OpenAIClient(settings.openai_api_key, settings.openai_model),
                toolbox,
                session,
                settings=settings,
            )

            if trip_id:
                try:
                    state = await repo.get(trip_id)
                except TripNotFound:
                    print(f"no such trip: {trip_id}")
                    return 1
            else:
                state = await repo.create(new_trip())
            print(f"trip {state.trip_id}  (model: {settings.openai_model})")

            # --- turn 1 ---------------------------------------------------
            banner('Turn 1:  "Plan 5 days in Tokyo."')
            run = await runner.run(state, "Plan 5 days in Tokyo.")
            show_run(run)

            state = await repo.get(state.trip_id)
            show_itinerary(state)

            if not state.itinerary.days:
                print("\nNo itinerary was produced, so there is nothing to replan.")
                return 1

            before = snapshot(state)
            day_three = state.itinerary.days[min(2, len(state.itinerary.days) - 1)].date

            # --- turn 2 ---------------------------------------------------
            banner(f'Turn 2:  "Day 3 is too busy. Make it easier."   (day 3 = {day_three})')
            run = await runner.run(
                state,
                f"Day 3 ({day_three}) is too busy. Make it easier.",
            )
            show_run(run)

            state = await repo.get(state.trip_id)
            after = snapshot(state)

            # --- the acceptance criterion, visibly ------------------------
            banner("Did only Day 3 change?")
            verdict = True
            for day_date in sorted(set(before) | set(after)):
                old = before.get(day_date, [])
                new = after.get(day_date, [])
                changed = old != new
                expected = day_date == day_three
                if changed != expected and not (not changed and expected):
                    verdict = False
                label = "CHANGED  " if changed else "unchanged"
                flag = "" if changed == expected or not changed else "   <-- UNEXPECTED"
                print(f"    {day_date}  {label}  {len(old)} -> {len(new)} items{flag}")

            unexpected = [
                day_date
                for day_date in before
                if day_date != day_three and before[day_date] != after.get(day_date, [])
            ]
            print()
            if unexpected:
                print(f"    FAILED: these days also changed: {unexpected}")
                verdict = False
            elif before.get(day_three) == after.get(day_three):
                print("    Day 3 did not change either - the replan had no effect.")
            else:
                print("    Only Day 3 changed.")

            banner("Day 3 after")
            show_itinerary(
                state.model_copy(
                    update={
                        "itinerary": state.itinerary.model_copy(
                            update={
                                "days": [d for d in state.itinerary.days if d.date == day_three]
                            }
                        )
                    }
                )
            )

            print(f"\n    trip_id     {state.trip_id}")
            print(f"    revision    {state.revision}")
            print("\n    Re-run against this trip:")
            print(
                f"      .\\.venv\\Scripts\\python.exe scripts\\plan_tokyo_trip.py "
                f"--trip-id {state.trip_id}"
            )
            return 0 if verdict else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trip-id", default=None)
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args.trip_id)))
    except MissingCredentials as exc:
        sys.exit(f"\n{exc}\n")
