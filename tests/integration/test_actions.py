"""The buttons, and the guarantees they must not quietly drop.

Every action here goes through the same patch engine the agent uses. What these
pin is that they do not take shortcuts on the way: nothing is reported as
changed until it has been committed and read back, a locked item refuses, and a
patch never claims a narrower scope than the change it actually makes.
"""

from datetime import date, datetime

import pytest

from app.db.repository import TripRepository
from app.models.entity import PlaceEntity
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.lock import LockRecord
from tests.conftest import DAY_ONE, sample_state

SECOND_DAY = date(2026, 10, 4)


def place(entity_id: str, name: str, *, categories=("restaurant",)) -> PlaceEntity:
    return PlaceEntity(
        entity_id=entity_id,
        name=name,
        categories=list(categories),
        lat=35.66,
        lng=139.70,
        rating=4.4,
        rating_count=900,
        provider_refs={"google_place_id": f"place_{entity_id}"},
    )


def item(item_id: str, title: str, hour: int, entity_id: str | None) -> ItineraryItem:
    return ItineraryItem(
        item_id=item_id,
        type="restaurant",
        entity_id=entity_id,
        title=title,
        start_at=datetime(2026, 10, 3, hour, 0),
        end_at=datetime(2026, 10, 3, hour + 1, 0),
    )


async def planned_trip(session, *, locks: list[str] | None = None, spares: int = 2):
    """A two-day trip with one scheduled meal and some unscheduled alternatives."""
    state = sample_state()
    state.entities = {"ent_a": place("ent_a", "First Choice")}
    for index in range(spares):
        state.entities[f"ent_spare{index}"] = place(f"ent_spare{index}", f"Spare {index}")

    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(date=DAY_ONE, items=[item("item_a", "First Choice", 19, "ent_a")]),
            ItineraryDay(date=SECOND_DAY, items=[]),
        ]
    )
    state.locks = [
        LockRecord(target_kind="itinerary_item", target_id=item_id, reason="booked")
        for item_id in (locks or [])
    ]
    return await TripRepository(session).create(state)


async def three_stop_trip(session):
    """Enough stops for two legs, which is what a drawn route needs."""
    state = sample_state()
    state.entities = {
        "ent_a": place("ent_a", "First"),
        "ent_b": place("ent_b", "Second"),
        "ent_c": place("ent_c", "Third"),
    }
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=DAY_ONE,
                items=[
                    item("item_a", "First", 10, "ent_a"),
                    item("item_b", "Second", 13, "ent_b"),
                    item("item_c", "Third", 16, "ent_c"),
                ],
            )
        ]
    )
    return await TripRepository(session).create(state)


# --- success is only ever reported after a reload ----------------------------


async def test_an_action_reports_the_revision_the_store_actually_holds(client, session):
    trip = await planned_trip(session)

    response = await client.post(f"/trips/{trip.trip_id}/items/item_a/lock", json={})
    body = response.json()

    persisted = await TripRepository(session).get(trip.trip_id)
    assert body["applied"] is True
    assert body["revision"] == persisted.revision == trip.revision + 1
    # The trip in the response is the persisted one, not an in-memory candidate.
    assert body["trip"]["revision"] == persisted.revision


async def test_a_rejected_action_changes_nothing_and_says_so(client, session):
    """A move onto a day that does not exist must leave the trip untouched."""
    trip = await planned_trip(session)

    response = await client.post(
        f"/trips/{trip.trip_id}/items/item_a/move", json={"to_date": "2026-12-25"}
    )

    assert response.status_code == 404
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.revision == trip.revision
    assert [i.item_id for _d, i in persisted.itinerary.iter_items()] == ["item_a"]


# --- locks hold --------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "items/item_a/move", {"to_time": "20:00"}),
        ("delete", "items/item_a", None),
        ("post", "items/item_a/replace", {}),
    ],
)
async def test_a_locked_item_refuses_and_names_the_lock(client, session, method, path, payload):
    trip = await planned_trip(session, locks=["item_a"])

    call = getattr(client, method)
    response = await call(
        f"/trips/{trip.trip_id}/{path}", **({"json": payload} if payload is not None else {})
    )

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.revision == trip.revision


async def test_unlocking_then_moving_works(client, session):
    trip = await planned_trip(session, locks=["item_a"])

    unlocked = await client.post(f"/trips/{trip.trip_id}/items/item_a/unlock")
    assert unlocked.json()["applied"] is True

    moved = await client.post(f"/trips/{trip.trip_id}/items/item_a/move", json={"to_time": "20:00"})
    assert moved.json()["applied"] is True

    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.itinerary.days[0].items[0].start_at.hour == 20
    assert persisted.locks == []


async def test_locking_twice_is_not_an_error_and_writes_nothing(client, session):
    trip = await planned_trip(session, locks=["item_a"])

    body = (await client.post(f"/trips/{trip.trip_id}/items/item_a/lock", json={})).json()

    assert body["applied"] is False
    assert body["summary"] == "already locked"
    assert body["revision"] == trip.revision


# --- scope is claimed honestly ------------------------------------------------


async def test_a_within_day_move_reports_only_that_day(client, session):
    trip = await planned_trip(session)

    body = (
        await client.post(f"/trips/{trip.trip_id}/items/item_a/move", json={"to_time": "17:30"})
    ).json()

    assert body["applied"] is True
    assert [d["day"] for d in body["diff"]] == [DAY_ONE.isoformat()]
    assert body["diff"][0]["moved"][0]["from_time"] == "19:00"
    assert body["diff"][0]["moved"][0]["at"] == "17:30"


async def test_a_cross_day_move_touches_two_days(client, session):
    """Two days change, so the patch cannot claim day scope - and does not."""
    trip = await planned_trip(session)

    body = (
        await client.post(
            f"/trips/{trip.trip_id}/items/item_a/move",
            json={"to_date": SECOND_DAY.isoformat(), "to_time": "12:00"},
        )
    ).json()

    assert body["applied"] is True
    days = {d["day"] for d in body["diff"]}
    assert days == {DAY_ONE.isoformat(), SECOND_DAY.isoformat()}

    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.itinerary.days[0].items == []
    assert persisted.itinerary.days[1].items[0].item_id == "item_a"


async def test_a_move_needs_somewhere_to_move_to(client, session):
    trip = await planned_trip(session)

    response = await client.post(f"/trips/{trip.trip_id}/items/item_a/move", json={})

    assert response.status_code == 422


# --- removing and replacing ---------------------------------------------------


async def test_removing_an_item_reports_it_as_removed(client, session):
    trip = await planned_trip(session)

    body = (await client.delete(f"/trips/{trip.trip_id}/items/item_a")).json()

    assert body["applied"] is True
    assert [c["title"] for c in body["diff"][0]["removed"]] == ["First Choice"]
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.itinerary.days[0].items == []


async def test_replace_swaps_in_an_unscheduled_candidate(client, session):
    trip = await planned_trip(session)

    body = (await client.post(f"/trips/{trip.trip_id}/items/item_a/replace", json={})).json()

    assert body["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    scheduled = persisted.itinerary.days[0].items
    assert len(scheduled) == 1
    assert scheduled[0].entity_id != "ent_a"
    assert scheduled[0].entity_id.startswith("ent_spare")
    # The slot is preserved - a replacement, not a reshuffle.
    assert scheduled[0].start_at.hour == 19


async def test_replace_with_nothing_to_swap_in_gives_a_reason_not_a_gap(client, session):
    trip = await planned_trip(session, spares=0)

    body = (await client.post(f"/trips/{trip.trip_id}/items/item_a/replace", json={})).json()

    assert body["applied"] is False
    assert "no alternative" in " ".join(body["warnings"])
    persisted = await TripRepository(session).get(trip.trip_id)
    # The original is still there. A hole would have been worse than a refusal.
    assert persisted.itinerary.days[0].items[0].entity_id == "ent_a"


async def test_replace_will_not_reuse_a_rejected_place(client, session):
    trip = await planned_trip(session, spares=1)

    await client.post(f"/trips/{trip.trip_id}/entities/ent_spare0/reject", json={})
    body = (await client.post(f"/trips/{trip.trip_id}/items/item_a/replace", json={})).json()

    assert body["applied"] is False
    assert "no alternative" in " ".join(body["warnings"])


# --- rejection ----------------------------------------------------------------


async def test_rejecting_a_place_records_it_and_takes_it_off_the_plan(client, session):
    trip = await planned_trip(session)

    body = (
        await client.post(
            f"/trips/{trip.trip_id}/entities/ent_a/reject", json={"reason": "too far"}
        )
    ).json()

    assert body["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert [r.target_id for r in persisted.rejections] == ["ent_a"]
    assert persisted.rejections[0].label == "First Choice"
    # Both halves: remembered, and gone from the day.
    assert persisted.itinerary.days[0].items == []


async def test_a_rejected_place_does_not_come_back_on_replan(client, session):
    trip = await planned_trip(session)
    await client.post(f"/trips/{trip.trip_id}/entities/ent_a/reject", json={})

    await client.post(f"/trips/{trip.trip_id}/days/{DAY_ONE}/replan", json={})

    persisted = await TripRepository(session).get(trip.trip_id)
    assert all(i.entity_id != "ent_a" for _d, i in persisted.itinerary.iter_items())


async def test_rejecting_a_locked_place_refuses(client, session):
    trip = await planned_trip(session, locks=["item_a"])

    response = await client.post(f"/trips/{trip.trip_id}/entities/ent_a/reject", json={})

    assert response.status_code == 409
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.rejections == []


# --- replanning ---------------------------------------------------------------


async def test_replanning_a_day_preserves_a_locked_item(client, session):
    trip = await planned_trip(session, locks=["item_a"])

    body = (
        await client.post(
            f"/trips/{trip.trip_id}/days/{DAY_ONE}/replan", json={"intensity": "relaxed"}
        )
    ).json()

    persisted = await TripRepository(session).get(trip.trip_id)
    assert [i.item_id for _d, i in persisted.itinerary.iter_items()] == ["item_a"]
    assert body["applied"] in (True, False)  # nothing to shed either way
    assert persisted.itinerary.days[0].items[0].start_at.hour == 19


async def test_a_replan_that_changes_nothing_does_not_spend_a_revision(client, session):
    """The number advancing with an empty diff is a change nobody made."""
    trip = await planned_trip(session)

    first = (await client.post(f"/trips/{trip.trip_id}/days/{DAY_ONE}/replan", json={})).json()
    second = (await client.post(f"/trips/{trip.trip_id}/days/{DAY_ONE}/replan", json={})).json()

    # Whatever the first one did, repeating it must be a no-op.
    assert second["applied"] is False
    assert second["summary"] == "nothing needed changing"
    assert second["diff"] == []
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.revision == first["revision"]


async def test_a_day_route_never_claims_a_road_it_did_not_fetch(client, session, monkeypatch):
    """A line between two pins is not a route. The map may draw one only when
    the server hands back geometry, and the two ways it can fail to - Google
    knows of no route, and the lookup itself failed - are reported apart."""
    from app.models.route import RouteLeg

    trip = await three_stop_trip(session)

    class Routes:
        async def path_between(self, origin, destination, *, mode):
            if origin.entity_id == "ent_a":
                return RouteLeg(
                    origin_index=0, destination_index=0, mode=mode,
                    duration_seconds=600, distance_meters=4200, polyline="abc123",
                )
            return None  # the lookup could not answer

    class FakeToolbox:
        routes = Routes()

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.api.actions.Toolbox", FakeToolbox)

    body = (await client.get(f"/trips/{trip.trip_id}/days/{DAY_ONE}/route")).json()

    assert body["geometry"] == "route"
    assert [leg["status"] for leg in body["legs"]] == ["ok", "lookup_failed"]
    assert "could not answer" in body["note"]
    assert "straight lines" in body["note"]


async def test_a_day_route_with_no_key_says_the_line_is_straight(client, session, monkeypatch):
    from app.services.toolbox import MissingCredentials

    trip = await three_stop_trip(session)

    def refuse(**kwargs):
        raise MissingCredentials("no key")

    monkeypatch.setattr("app.api.actions.Toolbox", refuse)

    body = (await client.get(f"/trips/{trip.trip_id}/days/{DAY_ONE}/route")).json()

    assert body["geometry"] == "straight_line"
    assert "not a route" in body["note"]
    assert body["legs"] == []


async def test_replanning_an_unknown_day_is_a_404(client, session):
    trip = await planned_trip(session)

    response = await client.post(f"/trips/{trip.trip_id}/days/2030-01-01/replan", json={})

    assert response.status_code == 404


# --- explanations --------------------------------------------------------------


async def test_why_reports_incomplete_when_nothing_was_recorded(client, session):
    trip = await planned_trip(session)

    body = (await client.get(f"/trips/{trip.trip_id}/items/item_a/why")).json()

    assert body["complete"] is False
    assert any("was not recorded" in gap for gap in body["missing"])
    # No invented rationale anywhere in the payload.
    assert body["pros"] == []
    assert body["cons"] == []


async def test_why_for_an_unknown_item_is_a_404(client, session):
    trip = await planned_trip(session)

    assert (await client.get(f"/trips/{trip.trip_id}/items/item_nope/why")).status_code == 404


async def test_every_action_carries_the_derived_views_the_ui_needs(client, session):
    trip = await planned_trip(session)

    body = (await client.post(f"/trips/{trip.trip_id}/items/item_a/lock", json={})).json()

    assert "status" in body["validation"]
    assert isinstance(body["conflicts"], list)
    assert isinstance(body["blocking"], list)
