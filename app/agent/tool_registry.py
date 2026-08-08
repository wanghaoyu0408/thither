"""Tools the agent can call (spec section 13).

Business capabilities, not endpoints: `search_places`, not
`google_places_http_request`. The provider underneath stays replaceable, and
the model never learns a vendor's request shape.

Every tool here either reads or *proposes*. Only `apply_trip_patch` changes a
trip, and it does so through the same validated patch engine as everything else.
"""

import json
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Any

from app.config import Settings
from app.models.itinerary_plan import ItineraryProposal, PlanParams, ReplanParams
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, SearchPlacesInput
from app.models.research import ResearchWebInput
from app.models.route import GetRoutesInput, LocationRef
from app.models.trip import TripState
from app.services.entity_service import resolve_places
from app.services.itinerary_service import build_itinerary, replan_day
from app.services.proposal_store import ProposalStore
from app.services.toolbox import Toolbox
from app.services.validation_service import (
    TravelLookup,
    build_travel_lookup,
    validate_itinerary,
)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_places",
        "description": (
            "Find real restaurants, cafes, bars, museums, parks or shops. Returns Google "
            "data: name, rating, review count, price level, location. Use this before "
            "planning so the itinerary is built from places that exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for, e.g. 'izakaya', 'specialty coffee'.",
                },
                "near": {
                    "type": "string",
                    "description": "Where, in words, e.g. 'Shibuya, Tokyo'.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "min_rating": {"type": "number", "minimum": 0, "maximum": 5},
                "store": {
                    "type": "boolean",
                    "description": (
                        "Propose adding the top results to the trip's place registry so they "
                        "can be scheduled. Defaults to true."
                    ),
                },
            },
            "required": ["query", "near"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_place_details",
        "description": (
            "Opening hours and website for places already found. Only call this for a "
            "shortlist of three to five - it is the expensive call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["entity_ids"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_routes",
        "description": (
            "Real travel times between stored places. Never estimate a journey yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin_entity_ids": {"type": "array", "items": {"type": "string"}},
                "destination_entity_ids": {"type": "array", "items": {"type": "string"}},
                "mode": {"type": "string", "enum": ["walking", "transit", "driving"]},
            },
            "required": ["origin_entity_ids", "destination_entity_ids"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "research_web",
        "description": (
            "What people actually say about places - Xiaohongshu, Reddit, blogs and "
            "publications. Use this for taste, reputation and local knowledge. It is NOT a "
            "source of opening hours, addresses, prices or travel times; those come from "
            "Google. Reports which sources returned nothing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "e.g. 'best izakaya locals actually go to'.",
                },
                "near": {"type": "string", "description": "e.g. 'Shibuya, Tokyo'."},
                "purpose": {
                    "type": "string",
                    "enum": [
                        "restaurant_discovery",
                        "activity_discovery",
                        "hotel_research",
                        "neighborhood_research",
                        "destination_research",
                        "general",
                    ],
                },
                "recency_days": {"type": ["integer", "null"]},
            },
            "required": ["query", "near"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "discover_restaurants",
        "description": (
            "The full recommendation pipeline: Google candidates plus community research, "
            "resolved against real places, ranked, and returned with the sources behind "
            "each one. Prefer this over calling search_places and research_web separately. "
            "Still works when Xiaohongshu or all research is unavailable, on Google data "
            "alone, and says which happened."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "e.g. 'izakaya', 'ramen'."},
                "near": {"type": "string", "description": "e.g. 'Asakusa, Tokyo'."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                "min_rating": {"type": ["number", "null"], "minimum": 0, "maximum": 5},
            },
            "required": ["query", "near"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "generate_itinerary",
        "description": (
            "Lay out the whole trip from the places already stored: clusters them "
            "geographically, respects opening hours, and validates travel times. Returns a "
            "proposal with a validation report - it does not change the trip. Apply it with "
            "apply_trip_patch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 30},
                "areas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Neighbourhood name per day, in order, for day themes.",
                },
                "intensity": {"type": "string", "enum": ["relaxed", "balanced", "packed"]},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "replan_day",
        "description": (
            "Rework one day and nothing else. Use this whenever the user complains about a "
            "single day. Locked items are preserved. Returns a proposal scoped to that day."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date, e.g. 2026-10-05."},
                "intensity": {"type": "string", "enum": ["relaxed", "balanced", "packed"]},
                "max_items": {"type": "integer", "minimum": 0, "maximum": 12},
                "keep_item_ids": {"type": "array", "items": {"type": "string"}},
                "drop_item_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "validate_itinerary",
        "description": (
            "Check the itinerary for overlaps, closed venues, impossible travel times and "
            "overloaded days. Reports problems; fixes nothing."
        ),
        "parameters": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "Optional ISO date."}},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "apply_trip_patch",
        "description": (
            "Commit a proposal to the trip. Pass the proposal_id you were given. The server "
            "validates locks, rejections, constraints and scope before anything is written."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Why, in the user's terms. Goes into the audit trail.",
                },
                "unlock_targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "lock_ids the user explicitly agreed to release.",
                },
            },
            "required": ["proposal_id", "reason"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class ToolInvocation:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    milliseconds: int = 0


@dataclass
class ToolContext:
    """Everything a tool call needs, plus the run's bookkeeping."""

    state: TripState
    toolbox: Toolbox
    proposals: ProposalStore
    settings: Settings

    travel: TravelLookup = field(default_factory=TravelLookup)
    searches_used: int = 0
    pending_entity_ops: list = field(default_factory=list)
    # evidence_id -> EvidenceRecord discovered this turn, written alongside the
    # places it backs so an option's evidence_refs always resolve.
    pending_evidence: dict = field(default_factory=dict)

    def budget_left(self) -> int:
        return max(0, self.settings.planning_search_budget - self.searches_used)


def _proposal_view(proposal: ItineraryProposal) -> dict[str, Any]:
    return {
        "proposal_id": proposal.proposal_id,
        "summary": proposal.summary,
        "days_changed": [d.isoformat() for d in proposal.days_changed],
        "scope": proposal.scope.model_dump() if proposal.scope else None,
        "days": [day.model_dump(mode="json") for day in proposal.days],
        "validation": {
            "status": proposal.validation.status,
            "issues": [
                {"severity": i.severity, "type": i.type, "message": i.message}
                for i in proposal.validation.issues
            ],
        },
        "warnings": proposal.warnings,
        "note": "Nothing has changed yet. Call apply_trip_patch with this proposal_id.",
    }


async def _search_places(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if context.budget_left() <= 0:
        return {
            "error": "search budget exhausted for this turn",
            "hint": "plan with the places already stored, or ask the user to narrow the search",
        }
    context.searches_used += 1

    query = f"{args['query']} in {args['near']}"
    result = await context.toolbox.places.search_places(
        SearchPlacesInput(
            query=query,
            # Text Search resolves the neighbourhood from the query itself, so no
            # coordinates are needed from the model.
            lat=0.0,
            lng=0.0,
            limit=min(int(args.get("limit", 12)), 20),
            min_rating=args.get("min_rating"),
        )
    )
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}
    if result.found_nothing:
        return {"results": [], "note": f"nothing matched {query!r}; the search itself worked"}

    places = result.results
    stored: list[str] = []
    if args.get("store", True):
        entities = resolve_places(places, context.state.entities)
        for entity in entities:
            context.pending_entity_ops.append(entity)
            stored.append(entity.entity_id)

    return {
        "results": [
            {
                "entity_id": stored[index] if index < len(stored) else None,
                "name": place.name,
                "rating": place.rating,
                "rating_count": place.rating_count,
                "price_level": place.price_level,
                "address": place.address,
            }
            for index, place in enumerate(places)
        ],
        "stored_in_registry": bool(stored),
        "searches_left": context.budget_left(),
    }


async def _get_place_details(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    known = {**context.state.entities, **{e.entity_id: e for e in context.pending_entity_ops}}
    wanted = [known[eid] for eid in args.get("entity_ids", []) if eid in known]
    if not wanted:
        return {"error": "none of those entity_ids are known to this trip"}

    place_ids = [e.provider_refs.get("google_place_id") for e in wanted]
    place_ids = [pid for pid in place_ids if pid]
    result = await context.toolbox.places.get_place_details(
        GetPlaceDetailsInput(place_ids=place_ids, field_set=PlaceFieldSet.FULL)
    )
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}

    refreshed = resolve_places(result.results, known)
    context.pending_entity_ops.extend(refreshed)

    return {
        "results": [
            {
                "entity_id": entity.entity_id,
                "name": entity.name,
                "hours_published": entity.opening_hours is not None,
                "website": entity.website_url,
                "rating": entity.rating,
            }
            for entity in refreshed
        ]
    }


async def _get_routes(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    known = {**context.state.entities, **{e.entity_id: e for e in context.pending_entity_ops}}
    origins = [eid for eid in args.get("origin_entity_ids", []) if eid in known]
    destinations = [eid for eid in args.get("destination_entity_ids", []) if eid in known]
    if not origins or not destinations:
        return {"error": "unknown entity_ids; search and store the places first"}

    mode = args.get("mode", "walking")
    result = await context.toolbox.routes.get_routes(
        GetRoutesInput(
            origins=[LocationRef(entity_id=eid) for eid in origins],
            destinations=[LocationRef(entity_id=eid) for eid in destinations],
            mode=mode,
        ),
        entities=known,
    )
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}

    lookup = build_travel_lookup(result.results, destinations, mode)
    # build_travel_lookup keys on one list; redo it properly for the matrix.
    for leg in result.results:
        if leg.status != "ok" or leg.duration_seconds is None:
            continue
        key = (origins[leg.origin_index], destinations[leg.destination_index], mode)
        context.travel.minutes[key] = leg.duration_seconds / 60.0
        if leg.distance_meters is not None:
            context.travel.meters[key] = float(leg.distance_meters)
    del lookup

    return {
        "legs": [
            {
                "from": known[origins[leg.origin_index]].name,
                "to": known[destinations[leg.destination_index]].name,
                "minutes": round(leg.duration_minutes, 1) if leg.duration_minutes else None,
                "mode": mode,
                "status": leg.status,
            }
            for leg in result.results
        ],
        "warnings": result.warnings,
    }


def _working_state(context: ToolContext) -> TripState:
    """State as the model believes it to be, including places found this turn."""
    if not context.pending_entity_ops:
        return context.state
    working = context.state.model_copy(deep=True)
    for entity in context.pending_entity_ops:
        working.entities[entity.entity_id] = entity
    return working


async def _research_web(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if context.toolbox.research is None:
        return {
            "error": "web research is not configured (no OPENAI_API_KEY)",
            "hint": "plan from Google data and say that community signal is unavailable",
        }
    if context.budget_left() <= 0:
        return {"error": "search budget exhausted for this turn"}
    context.searches_used += 1

    result = await context.toolbox.research.research_web(
        ResearchWebInput(
            query=args["query"],
            near=args.get("near"),
            purpose=args.get("purpose", "general"),
            recency_days=args.get("recency_days"),
        )
    )
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}

    return {
        "sources": [
            {
                "url": row.url,
                "title": row.title,
                "source_type": row.source_type,
                "tier": row.tier,
                "summary": row.summary,
                "mentions": [
                    {
                        "name": mention.name,
                        "kind": mention.kind,
                        "sentiment": mention.sentiment,
                        "themes": mention.themes,
                    }
                    for mention in row.mentioned_entities
                ],
            }
            for row in result.results
        ],
        "warnings": result.warnings,
        "note": (
            "Discovery and taste only. Do not quote hours, addresses, prices or travel "
            "times from these - verify with Google first."
        ),
    }


async def _discover_restaurants(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if context.budget_left() <= 0:
        return {"error": "search budget exhausted for this turn"}
    context.searches_used += 1

    known = {**context.state.entities, **{e.entity_id: e for e in context.pending_entity_ops}}
    outcome = await context.toolbox.discovery.discover(
        query=args["query"],
        near=args["near"],
        existing_entities=known,
        limit=min(int(args.get("limit", 5)), 8),
        min_rating=args.get("min_rating", 4.0),
    )

    context.pending_entity_ops.extend(outcome.entities.values())
    context.pending_evidence.update(outcome.evidence)

    return {
        "recommendations": [
            {
                "entity_id": rec.entity_id,
                "name": rec.ranked.place.name,
                "rating": rec.ranked.place.rating,
                "rating_count": rec.ranked.place.rating_count,
                "price_level": rec.ranked.place.price_level,
                "score": rec.ranked.score.total,
                "dimensions": rec.ranked.score.dimensions,
                "pros": rec.ranked.pros,
                "cons": rec.ranked.cons,
                "evidence_refs": rec.evidence_ids,
                "community": (
                    {
                        "sources": rec.signal.source_count,
                        "sentiment": rec.signal.sentiment,
                        "themes": rec.signal.themes,
                    }
                    if rec.signal
                    else None
                ),
            }
            for rec in outcome.recommendations
        ],
        "evidence": [
            {
                "evidence_id": record.evidence_id,
                "url": record.url,
                "title": record.title,
                "source_type": record.source_type,
                "source_authority": record.source_authority,
            }
            for record in outcome.evidence.values()
        ],
        "unresolved_mentions": [
            {"name": mention.name, "why": mention.resolution_note}
            for mention in outcome.unresolved_mentions
        ],
        "google_only": outcome.google_only,
        "warnings": outcome.warnings,
    }


MAX_AUTO_DETAILS = 24


async def _ensure_hours(context: ToolContext, entity_ids: list[str]) -> int:
    """Fetch opening hours for places that lack them.

    The planner picks slots by whether a venue is open, so hours have to be
    known *before* scheduling. Doing it here rather than hoping the model
    remembers to call get_place_details is the difference between an itinerary
    that is checked and one that merely says it is unverified.
    """
    known = {**context.state.entities, **{e.entity_id: e for e in context.pending_entity_ops}}
    missing = [
        known[eid].provider_refs.get("google_place_id")
        for eid in entity_ids
        if eid in known and known[eid].opening_hours is None
    ]
    missing = [pid for pid in missing if pid][:MAX_AUTO_DETAILS]
    if not missing:
        return 0

    result = await context.toolbox.places.get_place_details(
        GetPlaceDetailsInput(place_ids=missing, field_set=PlaceFieldSet.FULL)
    )
    if not result.ok:
        return 0

    context.pending_entity_ops.extend(resolve_places(result.results, known))
    return len(result.results)


async def _ensure_routes(context: ToolContext, proposal) -> int:
    """Look up real travel times between consecutive stops on each proposed day."""
    known = {**context.state.entities, **{e.entity_id: e for e in context.pending_entity_ops}}

    pairs: list[tuple[str, str]] = []
    for day in proposal.days:
        scheduled = [item.entity_id for item in day.items if item.entity_id in known]
        pairs.extend(zip(scheduled, scheduled[1:], strict=False))

    pairs = [
        pair
        for pair in pairs
        if pair[0] != pair[1] and (pair[0], pair[1], "walking") not in context.travel.minutes
    ]
    if not pairs:
        return 0

    origins = sorted({origin for origin, _ in pairs})
    destinations = sorted({destination for _, destination in pairs})

    result = await context.toolbox.routes.get_routes(
        GetRoutesInput(
            origins=[LocationRef(entity_id=eid) for eid in origins],
            destinations=[LocationRef(entity_id=eid) for eid in destinations],
            mode="walking",
        ),
        entities=known,
    )
    if not result.ok:
        return 0

    for leg in result.results:
        if leg.status != "ok" or leg.duration_seconds is None:
            continue
        key = (origins[leg.origin_index], destinations[leg.destination_index], "walking")
        context.travel.minutes[key] = leg.duration_seconds / 60.0
        if leg.distance_meters is not None:
            context.travel.meters[key] = float(leg.distance_meters)
    return len(result.results)


async def _generate_itinerary(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    working = _working_state(context)
    await _ensure_hours(context, list(working.entities))

    params = PlanParams(
        days=args.get("days"),
        areas=args.get("areas", []),
        intensity=args.get("intensity"),
    )

    proposal = build_itinerary(_working_state(context), params=params, travel=context.travel)
    # Now that the day's stops are known, price the journeys between them and
    # re-run validation against real durations rather than none.
    if await _ensure_routes(context, proposal):
        proposal = build_itinerary(_working_state(context), params=params, travel=context.travel)

    context.proposals.put(proposal)
    return _proposal_view(proposal)


async def _replan_day(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        target = date_type.fromisoformat(args["date"])
    except (KeyError, ValueError):
        return {"error": f"date must be an ISO date, got {args.get('date')!r}"}

    params = ReplanParams(
        intensity=args.get("intensity"),
        max_items=args.get("max_items"),
        keep_item_ids=args.get("keep_item_ids", []),
        drop_item_ids=args.get("drop_item_ids", []),
    )

    working = _working_state(context)
    day = next((d for d in working.itinerary.days if d.date == target), None)
    if day is not None:
        await _ensure_hours(context, [i.entity_id for i in day.items if i.entity_id])

    proposal = replan_day(_working_state(context), target, params=params, travel=context.travel)
    if await _ensure_routes(context, proposal):
        proposal = replan_day(_working_state(context), target, params=params, travel=context.travel)

    context.proposals.put(proposal)
    return _proposal_view(proposal)


async def _validate_itinerary(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    target = None
    if args.get("date"):
        try:
            target = date_type.fromisoformat(args["date"])
        except ValueError:
            return {"error": f"date must be an ISO date, got {args['date']!r}"}

    result = validate_itinerary(_working_state(context), travel=context.travel, target_date=target)
    return {
        "status": result.status,
        "issues": [
            {
                "severity": i.severity,
                "type": i.type,
                "message": i.message,
                "suggested_fix": i.suggested_fix,
            }
            for i in result.issues
        ],
    }


async def _apply_trip_patch(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    proposal = context.proposals.get(args.get("proposal_id", ""))
    if proposal is None:
        return {
            "error": "no such proposal; call generate_itinerary or replan_day first",
            "applied": False,
        }

    reason = args.get("reason", "agent update")

    # Places discovered or refreshed this turn have to land before anything
    # references them.
    entity_ops = [
        {
            "op": "add" if entity.entity_id not in context.state.entities else "set",
            "path": f"/entities/{entity.entity_id}",
            "value": entity.model_dump(mode="json"),
        }
        for entity in context.pending_entity_ops
    ]
    # Evidence lands with the places it backs, so evidence_refs never dangle.
    entity_ops += [
        {
            "op": "add" if evidence_id not in context.state.evidence else "set",
            "path": f"/evidence/{evidence_id}",
            "value": record.model_dump(mode="json"),
        }
        for evidence_id, record in context.pending_evidence.items()
    ]
    itinerary_ops = [op.model_dump(mode="json") for op in proposal.operations]

    plans: list[dict[str, Any]] = []

    # A day-scoped patch may add places but not rewrite existing ones - so
    # refreshing a venue's opening hours cannot ride along inside it. Updating
    # Google's facts about a place is not "changing day 3" anyway; it is its own
    # operation, and splitting it keeps the scope guarantee absolute rather than
    # negotiable.
    rewrites_existing = any(op["op"] == "set" for op in entity_ops)
    if proposal.scope is not None and rewrites_existing:
        plans.append(
            {
                "operations": entity_ops,
                "scope": None,
                "reason": f"refresh place facts before: {reason}",
            }
        )
        entity_ops = []

    plans.append(
        {
            "operations": entity_ops + itinerary_ops,
            "scope": proposal.scope.model_dump(mode="json") if proposal.scope else None,
            "reason": reason,
            "unlock_targets": args.get("unlock_targets", []),
        }
    )

    return {"__patches__": plans, "proposal_id": proposal.proposal_id}


HANDLERS = {
    "research_web": _research_web,
    "discover_restaurants": _discover_restaurants,
    "search_places": _search_places,
    "get_place_details": _get_place_details,
    "get_routes": _get_routes,
    "generate_itinerary": _generate_itinerary,
    "replan_day": _replan_day,
    "validate_itinerary": _validate_itinerary,
    "apply_trip_patch": _apply_trip_patch,
}


async def dispatch(context: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return await handler(context, arguments)
    except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the turn
        return {"error": f"{type(exc).__name__}: {exc}"}


def serialize(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
