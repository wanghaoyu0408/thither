"""Milestone 1 acceptance criteria, exercised over HTTP.

Each test maps to one criterion from the plan:
create trip, load trip, apply patch, reject invalid patch, respect locks,
revision increments correctly.
"""

from typing import Any

import pytest
from httpx import AsyncClient

TOKYO_BRIEF = {
    "destination": {"city": "Tokyo", "country": "Japan", "flexible": False},
    "dates": {"start": "2026-10-03", "end": "2026-10-08"},
    "party": {"adults": 4, "rooms": 2},
    "budget": {"total_per_person": 2500, "hotel_per_night": 250},
    "priorities": ["food", "city exploration"],
    "pace": "balanced",
}

CAFE = {
    "entity_type": "place",
    "entity_id": "ent_cafe",
    "name": "Fuglen Tokyo",
    "categories": ["cafe"],
    "lat": 35.6659,
    "lng": 139.6979,
    "rating": 4.3,
    "rating_count": 2300,
}

DINNER_ITEM = {
    "item_id": "item_dinner",
    "type": "restaurant",
    "entity_id": "ent_cafe",
    "title": "Dinner at Fuglen",
    "start_at": "2026-10-03T19:00:00",
    "end_at": "2026-10-03T21:00:00",
    "time_flexibility": "fixed",
}


async def create_trip(client: AsyncClient, **overrides) -> dict[str, Any]:
    payload = {"title": "Tokyo food trip", "created_by": "user_haoyu", "brief": TOKYO_BRIEF}
    payload.update(overrides)
    response = await client.post("/trips", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def send_patch(
    client: AsyncClient,
    trip_id: str,
    base: int,
    reason: str,
    operations: list[dict[str, Any]],
    **extra,
):
    return await client.post(
        f"/trips/{trip_id}/patch",
        json={"base_revision": base, "reason": reason, "operations": operations, **extra},
    )


async def build_day_one(client: AsyncClient, trip_id: str) -> int:
    """Seed a cafe entity and a day holding one fixed dinner. Returns the revision."""
    response = await send_patch(
        client,
        trip_id,
        0,
        "seed day one",
        [
            {"op": "add", "path": "/entities/ent_cafe", "value": CAFE},
            {
                "op": "add",
                "path": "/itinerary/days/-",
                "value": {"date": "2026-10-03", "theme": "Shibuya", "items": [DINNER_ITEM]},
            },
        ],
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


# --- Criterion 1: create trip ------------------------------------------------


async def test_create_trip(client: AsyncClient):
    state = await create_trip(client)

    assert state["revision"] == 0
    assert state["status"] == "draft"
    assert state["brief"]["destination"]["city"] == "Tokyo"
    assert state["metadata"]["title"] == "Tokyo food trip"

    listed = await client.get("/trips")
    assert listed.status_code == 200
    assert [t["trip_id"] for t in listed.json()] == [state["trip_id"]]


# --- Criterion 2: load trip --------------------------------------------------


async def test_load_trip_returns_what_was_created(client: AsyncClient):
    created = await create_trip(client)

    fetched = await client.get(f"/trips/{created['trip_id']}")

    assert fetched.status_code == 200
    assert fetched.json() == created

    debug = await client.get(f"/trips/{created['trip_id']}/state")
    assert debug.json() == created


async def test_unknown_trip_is_404(client: AsyncClient):
    assert (await client.get("/trips/trip_nope")).status_code == 404
    assert (await client.get("/trips/trip_nope/events")).status_code == 404

    missing = await send_patch(
        client, "trip_nope", 0, "x", [{"op": "set", "path": "/status", "value": "planning"}]
    )
    assert missing.status_code == 404


# --- Criterion 3: apply patch ------------------------------------------------


async def test_apply_patch_changes_state(client: AsyncClient):
    trip = await create_trip(client)
    trip_id = trip["trip_id"]

    response = await send_patch(
        client,
        trip_id,
        0,
        "record the party's hard constraint and seed day one",
        [
            {"op": "set", "path": "/status", "value": "planning"},
            {
                "op": "add",
                "path": "/constraints/-",
                "value": {
                    "id": "con_shellfish",
                    "category": "food",
                    "description": "Alice has a shellfish allergy",
                    "type": "hard",
                    "scope": "trip",
                    "source": "user_explicit",
                    "confirmed": True,
                },
            },
            {"op": "add", "path": "/entities/ent_cafe", "value": CAFE},
            {
                "op": "add",
                "path": "/itinerary/days/-",
                "value": {"date": "2026-10-03", "theme": "Shibuya", "items": [DINNER_ITEM]},
            },
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["revision"] == 1

    state = body["state"]
    assert state["status"] == "planning"
    assert state["constraints"][0]["id"] == "con_shellfish"
    assert state["itinerary"]["days"][0]["items"][0]["title"] == "Dinner at Fuglen"

    # The food constraint has no deterministic checker yet; that is reported,
    # not silently treated as satisfied.
    assert [w["constraint_id"] for w in body["warnings"]] == ["con_shellfish"]
    assert body["warnings"][0]["code"] == "CONSTRAINT_NOT_CHECKABLE"

    assert (await client.get(f"/trips/{trip_id}")).json() == state


# --- Criterion 4: reject invalid patch ---------------------------------------


async def test_stale_revision_is_409_and_changes_nothing(client: AsyncClient):
    trip = await create_trip(client)
    trip_id = trip["trip_id"]
    await send_patch(
        client, trip_id, 0, "first", [{"op": "set", "path": "/status", "value": "planning"}]
    )

    response = await send_patch(
        client, trip_id, 0, "stale", [{"op": "set", "path": "/status", "value": "ready"}]
    )

    assert response.status_code == 409
    assert [e["code"] for e in response.json()["errors"]] == ["REVISION_CONFLICT"]
    assert (await client.get(f"/trips/{trip_id}")).json()["status"] == "planning"


@pytest.mark.parametrize(
    ("label", "operations", "expected_code"),
    [
        (
            "type-invalid value",
            [{"op": "set", "path": "/status", "value": "teleporting"}],
            "SCHEMA_INVALID",
        ),
        (
            "dangling entity reference",
            [
                {
                    "op": "add",
                    "path": "/itinerary/days/-",
                    "value": {
                        "date": "2026-10-04",
                        "items": [
                            {
                                "item_id": "item_ghost",
                                "type": "restaurant",
                                "title": "Ghost",
                                "entity_id": "ent_missing",
                            }
                        ],
                    },
                }
            ],
            "INTEGRITY_ERROR",
        ),
        (
            "write to a server-managed field",
            [{"op": "set", "path": "/revision", "value": 99}],
            "PROTECTED_PATH",
        ),
        (
            "pointer that does not resolve",
            [{"op": "set", "path": "/nowhere/deep", "value": 1}],
            "INVALID_POINTER",
        ),
    ],
)
async def test_invalid_patches_are_422_and_leave_the_revision_alone(
    client: AsyncClient, label, operations, expected_code
):
    trip = await create_trip(client)
    trip_id = trip["trip_id"]
    revision = await build_day_one(client, trip_id)

    response = await send_patch(client, trip_id, revision, label, operations)

    assert response.status_code == 422, f"{label}: {response.text}"
    body = response.json()
    assert body["applied"] is False
    assert [e["code"] for e in body["errors"]] == [expected_code]
    assert body["state"] is None

    after = (await client.get(f"/trips/{trip_id}")).json()
    assert after["revision"] == revision


async def test_hard_constraint_violation_blocks_the_patch(client: AsyncClient):
    trip = await create_trip(client)
    trip_id = trip["trip_id"]
    revision = await build_day_one(client, trip_id)

    response = await send_patch(
        client,
        trip_id,
        revision,
        "add a budget ceiling the itinerary already blows",
        [
            {
                "op": "set",
                "path": "/itinerary/days/0/items/0/estimated_cost",
                "value": {"amount": 4000.0, "currency": "USD"},
            },
            {
                "op": "add",
                "path": "/constraints/-",
                "value": {
                    "id": "con_budget",
                    "category": "budget",
                    "description": "stay under $100 per person",
                    "type": "hard",
                    "scope": "trip",
                    "source": "user_explicit",
                    "params": {"max_total_per_person": 100},
                },
            },
        ],
    )

    assert response.status_code == 422
    body = response.json()
    assert [e["code"] for e in body["errors"]] == ["CONSTRAINT_VIOLATION"]
    assert body["errors"][0]["constraint_id"] == "con_budget"


# --- Criterion 5: respect locks ----------------------------------------------


async def test_locks_block_changes_until_explicitly_released(client: AsyncClient):
    trip = await create_trip(client)
    trip_id = trip["trip_id"]
    revision = await build_day_one(client, trip_id)

    locked = await send_patch(
        client,
        trip_id,
        revision,
        "dinner is booked",
        [
            {
                "op": "add",
                "path": "/locks/-",
                "value": {
                    "lock_id": "lock_dinner",
                    "target_kind": "itinerary_item",
                    "target_id": "item_dinner",
                    "reason": "reservation already made",
                    "locked_by": "user",
                },
            }
        ],
    )
    assert locked.status_code == 200
    revision = locked.json()["revision"]

    move_dinner = [
        {"op": "set", "path": "/itinerary/days/0/items/0/start_at", "value": "2026-10-03T17:00:00"}
    ]

    blocked = await send_patch(client, trip_id, revision, "move dinner earlier", move_dinner)

    assert blocked.status_code == 422
    body = blocked.json()
    assert [e["code"] for e in body["errors"]] == ["LOCK_VIOLATION"]
    assert body["errors"][0]["lock_id"] == "lock_dinner"
    assert "reservation already made" in body["errors"][0]["message"]

    state = (await client.get(f"/trips/{trip_id}")).json()
    assert state["itinerary"]["days"][0]["items"][0]["start_at"] == "2026-10-03T19:00:00"
    assert state["revision"] == revision

    released = await send_patch(
        client,
        trip_id,
        revision,
        "user agreed to move dinner",
        move_dinner,
        unlock_targets=["lock_dinner"],
    )

    assert released.status_code == 200, released.text
    released_state = released.json()["state"]
    assert released_state["itinerary"]["days"][0]["items"][0]["start_at"] == "2026-10-03T17:00:00"
    assert released_state["locks"] == []


async def test_rejected_places_do_not_come_back(client: AsyncClient):
    """Spec scenario C: 'I don't want Restaurant X' must stick."""
    trip = await create_trip(client)
    trip_id = trip["trip_id"]
    revision = await build_day_one(client, trip_id)

    rejected = await send_patch(
        client,
        trip_id,
        revision,
        "user does not want Ichiran",
        [
            {
                "op": "add",
                "path": "/rejections/-",
                "value": {
                    "rejection_id": "rej_ichiran",
                    "target_kind": "entity",
                    "target_id": "ent_ramen",
                    "label": "Ichiran Shibuya",
                    "reason": "too touristy",
                    "scope": "trip",
                },
            }
        ],
    )
    assert rejected.status_code == 200
    revision = rejected.json()["revision"]

    recommend_again = [
        {
            "op": "add",
            "path": "/entities/ent_ramen",
            "value": {**CAFE, "entity_id": "ent_ramen", "name": "Ichiran Shibuya"},
        }
    ]

    blocked = await send_patch(client, trip_id, revision, "suggest ramen", recommend_again)

    assert blocked.status_code == 422
    assert [e["code"] for e in blocked.json()["errors"]] == ["REJECTION_VIOLATION"]

    reconsidered = await send_patch(
        client,
        trip_id,
        revision,
        "user asked to reconsider it",
        recommend_again,
        allow_rejected=["ent_ramen"],
    )

    assert reconsidered.status_code == 200
    assert "ent_ramen" in reconsidered.json()["state"]["entities"]


# --- Criterion 6: revision increments correctly ------------------------------


async def test_revision_increments_once_per_applied_patch(client: AsyncClient):
    trip = await create_trip(client)
    trip_id = trip["trip_id"]

    themes = ["Shibuya", "Asakusa", "Ginza", "Daikanyama", "Nakameguro"]
    for index, theme in enumerate(themes):
        response = await send_patch(
            client,
            trip_id,
            index,
            f"add {theme}",
            [
                {
                    "op": "add",
                    "path": "/itinerary/days/-",
                    "value": {"date": f"2026-10-0{index + 3}", "theme": theme},
                }
            ],
        )
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == index + 1

    state = (await client.get(f"/trips/{trip_id}")).json()
    assert state["revision"] == len(themes)
    assert [day["theme"] for day in state["itinerary"]["days"]] == themes

    events = (await client.get(f"/trips/{trip_id}/events")).json()
    applied = [e for e in events if e["event_type"] == "patch_applied"]
    assert [e["revision"] for e in applied] == [1, 2, 3, 4, 5]
    assert events[0]["event_type"] == "trip_created"


async def test_two_patches_on_the_same_base_cannot_both_win(client: AsyncClient):
    trip = await create_trip(client)
    trip_id = trip["trip_id"]

    first = await send_patch(
        client, trip_id, 0, "first writer", [{"op": "set", "path": "/status", "value": "planning"}]
    )
    second = await send_patch(
        client,
        trip_id,
        0,
        "second writer",
        [{"op": "set", "path": "/brief/pace", "value": "relaxed"}],
    )

    assert first.status_code == 200
    assert second.status_code == 409

    state = (await client.get(f"/trips/{trip_id}")).json()
    assert state["revision"] == 1
    assert state["status"] == "planning"
    assert state["brief"]["pace"] == "balanced"
