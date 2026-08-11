"""Creating a trip the way the page creates one.

Every other test in this suite builds its world by hand - `TripRepository.create(
solo_state())` with a traveller and a profile already attached - and so none of
them could notice that the application itself never made either. Thirteen trips
in the live database, zero travellers, zero profiles, zero learning signals:
two whole milestones of per-person machinery that no user could reach.

These go through `POST /trips` with the payload the browser actually sends.
"""

from datetime import date, timedelta

from app.db.repository import ProfileRepository, TripRepository
from app.models.traveler import PacePreferences, TravelerProfile
from app.services.learning_service import behavioral_signal_allowed

START = date.today() + timedelta(days=60)


def payload(profile_id: str | None = None, name: str = "Haoyu") -> dict:
    """Exactly the shape `createTrip` posts."""
    body = {
        "title": "Kyoto",
        "brief": {
            "origin": {"city": "San Francisco"},
            "destination": {"city": "Kyoto", "flexible": False},
            "dates": {"start": START.isoformat(), "end": (START + timedelta(days=4)).isoformat()},
            "party": {"adults": 2, "rooms": 1},
        },
    }
    if profile_id:
        body["travelers"] = [
            {"name": name, "role": "organizer", "profile_id": profile_id}
        ]
    return body


async def a_profile(session, **fields) -> TravelerProfile:
    return await ProfileRepository(session).create(TravelerProfile(name="Haoyu", **fields))


async def test_a_trip_created_the_way_the_page_creates_one_has_a_traveller(client, session):
    profile = await a_profile(session)

    created = (await client.post("/trips", json=payload(profile.profile_id))).json()

    assert len(created["travelers"]) == 1
    traveller = created["travelers"][0]
    assert traveller["profile_id"] == profile.profile_id
    assert traveller["role"] == "organizer"


async def test_the_snapshot_is_resolved_at_creation(session, client):
    """Nothing else would resolve it.

    `review_group_preferences` returns immediately below two travellers, so a
    solo trip's `preferences` stayed None forever - and a preference learned
    last year would have reached this trip's day templates never.
    """
    profile = await a_profile(
        session, pace_preferences=PacePreferences(preferred_start_time="10:30")
    )

    created = (await client.post("/trips", json=payload(profile.profile_id))).json()

    snapshot = created["travelers"][0]["preferences"]
    assert snapshot is not None, "planning would otherwise run on neutral defaults"
    assert snapshot["pace"]["preferred_start_time"] == "10:30"
    # Stamped with where it came from, so "why is this trip planned that way?"
    # stays answerable after the profile moves on.
    assert snapshot["source_profile_id"] == profile.profile_id
    assert snapshot["source_profile_revision"] == profile.revision


async def test_that_traveller_can_carry_learning(client, session):
    """The reachability check. Every M9 surface is keyed to a profiled
    traveller, and with none they are all silently false."""
    profile = await a_profile(session)
    created = (await client.post("/trips", json=payload(profile.profile_id))).json()
    stored = await TripRepository(session).get(created["trip_id"])

    # Behaviour can now be attributed - this returned None for every trip that
    # had ever existed.
    assert behavioral_signal_allowed(stored) == profile.profile_id

    overview = (await client.get(f"/trips/{created['trip_id']}/overview")).json()
    assert overview["learning"], "the Travel DNA chip reads this and it was always empty"
    assert overview["learning"][0]["profile_id"] == profile.profile_id


async def test_a_trip_with_nobody_in_it_still_works(client, session):
    """Degrading, not crashing: the older trips have no travellers at all."""
    created = (await client.post("/trips", json=payload())).json()
    stored = await TripRepository(session).get(created["trip_id"])

    assert created["travelers"] == []
    assert behavioral_signal_allowed(stored) is None
    overview = (await client.get(f"/trips/{created['trip_id']}/overview")).json()
    assert overview["learning"] == []
    assert overview["reflection"]["due"] is False


async def test_a_deleted_profile_does_not_stop_a_trip_being_created(client, session):
    """The snapshot is a convenience, not a precondition."""
    created = (await client.post("/trips", json=payload("user_gone"))).json()

    assert created["travelers"][0]["profile_id"] == "user_gone"
    assert created["travelers"][0]["preferences"] is None
