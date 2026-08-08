import pytest
from sqlalchemy import select, update

from app.db.models import TripRow
from app.db.repository import (
    ProfileNotFound,
    ProfileRepository,
    TripNotFound,
    TripRepository,
)
from app.models import PatchOperation, TravelerProfile, TripPatch
from tests.conftest import sample_state


def patch(*operations: PatchOperation, base: int = 0, **kwargs) -> TripPatch:
    return TripPatch(base_revision=base, reason="test", operations=list(operations), **kwargs)


async def test_create_and_load_round_trip(session):
    repo = TripRepository(session)
    state = sample_state()

    await repo.create(state)
    loaded = await repo.get(state.trip_id)

    assert loaded == state


async def test_get_missing_trip_raises(session):
    with pytest.raises(TripNotFound):
        await TripRepository(session).get("trip_nope")


async def test_create_writes_a_trip_created_event(session):
    repo = TripRepository(session)
    state = await repo.create(sample_state())

    events = await repo.list_events(state.trip_id)

    assert [e["event_type"] for e in events] == ["trip_created"]
    assert events[0]["revision"] == 0


async def test_apply_patch_persists_and_bumps_revision(session):
    repo = TripRepository(session)
    state = await repo.create(sample_state())

    result = await repo.apply_patch(
        state.trip_id, patch(PatchOperation(op="set", path="/status", value="planning"))
    )

    assert result.applied is True
    reloaded = await repo.get(state.trip_id)
    assert reloaded.revision == 1
    assert reloaded.status == "planning"

    # The mirrored column must track the document, or the concurrency guard lies.
    column_revision = await session.scalar(
        select(TripRow.revision).where(TripRow.id == state.trip_id)
    )
    assert column_revision == 1


async def test_events_are_named_after_what_changed(session):
    repo = TripRepository(session)
    state = await repo.create(sample_state())

    await repo.apply_patch(
        state.trip_id,
        patch(
            PatchOperation(
                op="add",
                path="/constraints/-",
                value={
                    "id": "con_1",
                    "category": "food",
                    "description": "no shellfish",
                    "type": "hard",
                    "scope": "trip",
                    "source": "user_explicit",
                },
            )
        ),
    )

    events = await repo.list_events(state.trip_id)
    assert [e["event_type"] for e in events] == [
        "trip_created",
        "constraint_added",
        "patch_applied",
    ]
    assert events[-1]["payload"]["reason"] == "test"
    assert events[-1]["payload"]["operations"][0]["path"] == "/constraints/-"


async def test_stale_base_revision_leaves_the_database_untouched(session):
    repo = TripRepository(session)
    state = await repo.create(sample_state())
    await repo.apply_patch(
        state.trip_id, patch(PatchOperation(op="set", path="/status", value="planning"))
    )

    result = await repo.apply_patch(
        state.trip_id, patch(PatchOperation(op="set", path="/status", value="ready"), base=0)
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["REVISION_CONFLICT"]
    assert (await repo.get(state.trip_id)).status == "planning"


async def test_rejected_patch_writes_no_events(session):
    repo = TripRepository(session)
    state = await repo.create(sample_state())

    await repo.apply_patch(
        state.trip_id, patch(PatchOperation(op="set", path="/status", value="nonsense"))
    )

    events = await repo.list_events(state.trip_id)
    assert [e["event_type"] for e in events] == ["trip_created"]


async def test_conditional_update_guards_a_second_writer(session):
    """The concurrency guard lives in SQL, not only in the Python check.

    Two writers that both read revision 0 cannot both commit: the second
    UPDATE matches zero rows.
    """
    state = await TripRepository(session).create(sample_state())

    def bump(base_revision: int):
        return (
            update(TripRow)
            .where(TripRow.id == state.trip_id, TripRow.revision == base_revision)
            .values(revision=base_revision + 1)
        )

    first = await session.execute(bump(0))
    second = await session.execute(bump(0))

    assert first.rowcount == 1
    assert second.rowcount == 0


async def test_list_trips_returns_created_trips(session):
    repo = TripRepository(session)
    first = await repo.create(sample_state())
    second = await repo.create(sample_state())

    listed = await repo.list_trips()

    assert {t.trip_id for t in listed} == {first.trip_id, second.trip_id}


# --- profiles ----------------------------------------------------------------


async def test_profile_round_trip(session):
    repo = ProfileRepository(session)
    profile = TravelerProfile(profile_id="user_001", name="Haoyu", home_city="San Francisco")

    await repo.create(profile)

    assert (await repo.get("user_001")).name == "Haoyu"


async def test_profile_update_merges_fields(session):
    repo = ProfileRepository(session)
    await repo.create(TravelerProfile(profile_id="user_001", name="Haoyu"))

    updated = await repo.update("user_001", {"preferred_airports": ["SFO", "SJC", "OAK"]})

    assert updated.preferred_airports == ["SFO", "SJC", "OAK"]
    assert updated.name == "Haoyu"


async def test_profile_update_rejects_unknown_fields(session):
    repo = ProfileRepository(session)
    await repo.create(TravelerProfile(profile_id="user_001", name="Haoyu"))

    with pytest.raises(ValueError, match="unknown profile fields"):
        await repo.update("user_001", {"favourite_colour": "blue"})


async def test_missing_profile_raises(session):
    with pytest.raises(ProfileNotFound):
        await ProfileRepository(session).get("user_nope")
