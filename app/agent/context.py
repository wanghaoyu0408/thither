"""What the model is shown of the trip.

A whole `TripState` is far more than the model needs and grows without bound.
This is a compact, stable projection: enough to reason and to reference things
by id, small enough to send every turn.
"""

from typing import Any

from app.models.trip import TripState
from app.services.opening_hours import describe

MAX_ENTITIES = 60
MAX_ISSUES = 12


def _entity_line(state: TripState, entity_id: str) -> dict[str, Any]:
    entity = state.entities[entity_id]
    return {
        "entity_id": entity.entity_id,
        "name": entity.name,
        "categories": entity.categories[:3],
        "rating": entity.rating,
        "rating_count": entity.rating_count,
        "price_level": entity.price_level,
        "hours_published": entity.opening_hours is not None,
    }


def summarize(state: TripState) -> dict[str, Any]:
    """The trip, small enough to send on every turn."""
    entity_ids = sorted(state.entities)
    scheduled = {item.entity_id for _day, item in state.itinerary.iter_items() if item.entity_id}

    # Scheduled places first, so truncation never hides what is actually planned.
    ordered = sorted(entity_ids, key=lambda eid: (eid not in scheduled, eid))
    shown = ordered[:MAX_ENTITIES]

    locked_items = {lock.target_id for lock in state.locks if lock.target_kind == "itinerary_item"}

    return {
        "trip_id": state.trip_id,
        "revision": state.revision,
        "status": state.status,
        "brief": {
            "destination": state.brief.destination.city or state.brief.destination.country,
            "start": state.brief.dates.start.isoformat() if state.brief.dates.start else None,
            "end": state.brief.dates.end.isoformat() if state.brief.dates.end else None,
            "timezone": state.brief.timezone,
            "party": state.brief.party.model_dump(),
            "budget": state.brief.budget.model_dump(),
            "priorities": state.brief.priorities,
            "pace": state.brief.pace,
        },
        "travelers": [
            {"traveler_id": t.traveler_id, "name": t.name, "role": t.role} for t in state.travelers
        ],
        "constraints": [
            {
                "id": c.id,
                "category": c.category,
                "type": c.type,
                "description": c.description,
                "traveler_id": c.traveler_id,
            }
            for c in state.constraints
        ],
        "locks": [
            {
                "lock_id": lock.lock_id,
                "target_kind": lock.target_kind,
                "target_id": lock.target_id,
                "reason": lock.reason,
            }
            for lock in state.locks
        ],
        "rejections": [
            {"target_id": r.target_id, "label": r.label, "reason": r.reason}
            for r in state.rejections
        ],
        "entities_total": len(state.entities),
        "entities": [_entity_line(state, entity_id) for entity_id in shown],
        "entities_truncated": max(0, len(entity_ids) - len(shown)),
        "itinerary": [
            {
                "date": day.date.isoformat(),
                "theme": day.theme,
                "items": [
                    {
                        "item_id": item.item_id,
                        "title": item.title,
                        "type": item.type,
                        "start_at": item.start_at.isoformat() if item.start_at else None,
                        "end_at": item.end_at.isoformat() if item.end_at else None,
                        "entity_id": item.entity_id,
                        "locked": item.item_id in locked_items,
                        "hours": (
                            describe(state.entities[item.entity_id].opening_hours, item.start_at)
                            if item.entity_id in state.entities and item.start_at
                            else None
                        ),
                    }
                    for item in day.items
                ],
            }
            for day in state.itinerary.days
        ],
        "validation": {
            "status": state.validation.status,
            "issues": [
                {"severity": i.severity, "type": i.type, "message": i.message}
                for i in state.validation.issues[:MAX_ISSUES]
            ],
        },
    }
