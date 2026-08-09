"""Deleting a trip removes everything that hangs off it - and only it.

The cascade is written out rather than trusted to the schema, because SQLite
only honours `ON DELETE CASCADE` behind a pragma nobody sets. These tests pin
that: no orphaned messages or events, and the neighbour trip untouched.
"""

from sqlalchemy import func, select

from app.db.models import TripEventRow, TripMessageRow, TripRow
from app.db.repository import TripNotFound, TripRepository
from tests.conftest import sample_state


async def _counts(session, trip_id: str) -> tuple[int, int, int]:
    async def count(table, column):
        result = await session.execute(
            select(func.count()).select_from(table).where(column == trip_id)
        )
        return result.scalar_one()

    return (
        await count(TripRow, TripRow.id),
        await count(TripMessageRow, TripMessageRow.trip_id),
        await count(TripEventRow, TripEventRow.trip_id),
    )


async def test_delete_removes_the_trip_its_messages_and_its_events(session):
    repo = TripRepository(session)
    doomed = await repo.create(sample_state())
    kept = await repo.create(sample_state())
    for trip in (doomed, kept):
        session.add(
            TripMessageRow(
                trip_id=trip.trip_id, message_id=f"msg_{trip.trip_id}", role="user", content="hi"
            )
        )
    await session.commit()

    await repo.delete(doomed.trip_id)

    assert await _counts(session, doomed.trip_id) == (0, 0, 0)
    # The neighbour keeps its row, its message and its trip_created event.
    assert await _counts(session, kept.trip_id) == (1, 1, 1)


async def test_deleting_an_unknown_trip_raises_not_found(session):
    try:
        await TripRepository(session).delete("trip_never_existed")
        raise AssertionError("should have raised TripNotFound")
    except TripNotFound:
        pass


async def test_the_delete_endpoint_is_204_then_404(client, session):
    stored = await TripRepository(session).create(sample_state())

    assert (await client.delete(f"/trips/{stored.trip_id}")).status_code == 204
    assert (await client.get(f"/trips/{stored.trip_id}")).status_code == 404
    # Deleting again is a 404, not a silent success: the second caller should
    # learn the trip was already gone.
    assert (await client.delete(f"/trips/{stored.trip_id}")).status_code == 404


async def test_the_run_endpoints_answer_when_nothing_is_running(client):
    """"No turn right now" is an answer the poller needs, not an error."""
    assert (await client.get("/trips/trip_x/run")).json() == {"running": False}
    assert (await client.post("/trips/trip_x/run/cancel")).json() == {
        "running": False,
        "cancelling": False,
    }


async def test_the_run_endpoint_mirrors_a_registered_run(client):
    from app.agent import run_control

    control = run_control.begin("trip_live")
    try:
        control.begin_iteration(1)
        control.begin_tool("discover_restaurants")

        body = (await client.get("/trips/trip_live/run")).json()
        assert body["running"] is True
        assert body["current_tool"]["name"] == "discover_restaurants"

        cancel = (await client.post("/trips/trip_live/run/cancel")).json()
        assert cancel == {"running": True, "cancelling": True}
        assert control.cancelled is True
        assert (await client.get("/trips/trip_live/run")).json()["cancelled"] is True
    finally:
        run_control.finish("trip_live")
