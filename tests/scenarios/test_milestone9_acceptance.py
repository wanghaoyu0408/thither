"""Milestone 9 acceptance.

    The agent may learn about the traveler, but must never silently change
    TravelerProfile or rewrite its own system prompt.

Learning is deterministic end to end - signals are stored facts, hypotheses
are pure functions of them, and generation is template arithmetic - so all
nine criteria are provable offline, without a model in the loop and without a
key. The conversational half (the model choosing to call the tools) rides the
same handlers exercised here directly.
"""

from datetime import date, datetime, time, timedelta
from typing import Any

from app.config import Settings
from app.db.repository import LearningRepository, ProfileRepository, TripRepository
from app.models.group import TravelerPreferences
from app.models.learning import LearningSignal
from app.models.traveler import PacePreferences, TravelerProfile
from app.models.trip import TripState, TripTraveler
from app.services.learning_service import derive_hypotheses
from app.services.preference_service import resolve
from tests.conftest import make_item, sample_state

SETTINGS = Settings(learning_min_signals=3, learning_min_trips=2)


# --- builders ----------------------------------------------------------------


def solo_state(profile_id: str | None = "user_solo") -> TripState:
    state = sample_state()
    state.travelers = [
        TripTraveler(
            traveler_id="trv_solo", profile_id=profile_id, name="Haoyu", role="organizer"
        )
    ]
    day = state.itinerary.days[0]
    day.items.insert(
        0,
        make_item(
            "item_sunrise",
            title="Fish market at dawn",
            entity_id=None,
            start=datetime.combine(day.date, time(8, 30)),
            end=datetime.combine(day.date, time(9, 30)),
            cost=None,
        ),
    )
    return state


def ended_solo_state(profile_id: str | None = "user_solo") -> TripState:
    state = solo_state(profile_id)
    today = date.today()
    span = state.brief.dates.end - state.brief.dates.start
    delta_days = (today - timedelta(days=5)) - state.brief.dates.end
    state.brief.dates.start = state.brief.dates.start + delta_days
    state.brief.dates.end = today - timedelta(days=5)
    assert state.brief.dates.end - state.brief.dates.start == span
    for day in state.itinerary.days:
        day.date = day.date + delta_days
        for item in day.items:
            if item.start_at:
                item.start_at = datetime.combine(day.date, item.start_at.time())
            if item.end_at:
                item.end_at = datetime.combine(day.date, item.end_at.time())
    return state


def move_signal(trip: str, to: str = "11:00") -> LearningSignal:
    return LearningSignal(
        profile_id="user_solo",
        trip_id=trip,
        preference_key="avoid_early_mornings",
        strength="weak",
        source="behavior_move",
        context={"item": "Fish market at dawn", "from": "08:30", "to": to,
                 "trip_title": trip},
    )


class FakeProfiles:
    """Duck-typed ProfileRepository over a dict, in the M7 mould."""

    def __init__(self, profiles: dict[str, TravelerProfile]) -> None:
        self._profiles = profiles

    async def get(self, profile_id: str) -> TravelerProfile:
        from app.db.repository import ProfileNotFound

        if profile_id not in self._profiles:
            raise ProfileNotFound(profile_id)
        return self._profiles[profile_id]


class FakeLearning:
    """Duck-typed LearningRepository over a list. Records immediately."""

    def __init__(self, signals: list[LearningSignal] | None = None) -> None:
        self.signals = list(signals or [])

    async def record(self, signal: LearningSignal) -> LearningSignal:
        self.signals.append(signal)
        return signal

    async def record_many(self, signals: list[LearningSignal]) -> list[LearningSignal]:
        self.signals.extend(signals)
        return signals

    async def list_for_profile(self, profile_id: str, limit: int = 500):
        return [s for s in self.signals if s.profile_id == profile_id][:limit]


def tool_context(state: TripState, *, profiles=None, learning=None):
    from app.agent.tool_registry import ToolContext
    from app.services.proposal_store import ProposalStore

    return ToolContext(
        state=state,
        toolbox=None,
        proposals=ProposalStore(),
        settings=SETTINGS,
        profiles=profiles or FakeProfiles({}),
        learning=learning if learning is not None else FakeLearning(),
    )


async def stored_profile(session, profile_id="user_solo") -> TravelerProfile:
    return await ProfileRepository(session).create(
        TravelerProfile(profile_id=profile_id, name="Haoyu")
    )


# --- 1. one click is not a personality ---------------------------------------


async def test_moving_one_early_activity_later_never_touches_the_profile(client, session):
    await stored_profile(session)
    trip = await TripRepository(session).create(solo_state())

    response = await client.post(
        f"/trips/{trip.trip_id}/items/item_sunrise/move", json={"to_time": "11:00"}
    )
    assert response.status_code == 200 and response.json()["applied"]

    profile = await ProfileRepository(session).get("user_solo")
    assert profile.revision == 0
    assert profile.pace_preferences.preferred_start_time == "09:00"
    assert profile.learned == {}

    view = (await client.get("/profiles/user_solo/learning")).json()
    statuses = {h["status"] for h in view["hypotheses"]}
    assert statuses <= {"emerging"}  # visible as a suspicion, proposed as nothing


# --- 2. repetition across trips becomes a hypothesis -------------------------


def test_the_same_late_start_pattern_across_trips_becomes_a_hypothesis():
    profile = TravelerProfile(profile_id="user_solo", name="Haoyu")
    signals = [move_signal("trip_maui"), move_signal("trip_maui"), move_signal("trip_tokyo")]

    hypotheses = derive_hypotheses(profile, signals, settings=SETTINGS)

    assert len(hypotheses) == 1
    hypothesis = hypotheses[0]
    assert hypothesis.preference_key == "avoid_early_mornings"
    assert hypothesis.status == "proposable"
    # The split is the point: intensity and evidence reported apart.
    assert hypothesis.strength == "weak"
    assert hypothesis.confidence == "likely"
    assert set(hypothesis.trip_ids) == {"trip_maui", "trip_tokyo"}


# --- 3. the agent proposes, never applies ------------------------------------


async def test_the_agent_proposes_and_never_applies(session):
    from app.agent.tool_registry import _record_stated_preference

    await stored_profile(session)
    state = solo_state()
    context = tool_context(
        state,
        profiles=ProfileRepository(session),
        learning=LearningRepository(session),
    )

    first = await _record_stated_preference(
        context,
        {"traveler_id": "trv_solo", "preference_key": "dislikes_queueing",
         "quote": "我讨厌排队"},
    )
    assert first["recorded"]["quote"] == "我讨厌排队"
    assert "only the traveller answers" in first["note"]

    # However many times it is said, the tool records and the profile stands.
    for _ in range(4):
        await _record_stated_preference(
            context,
            {"traveler_id": "trv_solo", "preference_key": "dislikes_queueing",
             "quote": "我讨厌排队"},
        )

    profile = await ProfileRepository(session).get("user_solo")
    assert profile.revision == 0
    assert profile.food_preferences.queue_tolerance == 0.5
    assert profile.learned == {}


# --- 4 & 5. rejection is durable; acceptance carries provenance --------------


async def seeded(client, session):
    await stored_profile(session)
    repo = LearningRepository(session)
    for trip in ("trip_maui", "trip_maui", "trip_tokyo"):
        await repo.record(move_signal(trip))
    view = (await client.get("/profiles/user_solo/learning")).json()
    return view["hypotheses"][0], view["profile_revision"]


async def test_rejecting_the_proposal_leaves_the_profile_unchanged(client, session):
    hypothesis, revision = await seeded(client, session)

    response = await client.post(
        f"/profiles/user_solo/learning/{hypothesis['hypothesis_id']}/dismiss",
        json={"expected_revision": revision, "reason": "I like my mornings"},
    )
    assert response.status_code == 200

    profile = await ProfileRepository(session).get("user_solo")
    assert profile.pace_preferences.preferred_start_time == "09:00"
    assert profile.learned == {}
    assert [r.target_id for r in profile.learning_rejections] == [
        hypothesis["hypothesis_id"]
    ]
    assert profile.learning_rejections[0].scope == "profile"

    # More evidence arrives; the answer stands.
    await LearningRepository(session).record(move_signal("trip_kyoto"))
    view = (await client.get("/profiles/user_solo/learning")).json()
    assert view["hypotheses"][0]["status"] == "dismissed"

    refused = await client.post(
        f"/profiles/user_solo/learning/{hypothesis['hypothesis_id']}/accept",
        json={"expected_revision": profile.revision},
    )
    assert refused.status_code == 409


async def test_accepting_updates_the_long_term_profile_with_provenance(client, session):
    hypothesis, revision = await seeded(client, session)

    response = await client.post(
        f"/profiles/user_solo/learning/{hypothesis['hypothesis_id']}/accept",
        json={"expected_revision": revision},
    )
    assert response.status_code == 200
    profile = response.json()["profile"]

    assert profile["pace_preferences"]["preferred_start_time"] == "11:00"
    assert profile["revision"] == revision + 1
    provenance = profile["learned"]["pace_preferences.preferred_start_time"]
    assert provenance["hypothesis_id"] == hypothesis["hypothesis_id"]
    assert len(provenance["signal_ids"]) == 3
    assert set(provenance["trip_ids"]) == {"trip_maui", "trip_tokyo"}
    assert provenance["previous_value"] == "09:00"
    # Siblings survived the deep merge.
    assert profile["pace_preferences"]["max_daily_walking_km"] == 12.0
    assert profile["pace_preferences"]["intensity"] == "balanced"


# --- 6. the current trip's snapshot is untouched -----------------------------


async def test_the_current_trips_snapshot_survives_acceptance(client, session):
    hypothesis, revision = await seeded(client, session)

    state = solo_state()
    profile_before = await ProfileRepository(session).get("user_solo")
    state.travelers[0].preferences = resolve(state.travelers[0], profile_before)
    trip = await TripRepository(session).create(state)
    snapshot_before = trip.travelers[0].preferences.model_dump(mode="json")

    accepted = await client.post(
        f"/profiles/user_solo/learning/{hypothesis['hypothesis_id']}/accept",
        json={"expected_revision": revision},
    )
    assert accepted.status_code == 200

    reread = await TripRepository(session).get(trip.trip_id)
    snapshot_after = reread.travelers[0].preferences.model_dump(mode="json")
    assert snapshot_after == snapshot_before  # byte-identical
    assert snapshot_after["source_profile_revision"] == 0  # still names the old version
    assert snapshot_after["pace"]["preferred_start_time"] == "09:00"


# --- 7. a future trip uses the learned preference ----------------------------


async def test_a_future_trip_starts_later_because_of_what_was_learned(client, session):
    from app.services.itinerary_service import build_itinerary

    hypothesis, revision = await seeded(client, session)
    accepted = await client.post(
        f"/profiles/user_solo/learning/{hypothesis['hypothesis_id']}/accept",
        json={"expected_revision": revision},
    )
    assert accepted.status_code == 200

    updated = await ProfileRepository(session).get("user_solo")

    def first_start(proposal):
        return min(
            datetime.fromisoformat(item.start_at).time()
            for day in proposal.days
            for item in day.items
            if item.start_at
        )

    def plannable(state):
        # sample_state labels both entities "cafe", which fills only the
        # afternoon cafe slot; a museum that is actually a museum takes the
        # morning activity slot the shift is about.
        state.entities["ent_museum"].categories = ["museum"]
        return state

    control = plannable(sample_state())  # nobody resolved: neutral defaults
    control_first = first_start(build_itinerary(control))

    future = plannable(sample_state())
    future.travelers = [
        TripTraveler(
            traveler_id="trv_solo", profile_id="user_solo", name="Haoyu", role="organizer"
        )
    ]
    future.travelers[0].preferences = resolve(future.travelers[0], updated)
    future_first = first_start(build_itinerary(future))

    assert control_first == time(10, 0)  # the balanced template as authored
    assert future_first == time(11, 0)  # the learned start, consumed


# --- 8. Why? comes from persisted evidence only ------------------------------


async def test_why_traces_a_learned_preference_to_persisted_evidence_only(client, session):
    hypothesis, _revision = await seeded(client, session)

    stored = {
        s.signal_id: s for s in await LearningRepository(session).list_for_profile("user_solo")
    }
    assert hypothesis["evidence"], "a proposable hypothesis must carry its evidence"
    for line in hypothesis["evidence"]:
        signal = stored[line["signal_id"]]  # KeyError = invented evidence
        assert signal.context["from"] in line["line"]  # quoted from stored context
        assert signal.context["to"] in line["line"]
    assert set(hypothesis["signal_ids"]) <= set(stored)


# --- 9. signals are never assigned to the wrong traveler ---------------------


async def test_signals_are_never_assigned_to_the_wrong_traveler(client, session):
    from app.agent.tool_registry import _record_stated_preference

    await stored_profile(session)
    await ProfileRepository(session).create(
        TravelerProfile(profile_id="user_alice", name="Alice")
    )
    learning = LearningRepository(session)

    # A group trip's anonymous click records nothing.
    group = solo_state()
    group.travelers = [
        TripTraveler(traveler_id="trv_a", profile_id="user_solo", name="Haoyu",
                     role="organizer"),
        TripTraveler(traveler_id="trv_b", profile_id="user_alice", name="Alice"),
    ]
    stored = await TripRepository(session).create(group)
    response = await client.post(
        f"/trips/{stored.trip_id}/items/item_sunrise/move", json={"to_time": "11:00"}
    )
    assert response.status_code == 200 and response.json()["applied"]
    assert await learning.list_for_profile("user_solo") == []
    assert await learning.list_for_profile("user_alice") == []

    # The tool refuses an unknown traveler outright.
    context = tool_context(stored, profiles=ProfileRepository(session), learning=learning)
    unknown = await _record_stated_preference(
        context,
        {"traveler_id": "trv_nobody", "preference_key": "dislikes_queueing", "quote": "…"},
    )
    assert "error" in unknown
    assert await learning.list_for_profile("user_solo") == []

    # A named traveler without a profile is refused politely, recorded nowhere.
    no_profile = solo_state(profile_id=None)
    context = tool_context(no_profile, profiles=ProfileRepository(session), learning=learning)
    declined = await _record_stated_preference(
        context,
        {"traveler_id": "trv_solo", "preference_key": "dislikes_queueing", "quote": "…"},
    )
    assert declined["recorded"] is False
    assert await learning.list_for_profile("user_solo") == []

    # Reflection signals land under whoever answered - and only them.
    ended = ended_solo_state()
    ended.travelers = group.travelers
    stored = await TripRepository(session).create(ended)
    first_day = stored.itinerary.days[0].date.isoformat()
    response = await client.post(
        f"/trips/{stored.trip_id}/reflection",
        json={"answered_by": "trv_b", "days_too_busy": [first_day]},
    )
    assert response.status_code == 200
    assert await learning.list_for_profile("user_solo") == []
    alice = await learning.list_for_profile("user_alice")
    assert len(alice) == 1 and alice[0].profile_id == "user_alice"


# --- the machinery is wired, and the prompt carries the rule -----------------


async def test_both_learning_tools_are_registered():
    from app.agent.tool_registry import HANDLERS, TOOL_SCHEMAS

    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert {"record_stated_preference", "review_learned_preferences"} <= names
    assert {"record_stated_preference", "review_learned_preferences"} <= set(HANDLERS)

    record = next(s for s in TOOL_SCHEMAS if s["name"] == "record_stated_preference")
    assert "never guess" in record["description"]
    review = next(s for s in TOOL_SCHEMAS if s["name"] == "review_learned_preferences")
    assert "only the traveller can" in review["description"]


def test_the_prompt_forbids_silent_profile_writes():
    from app.agent.prompts import SYSTEM_INSTRUCTIONS

    assert "you must never write them into anyone's profile" in SYSTEM_INSTRUCTIONS
    assert "On a group trip, attribute or abstain" in SYSTEM_INSTRUCTIONS
    assert "A declined pattern stays declined" in SYSTEM_INSTRUCTIONS


async def test_the_review_tool_reports_and_never_applies(session):
    from app.agent.tool_registry import _review_learned_preferences

    await stored_profile(session)
    learning = LearningRepository(session)
    for trip in ("trip_maui", "trip_maui", "trip_tokyo"):
        await learning.record(move_signal(trip))

    state = solo_state()
    context = tool_context(state, profiles=ProfileRepository(session), learning=learning)
    report = await _review_learned_preferences(context, {})

    assert report["travelers"][0]["patterns"][0]["status"] == "proposable"
    assert "only the traveller answers" in report["note"]

    profile = await ProfileRepository(session).get("user_solo")
    assert profile.revision == 0 and profile.learned == {}
