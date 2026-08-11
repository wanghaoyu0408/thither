"""The decisions workspace, and the gates it must go through like everything else.

A decision changed from a button reaches the same patch engine as one changed by
the agent. What these pin is that it does not get a shortcut on the way: a
locked decision refuses, a rejected option does not come back on its own, and
`booked` is recorded as a fact rather than smuggled in as a status.
"""

from app.db.repository import TripRepository
from app.models.decision import Decision, DecisionOption, HotelAreaOption
from app.models.lock import LockRecord
from tests.conftest import sample_state


def state_with_areas():
    """Two neighbourhoods, ranked and not yet chosen."""
    state = sample_state()
    state.decisions.hotel_area = Decision[HotelAreaOption](
        decision_id="dec_area",
        status="shortlisted",
        options=[
            DecisionOption[HotelAreaOption](
                option_id="opt_ginza",
                data=HotelAreaOption(area_name="Ginza", mean_minutes=14.0, travel_mode="transit"),
                status="shortlisted",
                pros=["shortest average travel time to planned areas"],
            ),
            DecisionOption[HotelAreaOption](
                option_id="opt_asakusa",
                data=HotelAreaOption(area_name="Asakusa", mean_minutes=27.0, travel_mode="transit"),
                status="shortlisted",
            ),
        ],
    )
    return state


async def stored(session, state=None):
    return await TripRepository(session).create(state or state_with_areas())


# --- reading -----------------------------------------------------------------


async def test_the_projection_lists_what_the_trip_has_to_decide(client, session):
    trip = await stored(session)

    body = (await client.get(f"/trips/{trip.trip_id}/decisions")).json()

    by_name = {view["name"]: view for view in body}
    assert by_name["hotel_area"]["exists"] is True
    assert [option["label"] for option in by_name["hotel_area"]["options"]] == ["Ginza", "Asakusa"]
    # A decision this trip has not started is still listed - that it is
    # outstanding is information the workspace has to carry.
    assert by_name["hotel"]["exists"] is False


async def test_a_fixed_destination_is_hidden_with_its_reason(client, session):
    state = state_with_areas()
    state.brief.destination.city = "Tokyo"
    state.brief.destination.flexible = False
    trip = await stored(session, state)

    body = (await client.get(f"/trips/{trip.trip_id}/decisions")).json()
    destination = next(view for view in body if view["name"] == "destination")

    # Shown anyway, because it has stored options - hiding real work loses it -
    # but the projection says plainly that it is settled.
    assert destination["relevant"] is False
    assert "Tokyo is fixed" in destination["hidden_reason"]


async def test_lock_state_comes_from_the_locks_not_the_status_enum(client, session):
    """`DecisionStatus` has a "locked" value that nothing has ever set."""
    state = state_with_areas()
    state.locks.append(
        LockRecord(target_kind="decision", target_id="dec_area", reason="settled with the group")
    )
    trip = await stored(session, state)

    body = (await client.get(f"/trips/{trip.trip_id}/decisions")).json()
    area = next(view for view in body if view["name"] == "hotel_area")

    assert area["locked"] is True
    assert area["status"] == "shortlisted", "status is workflow state, not lock state"


# --- selecting ---------------------------------------------------------------


async def test_selecting_an_option_commits_and_reports_from_the_reloaded_row(client, session):
    trip = await stored(session)

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_ginza"}
        )
    ).json()

    assert result["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.revision == result["revision"]
    assert persisted.decisions.hotel_area.selected_option_id == "opt_ginza"
    assert persisted.decisions.hotel_area.status == "selected"


async def test_the_overview_says_what_a_choice_has_unblocked(client, session):
    """Choosing used to change the trip and tell nobody what it made possible.

    The browser reads this after every action; it is what turns the last card
    into "now find a hotel" instead of a screen that goes quiet.
    """
    trip = await stored(session)

    before = (await client.get(f"/trips/{trip.trip_id}/overview")).json()["next_steps"]
    assert ("hotel", "research") not in [(s["what"], s["kind"]) for s in before]
    assert ("hotel_area", "choose") in [(s["what"], s["kind"]) for s in before]

    await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_ginza"}
    )

    after = (await client.get(f"/trips/{trip.trip_id}/overview")).json()["next_steps"]
    assert ("hotel", "research") in [(s["what"], s["kind"]) for s in after]
    assert ("hotel_area", "choose") not in [(s["what"], s["kind"]) for s in after]


async def test_selecting_again_changes_nothing_and_spends_no_revision(client, session):
    trip = await stored(session)
    first = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_ginza"}
        )
    ).json()

    second = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_ginza"}
        )
    ).json()

    assert second["applied"] is False
    assert second["revision"] == first["revision"]


async def test_a_locked_decision_refuses_a_new_choice(client, session):
    state = state_with_areas()
    state.locks.append(
        LockRecord(target_kind="decision", target_id="dec_area", reason="already paid a deposit")
    )
    trip = await stored(session, state)
    before = (await TripRepository(session).get(trip.trip_id)).revision

    response = await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_asakusa"}
    )

    assert response.status_code == 409
    assert "already paid a deposit" in response.json()["detail"]
    assert (await TripRepository(session).get(trip.trip_id)).revision == before


# --- locking -----------------------------------------------------------------


async def test_a_decision_can_be_locked_and_released_by_naming_the_lock(client, session):
    trip = await stored(session)

    locked = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/lock", json={"reason": "group agreed"}
        )
    ).json()
    assert locked["applied"] is True

    unlocked = (await client.post(f"/trips/{trip.trip_id}/decisions/hotel_area/unlock")).json()

    assert unlocked["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert not [lock for lock in persisted.locks if lock.target_kind == "decision"]


# --- booked ------------------------------------------------------------------


async def test_booked_is_recorded_as_a_fact_beside_the_reasoning(client, session):
    trip = await stored(session)

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/booked",
            json={"booked": True, "reference": "CONF-4471"},
        )
    ).json()

    assert result["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    area = persisted.decisions.hotel_area
    assert area.booked is True
    assert area.booked_reference == "CONF-4471"
    assert area.booked_at is not None
    # Booking does not erase why: "how did we end up here?" is asked afterwards.
    assert len(area.options) == 2
    assert area.options[0].pros


async def test_booked_is_reversible(client, session):
    trip = await stored(session)
    await client.post(f"/trips/{trip.trip_id}/decisions/hotel_area/booked", json={"booked": True})

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/booked", json={"booked": False}
        )
    ).json()

    assert result["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.decisions.hotel_area.booked is False
    assert persisted.decisions.hotel_area.booked_at is None


# --- rejecting ---------------------------------------------------------------


async def test_rejecting_an_option_records_it_and_demotes_it_in_one_patch(client, session):
    trip = await stored(session)

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/options/opt_asakusa/reject",
            json={"reason": "too far from the station"},
        )
    ).json()

    assert result["applied"] is True, result["errors"]
    persisted = await TripRepository(session).get(trip.trip_id)
    assert [record.target_id for record in persisted.rejections] == ["opt_asakusa"]
    options = persisted.decisions.hotel_area.options
    rejected = next(o for o in options if o.option_id == "opt_asakusa")
    assert rejected.status == "rejected"


async def test_a_rejected_option_is_kept_out_of_the_ranked_list(client, session):
    trip = await stored(session)
    await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel_area/options/opt_asakusa/reject", json={}
    )

    body = (await client.get(f"/trips/{trip.trip_id}/decisions")).json()
    area = next(view for view in body if view["name"] == "hotel_area")

    assert [option["label"] for option in area["options"]] == ["Ginza"]
    assert [option["label"] for option in area["rejected"]] == ["Asakusa"]


async def test_a_rejected_option_cannot_be_selected_without_reconsidering_it(client, session):
    trip = await stored(session)
    await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel_area/options/opt_asakusa/reject", json={}
    )

    refused = await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_asakusa"}
    )
    assert refused.status_code == 409

    back = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/options/opt_asakusa/reconsider"
        )
    ).json()
    assert back["applied"] is True

    chosen = await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_asakusa"}
    )
    assert chosen.json()["applied"] is True


async def test_rejecting_the_selected_option_clears_the_selection(client, session):
    trip = await stored(session)
    await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel_area/select", json={"option_id": "opt_ginza"}
    )

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/decisions/hotel_area/options/opt_ginza/reject", json={}
        )
    ).json()

    assert result["applied"] is True, result["errors"]
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.decisions.hotel_area.selected_option_id is None
    assert persisted.decisions.hotel_area.status == "shortlisted"


async def test_an_unknown_decision_is_a_404(client, session):
    trip = await stored(session)

    response = await client.post(
        f"/trips/{trip.trip_id}/decisions/hotel/select", json={"option_id": "opt_x"}
    )

    assert response.status_code == 404
