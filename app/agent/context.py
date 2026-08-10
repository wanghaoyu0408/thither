"""What the model is shown of the trip.

A whole `TripState` is far more than the model needs and grows without bound.
This is a compact, stable projection: enough to reason and to reference things
by id, small enough to send every turn.
"""

from typing import Any

from app.models.common import utcnow
from app.models.trip import TripState, TripTraveler
from app.services.conflict_service import detect_conflicts
from app.services.decision_service import label_for
from app.services.intake_service import (
    missing_blocking,
    outstanding_questions,
    research_allowed,
    today_at,
)
from app.services.opening_hours import describe

MAX_ENTITIES = 60
MAX_ISSUES = 12
MAX_CONFLICTS = 8


def _selected_label(decision: Any) -> str | None:
    """What was chosen, in words the model can repeat back."""
    if not decision.selected_option_id:
        return None
    chosen = next(
        (o for o in decision.options if o.option_id == decision.selected_option_id), None
    )
    return label_for(chosen.data) if chosen else None


def _arrival_line(state: TripState, item: Any) -> dict[str, Any] | None:
    """Parking for one stop, as briefly as it can honestly be put."""
    arrival = state.arrival.get(item.entity_id) if item.entity_id else None
    if arrival is None:
        return None
    spot = arrival.parking.best
    return {
        # "unknown" means nobody checked. It is not "no parking", and the model
        # is told so in the prompt rather than left to infer it.
        "parking": arrival.parking.availability,
        "walk_minutes": spot.walking_minutes if spot else None,
        "cost": spot.cost if spot else None,
        "permit_required": arrival.parking.permit_required,
        "reservation_required": arrival.parking.reservation_required,
    }


def _traveler_line(traveler: TripTraveler) -> dict[str, Any]:
    """One traveller, with enough of their taste to be discussed by name.

    Until M7 this was `{traveler_id, name, role}` and nothing else, so the model
    could not have described what anyone wanted even if asked. Only the fields
    someone would actually argue about are included - the whole preference tree
    every turn would crowd out the trip itself.
    """
    line: dict[str, Any] = {
        "traveler_id": traveler.traveler_id,
        "name": traveler.name,
        "role": traveler.role,
    }

    preferences = traveler.preferences
    if preferences is None:
        # Said rather than defaulted. Neutral weights standing in silently is
        # how a group trip ends up planned for nobody in particular.
        line["preferences"] = "not resolved yet"
        return line

    line["preferences"] = {
        "flight": {
            "price": preferences.flight.price_importance,
            "nonstop": preferences.flight.nonstop_importance,
            "avoid_red_eye": preferences.flight.avoid_red_eye,
            "avoided_airlines": preferences.flight.avoided_airlines,
            "preferred_airlines": preferences.flight.preferred_airlines,
        },
        "hotel": {
            "price": preferences.hotel.price_importance,
            "location": preferences.hotel.location_importance,
            "quiet": preferences.hotel.quiet_importance,
            "min_rating": preferences.hotel.min_rating,
        },
        "food": {
            "dietary_restrictions": preferences.food.dietary_restrictions,
            "cuisines": preferences.food.cuisines[:5],
            "adventurousness": preferences.food.adventurousness,
        },
        "activity": {
            "interests": preferences.activity.interests[:5],
            "avoided": preferences.activity.avoided[:5],
        },
        "pace": {
            "intensity": preferences.pace.intensity,
            "max_daily_walking_km": preferences.pace.max_daily_walking_km,
            "preferred_start_time": preferences.pace.preferred_start_time,
        },
    }
    if preferences.overridden_paths:
        line["overridden_for_this_trip"] = preferences.overridden_paths
    return line


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
        # What day it is, read from the clock on every turn - never written into
        # the prompt, which would freeze it at whatever day the prompt was
        # edited. Without it the model cannot turn "8/10" into a year, and on
        # the first live run of intake it asked the traveller which year they
        # meant about a date two days away.
        "today": today_at(state).isoformat(),
        "now_utc": utcnow().isoformat(),
        "date_zone": state.brief.timezone or "UTC",
        "brief": {
            "destination": state.brief.destination.city or state.brief.destination.country,
            "start": state.brief.dates.start.isoformat() if state.brief.dates.start else None,
            "end": state.brief.dates.end.isoformat() if state.brief.dates.end else None,
            "timezone": state.brief.timezone,
            # Where the trip starts. This was missing, so a traveller who typed
            # their origin into the very first form was asked for it again in
            # the chat - the fact was stored and the model could not see it.
            "origin": {
                "city": state.brief.origin.city,
                "airport_codes": state.brief.origin.airport_codes,
            },
            "party": state.brief.party.model_dump(),
            "budget": state.brief.budget.model_dump(),
            "priorities": state.brief.priorities,
            "pace": state.brief.pace,
            # Shown so the agent neither re-asks nor forgets it when searching.
            "avoid_red_eye": state.brief.avoid_red_eye,
        },
        # Where intake stands, and what is still missing. A rule the model
        # cannot see the state for is not a rule - and without the questions
        # listed here it would ask the same ones again every turn.
        "intake": {
            "status": state.intake.status,
            "research_allowed": research_allowed(state),
            "still_needed": [
                {"requirement_id": gap.requirement_id, "field": gap.field, "why": gap.why}
                for gap in missing_blocking(state)
            ],
            "scope": state.brief.scope.model_dump(),
            # Only the ones still waiting on something. A question whose gap has
            # since closed would otherwise be shown here every turn, and the
            # model would dutifully keep asking it.
            "questions_awaiting_answer": [
                question.question for question in outstanding_questions(state)
            ],
        },
        "travelers": [_traveler_line(traveler) for traveler in state.travelers],
        # Put in front of the model every turn rather than left to a tool call.
        # The acceptance for this milestone is that conflicts get *identified*,
        # and a conflict the model has to remember to go looking for is one that
        # sometimes goes unmentioned.
        "preference_conflicts": [
            {
                "kind": conflict.kind,
                "severity": conflict.severity,
                "summary": conflict.summary,
                # Per person, never merged. Quoting a group average back at
                # people who disagree is the failure this exists to prevent.
                "positions": conflict.positions,
                "affects": conflict.affects[:6],
                "resolution_options": conflict.resolution_options,
            }
            for conflict in detect_conflicts(state)[:MAX_CONFLICTS]
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
        # Where each decision stands. The model was previously told nothing at
        # all about them, so it could not know a flight was already chosen - let
        # alone already booked - and would cheerfully offer to search again.
        "decisions": [
            {
                "name": name,
                "status": decision.status,
                "booked": decision.booked,
                "options": len(decision.options),
                "selected": _selected_label(decision),
                "stale_reason": decision.stale_reason,
            }
            for name, decision in state.decisions.iter_decisions()
        ],
        "entities_total": len(state.entities),
        "entities": [_entity_line(state, entity_id) for entity_id in shown],
        "entities_truncated": max(0, len(entity_ids) - len(shown)),
        "itinerary": [
            {
                "date": day.date.isoformat(),
                "theme": day.theme,
                # A forecast may be planned around; a norm may not be used to
                # say what this date will do. The kind travels with the figures
                # so the model cannot lose the distinction.
                "weather": (
                    {
                        "kind": day.weather.kind,
                        "summary": day.weather.headline(),
                        "is_forecast": day.weather.is_forecast,
                    }
                    if day.weather
                    else None
                ),
                "items": [
                    {
                        "item_id": item.item_id,
                        "title": item.title,
                        "type": item.type,
                        "start_at": item.start_at.isoformat() if item.start_at else None,
                        "end_at": item.end_at.isoformat() if item.end_at else None,
                        "entity_id": item.entity_id,
                        "locked": item.item_id in locked_items,
                        # Where the car goes and how far the walk is. Without
                        # this the model cannot act on "this place has bad
                        # parking" - it would be taking the traveller's word
                        # for something the trip already knows.
                        "arrival": _arrival_line(state, item),
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
