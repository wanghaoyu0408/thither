"""The learning layer's HTTP surface: emission, reflection, consent.

The consent invariant, exercised end to end: nothing but an explicit accept
writes learning into a profile; a dismissal is durable; the reflection is
answered once, after the trip is over, by a named traveller.
"""

from datetime import date, datetime, time, timedelta

from app.db.repository import (
    CalibrationRepository,
    LearningRepository,
    ProfileRepository,
    ProfileRevisionConflict,
    TripRepository,
)
from app.models.learning import LearningSignal
from app.models.traveler import TravelerProfile
from app.models.trip import TripTraveler
from tests.conftest import make_item, sample_state

import pytest


def solo_state(profile_id: str = "user_solo"):
    state = sample_state()
    state.travelers = [
        TripTraveler(
            traveler_id="trv_solo", profile_id=profile_id, name="Haoyu", role="organizer"
        )
    ]
    # An early item, because sample_state schedules nothing before 14:00 and
    # the move detector only reads starts before 10:00 as mornings.
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


def signal(profile_id="user_solo", trip="trip_x", key="avoid_early_mornings"):
    return LearningSignal(
        profile_id=profile_id,
        trip_id=trip,
        preference_key=key,
        strength="weak",
        source="behavior_move",
        context={"item": "Market", "from": "08:30", "to": "11:00", "trip_title": trip},
    )


async def make_profile(session, profile_id="user_solo") -> TravelerProfile:
    return await ProfileRepository(session).create(
        TravelerProfile(profile_id=profile_id, name="Haoyu")
    )


# --- repository basics -------------------------------------------------------


async def test_signals_round_trip_the_repository_in_order(session):
    repo = LearningRepository(session)
    first = signal(trip="trip_a")
    second = signal(trip="trip_b")
    await repo.record(first)
    await repo.record(second)

    stored = await repo.list_for_profile("user_solo")
    assert [s.signal_id for s in stored] == [first.signal_id, second.signal_id]
    assert stored[0].context["from"] == "08:30"


async def test_a_profile_update_with_a_stale_revision_is_refused_and_writes_nothing(session):
    await make_profile(session)
    repo = ProfileRepository(session)

    with pytest.raises(ProfileRevisionConflict):
        await repo.update("user_solo", {"notes": "stale"}, expected_revision=7)

    fresh = await repo.get("user_solo")
    assert fresh.notes is None and fresh.revision == 0


# --- behavioral emission -----------------------------------------------------


async def test_a_move_on_a_solo_trip_records_a_signal_and_a_group_trip_records_none(
    client, session
):
    await make_profile(session)

    solo = await TripRepository(session).create(solo_state())
    response = await client.post(
        f"/trips/{solo.trip_id}/items/item_sunrise/move", json={"to_time": "11:30"}
    )
    assert response.status_code == 200 and response.json()["applied"]

    recorded = await LearningRepository(session).list_for_profile("user_solo")
    assert len(recorded) == 1
    assert recorded[0].preference_key == "avoid_early_mornings"
    assert recorded[0].trip_id == solo.trip_id

    # The identical move on a group trip: same early item, two travelers.
    group_state = solo_state()
    group_state.travelers = sample_state().travelers
    group = await TripRepository(session).create(group_state)
    response = await client.post(
        f"/trips/{group.trip_id}/items/item_sunrise/move", json={"to_time": "11:30"}
    )
    assert response.status_code == 200 and response.json()["applied"]

    after = await LearningRepository(session).list_for_profile("user_solo")
    assert len(after) == 1  # nothing new: nobody knows whose hand moved it


async def test_an_afternoon_move_records_nothing(client, session):
    await make_profile(session)
    stored = await TripRepository(session).create(solo_state())

    # The museum starts at 14:00; pushing it later says nothing about mornings.
    response = await client.post(
        f"/trips/{stored.trip_id}/items/item_museum/move", json={"to_time": "16:00"}
    )
    assert response.status_code == 200 and response.json()["applied"]

    assert await LearningRepository(session).list_for_profile("user_solo") == []


# --- reflection --------------------------------------------------------------


def ended_solo_state():
    state = solo_state()
    today = date.today()
    span = state.brief.dates.end - state.brief.dates.start
    state.brief.dates.start = today - timedelta(days=span.days + 5)
    state.brief.dates.end = today - timedelta(days=5)
    delta = state.brief.dates.start - date(2026, 10, 3)
    for day in state.itinerary.days:
        day.date = day.date + delta
        for item in day.items:
            if item.start_at:
                item.start_at = datetime.combine(day.date, item.start_at.time())
            if item.end_at:
                item.end_at = datetime.combine(day.date, item.end_at.time())
    return state


async def test_reflection_is_answered_once_and_only_after_the_trip_ends(client, session):
    await make_profile(session)

    ongoing = await TripRepository(session).create(solo_state())  # ends 2026-10-08
    response = await client.post(
        f"/trips/{ongoing.trip_id}/reflection",
        json={"answered_by": "trv_solo"},
    )
    assert response.status_code == 409
    assert "not over" in response.json()["detail"]

    ended = await TripRepository(session).create(ended_solo_state())
    first_day = ended.itinerary.days[0].date.isoformat()
    response = await client.post(
        f"/trips/{ended.trip_id}/reflection",
        json={"answered_by": "trv_solo", "days_too_busy": [first_day]},
    )
    assert response.status_code == 200 and response.json()["applied"]

    again = await client.post(
        f"/trips/{ended.trip_id}/reflection", json={"answered_by": "trv_solo"}
    )
    assert again.status_code == 409
    assert "already" in again.json()["detail"]


async def test_reflection_signals_go_to_the_answering_traveler_only(client, session):
    await ProfileRepository(session).create(
        TravelerProfile(profile_id="user_alice", name="Alice")
    )
    state = ended_solo_state()
    state.travelers = [
        TripTraveler(traveler_id="trv_a", profile_id=None, name="Haoyu", role="organizer"),
        TripTraveler(traveler_id="trv_b", profile_id="user_alice", name="Alice"),
    ]
    stored = await TripRepository(session).create(state)

    first_day = stored.itinerary.days[0].date.isoformat()
    response = await client.post(
        f"/trips/{stored.trip_id}/reflection",
        json={"answered_by": "trv_b", "days_too_busy": [first_day]},
    )
    assert response.status_code == 200

    alice = await LearningRepository(session).list_for_profile("user_alice")
    assert len(alice) == 1 and alice[0].preference_key == "relaxed_pace"


async def test_a_reflection_from_an_unknown_traveler_is_refused(client, session):
    stored = await TripRepository(session).create(ended_solo_state())
    response = await client.post(
        f"/trips/{stored.trip_id}/reflection", json={"answered_by": "trv_nobody"}
    )
    assert response.status_code == 422


# --- the consent surface -----------------------------------------------------


async def seeded_profile(session, count=3, trips=("trip_a", "trip_a", "trip_b")):
    await make_profile(session)
    repo = LearningRepository(session)
    for trip in trips[:count]:
        await repo.record(signal(trip=trip))


async def test_get_learning_reports_evidence_a_ui_can_render_verbatim(client, session):
    await seeded_profile(session)

    response = await client.get("/profiles/user_solo/learning")
    assert response.status_code == 200
    body = response.json()

    assert body["profile_revision"] == 0
    hypothesis = body["hypotheses"][0]
    assert hypothesis["status"] == "proposable"
    assert hypothesis["strength"] == "weak"
    assert hypothesis["confidence"] == "likely"
    assert all("08:30" in line["line"] for line in hypothesis["evidence"])


async def test_accept_requires_the_revision_it_read(client, session):
    await seeded_profile(session)
    body = (await client.get("/profiles/user_solo/learning")).json()
    hyp = body["hypotheses"][0]

    stale = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/accept",
        json={"expected_revision": 41},
    )
    assert stale.status_code == 409

    ok = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/accept",
        json={"expected_revision": body["profile_revision"]},
    )
    assert ok.status_code == 200
    change = ok.json()["change"]
    assert change["path"] == "pace_preferences.preferred_start_time"
    # The seeds moved things to 11:00, so 11:00 is what the evidence proposes.
    assert change["before"] == "09:00" and change["after"] == "11:00"


async def test_accept_deep_merges_and_spares_sibling_pace_fields(client, session):
    await seeded_profile(session)
    await ProfileRepository(session).update(
        "user_solo", {"pace_preferences": {"preferred_start_time": "09:00",
                                           "max_daily_walking_km": 6.0,
                                           "intensity": "balanced",
                                           "parking_sensitive": False}}
    )
    body = (await client.get("/profiles/user_solo/learning")).json()
    hyp = body["hypotheses"][0]

    ok = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/accept",
        json={"expected_revision": body["profile_revision"]},
    )
    assert ok.status_code == 200
    pace = ok.json()["profile"]["pace_preferences"]
    assert pace["preferred_start_time"] == "11:00"
    assert pace["max_daily_walking_km"] == 6.0  # the hand-set sibling survived
    assert ok.json()["profile"]["learned"]["pace_preferences.preferred_start_time"]


async def test_dismiss_then_accept_is_refused(client, session):
    await seeded_profile(session)
    body = (await client.get("/profiles/user_solo/learning")).json()
    hyp = body["hypotheses"][0]

    dismissed = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/dismiss",
        json={"expected_revision": body["profile_revision"], "reason": "not really"},
    )
    assert dismissed.status_code == 200

    profile = await ProfileRepository(session).get("user_solo")
    assert profile.pace_preferences.preferred_start_time == "09:00"  # untouched
    assert len(profile.learning_rejections) == 1

    body = (await client.get("/profiles/user_solo/learning")).json()
    assert body["hypotheses"][0]["status"] == "dismissed"

    refused = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/accept",
        json={"expected_revision": profile.revision},
    )
    assert refused.status_code == 409
    assert "that answer stands" in refused.json()["detail"]


async def test_accept_with_a_value_is_the_edit_path_and_records_it_as_edited(client, session):
    await seeded_profile(session)
    body = (await client.get("/profiles/user_solo/learning")).json()
    hyp = body["hypotheses"][0]

    ok = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/accept",
        json={"expected_revision": body["profile_revision"], "value": "11:00"},
    )
    assert ok.status_code == 200
    assert ok.json()["change"]["after"] == "11:00"
    assert ok.json()["provenance"]["value_source"] == "edited"


async def test_remove_reverts_and_keeps_it_from_returning(client, session):
    await seeded_profile(session)
    body = (await client.get("/profiles/user_solo/learning")).json()
    hyp = body["hypotheses"][0]

    accepted = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/accept",
        json={"expected_revision": body["profile_revision"]},
    )
    revision = accepted.json()["profile"]["revision"]

    removed = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/remove",
        json={"expected_revision": revision},
    )
    assert removed.status_code == 200
    profile = removed.json()["profile"]
    assert profile["pace_preferences"]["preferred_start_time"] == "09:00"  # reverted
    assert profile["learned"] == {}

    # And the untouched evidence cannot bring it back.
    body = (await client.get("/profiles/user_solo/learning")).json()
    assert body["hypotheses"][0]["status"] == "dismissed"


async def test_a_malformed_edited_value_is_a_422_not_a_stored_lie(client, session):
    await seeded_profile(session)
    body = (await client.get("/profiles/user_solo/learning")).json()
    hyp = body["hypotheses"][0]

    bad = await client.post(
        f"/profiles/user_solo/learning/{hyp['hypothesis_id']}/accept",
        json={"expected_revision": body["profile_revision"], "value": "25:99"},
    )
    assert bad.status_code == 422

    profile = await ProfileRepository(session).get("user_solo")
    assert profile.pace_preferences.preferred_start_time == "09:00"


# --- marking our own numbers --------------------------------------------------


def ended_state_with_a_chosen_area():
    """A trip that is over, with a neighbourhood chosen on a measured figure."""
    from app.models.decision import Decision, DecisionOption, HotelAreaOption

    state = ended_solo_state()
    state.brief.timezone = "Asia/Tokyo"
    state.decisions.hotel_area = Decision(
        options=[
            DecisionOption(
                option_id="opt_ginza",
                data=HotelAreaOption(area_name="Ginza", mean_minutes=14.0, travel_mode="transit"),
            ),
            DecisionOption(
                option_id="opt_asakusa",
                data=HotelAreaOption(
                    area_name="Asakusa", mean_minutes=27.0, travel_mode="transit"
                ),
            ),
        ],
        selected_option_id="opt_ginza",
    )
    return state


async def test_the_card_asks_about_the_figure_that_decided_something(client, session):
    await make_profile(session)
    stored = await TripRepository(session).create(ended_state_with_a_chosen_area())

    asked = (await client.get(f"/trips/{stored.trip_id}/overview")).json()["reflection"][
        "estimates"
    ]

    assert len(asked) == 1
    assert "14" in asked[0]["question"] and "Ginza" in asked[0]["question"]
    assert asked[0]["unit"] == "minutes"


async def test_marking_an_estimate_records_it_against_the_provider(client, session):
    await make_profile(session)
    stored = await TripRepository(session).create(ended_state_with_a_chosen_area())
    asked = (await client.get(f"/trips/{stored.trip_id}/overview")).json()["reflection"][
        "estimates"
    ]

    response = await client.post(
        f"/trips/{stored.trip_id}/reflection",
        json={
            "answered_by": "trv_solo",
            "estimates": [
                {"prediction_id": asked[0]["prediction_id"], "answer": "much_longer"}
            ],
        },
    )

    assert response.status_code == 200 and response.json()["applied"]
    outcomes = await CalibrationRepository(session).list_for(dimension="travel_minutes")
    assert len(outcomes) == 1
    assert outcomes[0].predicted == 14.0
    assert outcomes[0].actual_low > 14.0
    assert outcomes[0].mode == "transit"
    assert outcomes[0].scope == "Asia/Tokyo"


async def test_an_exact_figure_beats_the_chip(client, session):
    await make_profile(session)
    stored = await TripRepository(session).create(ended_state_with_a_chosen_area())
    asked = (await client.get(f"/trips/{stored.trip_id}/overview")).json()["reflection"][
        "estimates"
    ]

    await client.post(
        f"/trips/{stored.trip_id}/reflection",
        json={
            "answered_by": "trv_solo",
            "estimates": [
                {
                    "prediction_id": asked[0]["prediction_id"],
                    "answer": "about_right",
                    "exact": 23,
                }
            ],
        },
    )

    outcomes = await CalibrationRepository(session).list_for(dimension="travel_minutes")
    assert (outcomes[0].actual_low, outcomes[0].actual_high) == (23.0, 23.0)


async def test_an_id_that_names_nothing_on_this_trip_records_nothing(client, session):
    """The corpus is only ever fed by figures this trip actually made. A caller
    cannot post arbitrary numbers into it by inventing an id."""
    await make_profile(session)
    stored = await TripRepository(session).create(ended_state_with_a_chosen_area())

    response = await client.post(
        f"/trips/{stored.trip_id}/reflection",
        json={
            "answered_by": "trv_solo",
            "estimates": [{"prediction_id": "pred_madeup", "answer": "much_longer"}],
        },
    )

    assert response.status_code == 200
    assert await CalibrationRepository(session).list_for() == []


async def test_a_trip_with_nobody_profiled_still_marks_our_numbers(client, session):
    """What they said about a routing API is a fact about the API. It has
    nothing to do with whether anyone on the trip has a profile."""
    state = ended_state_with_a_chosen_area()
    state.travelers[0].profile_id = None
    stored = await TripRepository(session).create(state)
    asked = (await client.get(f"/trips/{stored.trip_id}/overview")).json()["reflection"][
        "estimates"
    ]

    result = (
        await client.post(
            f"/trips/{stored.trip_id}/reflection",
            json={
                "answered_by": "trv_solo",
                "estimates": [
                    {"prediction_id": asked[0]["prediction_id"], "answer": "a_bit_longer"}
                ],
            },
        )
    ).json()

    assert any("no stored profile" in w for w in result["warnings"])
    assert len(await CalibrationRepository(session).list_for()) == 1


# --- overview projection -----------------------------------------------------


async def test_the_overview_says_when_reflection_is_due_and_what_is_proposable(
    client, session
):
    await seeded_profile(session)
    stored = await TripRepository(session).create(ended_solo_state())

    body = (await client.get(f"/trips/{stored.trip_id}/overview")).json()
    assert body["reflection"]["due"] is True
    assert body["reflection"]["submitted"] is False
    # This fixture chose nothing, so there is no figure that argued for
    # anything and nothing worth asking about. The card is not a survey.
    assert body["reflection"]["estimates"] == []
    assert body["learning"][0]["proposable"] == 1
    assert body["learning"][0]["profile_id"] == "user_solo"

    ongoing = await TripRepository(session).create(solo_state())
    body = (await client.get(f"/trips/{ongoing.trip_id}/overview")).json()
    assert body["reflection"]["due"] is False
    assert body["reflection"]["estimates"] == [], "a trip still running asks nothing"
