"""Walk the whole Milestone 1 flow against a running server.

Start the server first:

    .venv/Scripts/python.exe -m uvicorn app.main:app --reload

then:

    .venv/Scripts/python.exe scripts/demo_milestone1.py

Everything printed here comes from real HTTP calls - there is no LLM involved yet.
"""

import sys

import httpx

BASE = "http://127.0.0.1:8000"


def show(label: str, response: httpx.Response) -> dict:
    body = response.json()
    print(f"\n{'=' * 70}\n{label}\n{'-' * 70}")
    print(f"HTTP {response.status_code}")
    return body


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=10.0) as c:
        # 1. A traveler profile: long-term taste, reused across trips.
        profile = show(
            "1. Create a traveler profile",
            c.post(
                "/profiles",
                json={
                    "profile_id": "user_haoyu",
                    "name": "Haoyu",
                    "home_city": "San Francisco",
                    "preferred_airports": ["SFO", "SJC", "OAK"],
                    "hotel_preferences": {
                        "location_importance": 0.95,
                        "price_importance": 0.65,
                        "quiet_importance": 0.7,
                    },
                    "pace_preferences": {"preferred_start_time": "10:00", "intensity": "relaxed"},
                },
            ),
        )
        print(
            f"   {profile['name']}, home {profile['home_city']}, "
            f"airports {profile['preferred_airports']}"
        )

        # 2. A trip. Revision starts at 0.
        trip = show(
            "2. Create the trip",
            c.post(
                "/trips",
                json={
                    "title": "Tokyo food trip",
                    "created_by": "user_haoyu",
                    "brief": {
                        "destination": {"city": "Tokyo", "country": "Japan", "flexible": False},
                        "dates": {"start": "2026-10-03", "end": "2026-10-08"},
                        "party": {"adults": 4, "rooms": 2},
                        "budget": {"total_per_person": 2500, "hotel_per_night": 250},
                        "priorities": ["food", "city exploration"],
                        "pace": "relaxed",
                    },
                    "travelers": [
                        {"traveler_id": "trv_a", "name": "Haoyu", "role": "organizer"},
                        {"traveler_id": "trv_b", "name": "Alice"},
                    ],
                },
            ),
        )
        trip_id = trip["trip_id"]
        print(f"   trip_id={trip_id}  revision={trip['revision']}  status={trip['status']}")

        # 3. Everything below goes through the patch engine. Nothing else can write.
        seeded = show(
            "3. Patch: add a hard constraint, a place, and day one",
            c.post(
                f"/trips/{trip_id}/patch",
                json={
                    "base_revision": 0,
                    "reason": "seed the trip",
                    "actor": "user",
                    "operations": [
                        {"op": "set", "path": "/status", "value": "planning"},
                        {
                            "op": "add",
                            "path": "/constraints/-",
                            "value": {
                                "id": "con_shellfish",
                                "category": "food",
                                "description": "Alice has a shellfish allergy",
                                "type": "hard",
                                "scope": "traveler",
                                "traveler_id": "trv_b",
                                "source": "user_explicit",
                                "confirmed": True,
                            },
                        },
                        {
                            "op": "add",
                            "path": "/entities/ent_cafe",
                            "value": {
                                "entity_type": "place",
                                "entity_id": "ent_cafe",
                                "name": "Fuglen Tokyo",
                                "categories": ["cafe"],
                                "lat": 35.6659,
                                "lng": 139.6979,
                                "rating": 4.3,
                                "rating_count": 2300,
                            },
                        },
                        {
                            "op": "add",
                            "path": "/itinerary/days/-",
                            "value": {
                                "date": "2026-10-03",
                                "theme": "Shibuya",
                                "items": [
                                    {
                                        "item_id": "item_dinner",
                                        "type": "restaurant",
                                        "entity_id": "ent_cafe",
                                        "title": "Dinner at Fuglen",
                                        "start_at": "2026-10-03T19:00:00",
                                        "end_at": "2026-10-03T21:00:00",
                                        "time_flexibility": "fixed",
                                    }
                                ],
                            },
                        },
                    ],
                },
            ),
        )
        print(f"   applied={seeded['applied']}  revision -> {seeded['revision']}")
        for warning in seeded["warnings"]:
            print(f"   WARNING [{warning['code']}] {warning['message']}")
        print("   ^ the allergy is a hard constraint with no checker yet, so it is")
        print("     reported as unverified rather than silently passed.")

        # 4. Lock the dinner: it is booked.
        locked = show(
            "4. Patch: lock the dinner (reservation made)",
            c.post(
                f"/trips/{trip_id}/patch",
                json={
                    "base_revision": 1,
                    "reason": "dinner is booked",
                    "actor": "user",
                    "operations": [
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
                },
            ),
        )
        print(f"   applied={locked['applied']}  revision -> {locked['revision']}")

        move_dinner = [
            {
                "op": "set",
                "path": "/itinerary/days/0/items/0/start_at",
                "value": "2026-10-03T17:00:00",
            }
        ]

        # 5. The agent tries to move it. Refused.
        blocked = show(
            "5. Patch: try to move the locked dinner  (expected to FAIL)",
            c.post(
                f"/trips/{trip_id}/patch",
                json={
                    "base_revision": 2,
                    "reason": "make the evening lighter",
                    "operations": move_dinner,
                },
            ),
        )
        for error in blocked["errors"]:
            print(f"   ERROR [{error['code']}] {error['message']}")

        # 6. The user says yes. Now it moves.
        released = show(
            "6. Patch: same change, user explicitly unlocked it",
            c.post(
                f"/trips/{trip_id}/patch",
                json={
                    "base_revision": 2,
                    "reason": "user agreed to move dinner",
                    "actor": "user",
                    "operations": move_dinner,
                    "unlock_targets": ["lock_dinner"],
                },
            ),
        )
        print(f"   applied={released['applied']}  revision -> {released['revision']}")
        print(
            f"   dinner now at {released['state']['itinerary']['days'][0]['items'][0]['start_at']}"
        )

        # 7. Rejection memory.
        c.post(
            f"/trips/{trip_id}/patch",
            json={
                "base_revision": 3,
                "reason": "user does not want Ichiran",
                "actor": "user",
                "operations": [
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
            },
        )
        rejected = show(
            "7. Patch: recommend the rejected restaurant again  (expected to FAIL)",
            c.post(
                f"/trips/{trip_id}/patch",
                json={
                    "base_revision": 4,
                    "reason": "how about Ichiran",
                    "operations": [
                        {
                            "op": "add",
                            "path": "/entities/ent_ramen",
                            "value": {
                                "entity_type": "place",
                                "entity_id": "ent_ramen",
                                "name": "Ichiran Shibuya",
                                "lat": 35.659,
                                "lng": 139.700,
                            },
                        }
                    ],
                },
            ),
        )
        for error in rejected["errors"]:
            print(f"   ERROR [{error['code']}] {error['message']}")

        # 8. Two writers on the same base revision.
        stale = show(
            "8. Patch: a stale writer using an old revision  (expected to FAIL)",
            c.post(
                f"/trips/{trip_id}/patch",
                json={
                    "base_revision": 0,
                    "reason": "stale client",
                    "operations": [{"op": "set", "path": "/status", "value": "ready"}],
                },
            ),
        )
        for error in stale["errors"]:
            print(f"   ERROR [{error['code']}] {error['message']}")

        # 9. The audit trail.
        events = show("9. Audit trail", c.get(f"/trips/{trip_id}/events"))
        for event in events:
            print(f"   r{event['revision']:>2}  {event['event_type']}")

        final = c.get(f"/trips/{trip_id}").json()
        print(f"\n{'=' * 70}")
        print(f"Final revision: {final['revision']}   (only the 4 valid patches counted)")
        print(f"Open the trip in the browser: {BASE}/docs")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError:
        sys.exit(
            "Cannot reach the server. Start it first:\n"
            "  .venv/Scripts/python.exe -m uvicorn app.main:app --reload"
        )
