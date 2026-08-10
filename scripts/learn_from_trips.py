"""Milestone 9 acceptance: the agent learns, and only the traveller applies.

    .\\.venv\\Scripts\\python.exe scripts\\learn_from_trips.py

Six acts:

    1. Trip A (Kyoto, solo): two early activities dragged later - two weak
       signals, recorded as facts.
    2. Derivation: one trip is an emerging suspicion. Nothing is proposed.
    3. Trip B ends; the reflection names a skipped sunrise - now three
       signals across two trips, and the pattern becomes proposable, with
       strength and confidence reported as two separate words.
    4. Accepting writes the profile - value, provenance and revision in one
       update - and Trip B's own snapshot does not move.
    5. Dismissing a different pattern is durable: more evidence never
       resurrects it.
    6. A future trip resolved from the updated profile starts its days at
       11:00 where the old profile's days started at 10:00.

Needs no key at all: learning is deterministic - stored signals in, derived
hypotheses out, template arithmetic at the end.
"""

import asyncio
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import Base
from app.db.repository import LearningRepository, ProfileRepository, TripRepository
from app.models.entity import PlaceEntity
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.learning import ReflectionItem, TripReflection
from app.models.traveler import TravelerProfile
from app.models.trip import (
    DestinationSpec,
    PartySpec,
    TripBrief,
    TripDates,
    TripState,
    TripTraveler,
)
from app.services.itinerary_service import build_itinerary
from app.services.learning_service import (
    derive_hypotheses,
    profile_changes_for,
    signal_for_move,
    signals_for_reflection,
)
from app.services.preference_service import resolve

# Windows pipes stdout as cp1252, which cannot encode the em-dashes the
# evidence lines carry. Without this the script dies on its own output.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SETTINGS = Settings(learning_min_signals=3, learning_min_trips=2)
WIDTH = 74


def banner(title: str) -> None:
    print(f"\n{'=' * WIDTH}\n{title}\n{'-' * WIDTH}")


def show(hypothesis) -> None:
    print(
        f"    {hypothesis.summary:26} {hypothesis.status:10}"
        f"  strength={hypothesis.strength:9} confidence={hypothesis.confidence}"
    )
    for line in hypothesis.evidence:
        print(f"        - {line.line}")


def solo_trip(title: str, start: date, items: list[tuple[str, str, str, str]]) -> TripState:
    state = TripState.new(title=title)
    state.brief = TripBrief(
        destination=DestinationSpec(city="Kyoto", country="Japan"),
        dates=TripDates(start=start, end=start + timedelta(days=3)),
        party=PartySpec(adults=1),
    )
    state.travelers = [
        TripTraveler(traveler_id="trv_1", profile_id="user_haoyu", name="Haoyu",
                     role="organizer")
    ]
    state.itinerary = TripItinerary(days=[
        ItineraryDay(date=start, items=[
            ItineraryItem(
                item_id=item_id, type="activity", title=name,
                start_at=datetime.combine(start, time.fromisoformat(begin)),
                end_at=datetime.combine(start, time.fromisoformat(end)),
                time_flexibility="flexible",
            )
            for item_id, name, begin, end in items
        ])
    ])
    return state


async def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "learn_demo.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async with sessions() as session:
        profiles = ProfileRepository(session)
        learning = LearningRepository(session)
        trips = TripRepository(session)

        profile = await profiles.create(
            TravelerProfile(profile_id="user_haoyu", name="Haoyu")
        )

        banner("1. Trip A: two early activities dragged later")
        trip_a = solo_trip(
            "Kyoto in July", date.today() - timedelta(days=30),
            [("it_1", "Fushimi Inari at dawn", "08:00", "09:30"),
             ("it_2", "Arashiyama bamboo early", "08:30", "10:00")],
        )
        await trips.create(trip_a)
        for item, moved_to in (("Fushimi Inari at dawn", time(11, 0)),
                               ("Arashiyama bamboo early", time(10, 30))):
            signal = signal_for_move(
                trip_a, item_title=item, old_start=time(8, 30) if "bamboo" in item else time(8, 0),
                new_start=moved_to, profile_id="user_haoyu",
            )
            await learning.record(signal)
            print(f"    recorded: {signal.context['item']} "
                  f"{signal.context['from']} -> {signal.context['to']}  ({signal.strength})")

        banner("2. One trip is a suspicion, not a proposal")
        signals = await learning.list_for_profile("user_haoyu")
        for hypothesis in derive_hypotheses(profile, signals, settings=SETTINGS):
            show(hypothesis)
        print("    nothing is proposable: two signals, one trip.")

        banner("3. Trip B ends; the reflection makes it proposable")
        trip_b = solo_trip(
            "Kyoto again, August", date.today() - timedelta(days=9),
            [("it_3", "Kiyomizu at sunrise", "07:30", "09:00"),
             ("it_4", "Gion walk", "15:00", "17:00")],
        )
        await trips.create(trip_b)
        reflection = TripReflection(
            days_too_busy=[trip_b.itinerary.days[0].date],
            skipped=[ReflectionItem(item_id="it_3", label="Kiyomizu at sunrise")],
            loved=["the quiet evening in Gion"],
            answered_by="trv_1",
        )
        for signal in signals_for_reflection(trip_b, reflection, "user_haoyu"):
            await learning.record(signal)
            print(f"    recorded from reflection: {signal.preference_key}  ({signal.strength})")

        signals = await learning.list_for_profile("user_haoyu")
        hypotheses = derive_hypotheses(profile, signals, settings=SETTINGS)
        for hypothesis in hypotheses:
            show(hypothesis)

        banner("4. Accepting writes the profile - and no trip snapshot")
        trip_b.travelers[0].preferences = resolve(trip_b.travelers[0], profile)
        snapshot_before = trip_b.travelers[0].preferences.model_dump(mode="json")

        mornings = next(h for h in hypotheses if h.preference_key == "avoid_early_mornings")
        changes, provenance = profile_changes_for(profile, mornings, mornings.proposed_value)
        profile = await profiles.update(
            "user_haoyu", changes, expected_revision=profile.revision
        )
        print(f"    preferred_start_time: {provenance.previous_value} "
              f"-> {profile.pace_preferences.preferred_start_time}"
              f"   (revision {profile.revision})")
        print(f"    provenance: {len(provenance.signal_ids)} signals "
              f"across {len(provenance.trip_ids)} trips")
        unchanged = trip_b.travelers[0].preferences.model_dump(mode="json") == snapshot_before
        print(f"    Trip B's snapshot unchanged: {unchanged} "
              f"(still names profile revision "
              f"{trip_b.travelers[0].preferences.source_profile_revision})")

        banner("5. A dismissal is durable")
        pace = next(h for h in hypotheses if h.preference_key == "relaxed_pace")
        from app.models.rejection import RejectionRecord

        rejections = [r.model_dump(mode="json") for r in profile.learning_rejections]
        rejections.append(RejectionRecord(
            target_kind="hypothesis", target_id=pace.hypothesis_id,
            label=pace.summary, scope="profile", reason="I like full days",
        ).model_dump(mode="json"))
        profile = await profiles.update(
            "user_haoyu", {"learning_rejections": rejections},
            expected_revision=profile.revision,
        )
        # A flood of new evidence arrives; the answer stands.
        for extra in range(3):
            await learning.record(signals_for_reflection(
                trip_b, reflection, "user_haoyu")[0].model_copy(
                    update={"signal_id": f"sig_extra{extra}", "trip_id": f"trip_x{extra}"}))
        signals = await learning.list_for_profile("user_haoyu")
        for hypothesis in derive_hypotheses(profile, signals, settings=SETTINGS):
            if hypothesis.preference_key == "relaxed_pace":
                print(f"    relaxed_pace after 3 more trips of evidence: {hypothesis.status}")

        banner("6. The next trip starts later because of what was learned")
        old_first = new_first = None
        for label, prof in (("old profile", TravelerProfile(profile_id="u", name="old")),
                            ("learned profile", profile)):
            future = solo_trip("Kyoto, next spring", date.today() + timedelta(days=90), [])
            # Two placeable entities so the balanced template's first slot fills.
            future.entities = {
                "ent_m": PlaceEntity(
                    entity_id="ent_m", name="Nijo Castle", categories=["museum"],
                    lat=35.0142, lng=135.7481, rating=4.4, rating_count=48000,
                    provider_refs={"google_place_id": "place_nijo"},
                ),
                "ent_c": PlaceEntity(
                    entity_id="ent_c", name="Weekenders Coffee", categories=["cafe"],
                    lat=35.0047, lng=135.7630, rating=4.5, rating_count=1200,
                    provider_refs={"google_place_id": "place_weekenders"},
                ),
            }
            future.travelers[0].preferences = resolve(future.travelers[0], prof)
            proposal = build_itinerary(future)
            first = min(
                datetime.fromisoformat(i.start_at).time()
                for d in proposal.days for i in d.items if i.start_at
            )
            print(f"    {label:16} first slot of the day: {first.strftime('%H:%M')}")
            if label == "old profile":
                old_first = first
            else:
                new_first = first

        print(f"\n{'=' * WIDTH}")
        print("The profile changed once, at act 4, by explicit acceptance - and the")
        print(f"generated day moved from {old_first.strftime('%H:%M')} "
              f"to {new_first.strftime('%H:%M')} because of it.")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
