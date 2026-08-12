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
from app.models.arrival import ArrivalContext
from app.models.common import utcnow
from app.models.constraint import TripConstraint
from app.models.proposal import AgentProposal, ProposalChoice
from app.models.decision import (
    Decision,
    DecisionOption,
    DecisionScore,
    FlightOptionData,
    PlaceOption,
)
from app.models.flight import AirportOption, SearchAirportsInput, SearchFlightsInput
from app.models.group import TravelerPreferences
from app.models.hotel import SearchHotelsInput
from app.models.intake import SCOPE_LABELS, ClarificationChoice, ClarificationQuestion
from app.models.itinerary_plan import ItineraryProposal, PlanParams, ReplanParams
from app.models.learning import LearningSignal
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, SearchPlacesInput
from app.models.research import ResearchWebInput
from app.models.route import GetRoutesInput, LocationRef
from app.models.traveler import FlightPreferences, HotelPreferences
from app.models.trip import OpenQuestion, TripState
from app.services import json_pointer as jp
from app.services.conflict_service import detect_conflicts
from app.services.decision_service import label_for
from app.services.entity_service import resolve_places
from app.services.flight_ranking import cheapest_of, explain_choice, rank_flights
from app.services.flight_service import SANDBOX_DISCLAIMER
from app.services.group_scoring import (
    group_ranking_value,
    rank_flights_for_group,
    rank_hotels_for_group,
    rank_places_for_group,
)
from app.services.hotel_area_service import build_area_decision
from app.services.hotel_ranking import describe_prices, describe_ratings, explain_hotel_choice
from app.services.hotel_service import (
    SANDBOX_DISCLAIMER as HOTEL_SANDBOX_DISCLAIMER,
)
from app.services.hotel_service import (
    build_hotel_decision,
)
from app.services.intake_service import (
    missing_blocking,
    ready_to_confirm,
    research_allowed,
    resolve_date,
    today_at,
)
from app.services.learning_service import CATALOGUE, derive_hypotheses
from app.services.itinerary_service import (
    arrival_penalty,
    build_itinerary,
    replan_day,
    substitute_item,
    substitution_candidates,
    weather_penalty,
)
from app.services.preference_service import (
    diff_profile,
    effective,
    resolve,
    stale_targets,
    traveler_names,
)
from app.services.proposal_store import ProposalStore
from app.services.toolbox import Toolbox
from app.services.validation_service import (
    TravelLookup,
    build_travel_lookup,
    long_haul_mode,
    mode_between,
    validate_itinerary,
)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "update_trip_brief",
        "description": (
            "Write down what the traveller has told you about the trip: where, when, who, "
            "budget, priorities, and which parts they want you to plan. Pass ONLY fields "
            "they actually gave you - omitting a field leaves it alone, and guessing one "
            "puts a fact in the trip that nobody said. Call this before asking anything, "
            "so you never ask for something you were already told. Resolve loose dates "
            "against `today` in the state: '8/10-8/14' with no year is the next 10-14 "
            "August from today, and you record it rather than asking which year."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "destination_city": {
                    "type": ["string", "null"],
                    "description": (
                        "The place they named, when it is a city or a place you would "
                        "search within: Tokyo, Maui, Lisbon. Not the country."
                    ),
                },
                "destination_region": {
                    "type": ["string", "null"],
                    "description": "A wider area, when that is what they named: the Amalfi Coast.",
                },
                "destination_country": {
                    "type": ["string", "null"],
                    "description": (
                        "Only alongside a city or region, never instead of one. 'United "
                        "States' is not a destination anything can be planned around."
                    ),
                },
                "destination_flexible": {
                    "type": ["boolean", "null"],
                    "description": "True when they have not picked, or want you to suggest.",
                },
                "origin_city": {"type": ["string", "null"]},
                "origin_airport_codes": {"type": "array", "items": {"type": "string"}},
                "start_date": {"type": ["string", "null"], "description": "ISO date."},
                "end_date": {"type": ["string", "null"], "description": "ISO date."},
                "earliest_date": {
                    "type": ["string", "null"],
                    "description": "ISO. Start of the window when the dates are not decided.",
                },
                "latest_date": {"type": ["string", "null"], "description": "ISO. End of it."},
                "duration_nights": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "Use with the window for 'four days in October'.",
                },
                "adults": {"type": ["integer", "null"], "minimum": 1},
                "children": {"type": ["integer", "null"], "minimum": 0},
                "rooms": {"type": ["integer", "null"], "minimum": 1},
                "budget_total_per_person": {"type": ["number", "null"]},
                "budget_hotel_per_night": {"type": ["number", "null"]},
                "priorities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the trip is for, in their words: food, beaches, hiking.",
                },
                "pace": {"type": ["string", "null"], "enum": ["relaxed", "balanced", "packed"]},
                "notes": {
                    "type": ["string", "null"],
                    "description": (
                        "Anything else they want planned around, in their own words - the "
                        "record of what they asked for. Add to it; never paraphrase it away, "
                        "and never clear it because you have turned it into constraints."
                    ),
                },
                "scope": {
                    "type": "object",
                    "description": (
                        "What to do about each part. 'already_arranged' means booked or "
                        "otherwise sorted - plan around it and never shop for it."
                    ),
                    "properties": {
                        part: {
                            "type": "string",
                            "enum": ["plan", "already_arranged", "not_needed", "unknown"],
                        }
                        for part in SCOPE_LABELS
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "ask_clarifications",
        "description": (
            "Ask the traveller one to three questions - only for facts that actually block "
            "planning, and only ones you were not already told. Offer choices when the "
            "answers are genuinely enumerable; they can always reply in their own words "
            "instead. Do not ask for a destination they have deliberately left open: "
            "choosing one is your job, not theirs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["single_choice", "multi_choice", "text", "dates"],
                            },
                            "requirement_id": {
                                "type": "string",
                                "description": (
                                    "Which outstanding requirement this asks about, copied "
                                    "from `intake.still_needed` in the state. A question "
                                    "that does not name one is not asked."
                                ),
                            },
                            "choices": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "value": {"type": "string"},
                                        "label": {"type": "string"},
                                        "description": {"type": ["string", "null"]},
                                    },
                                    "required": ["value", "label"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["question", "kind", "requirement_id"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    },
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
        "name": "get_weather_context",
        "description": (
            "Weather for the trip's days, saved onto the trip. Each day comes back as one "
            "of two kinds and they are not interchangeable: a 'forecast' is about that "
            "date and you may plan around it; a 'historical_norm' says what the season is "
            "usually like and can never tell anyone what a date will do. Call this before "
            "planning outdoor days, and again if the dates move."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "search_airports",
        "description": (
            "Airports near a place, with the real driving time to each from the Routes API. "
            "Use this before searching flights when the traveller could plausibly use more "
            "than one airport - the drive time is what makes comparing them meaningful."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "e.g. 'San Francisco Bay Area'."},
                "role": {
                    "type": "string",
                    "enum": ["departure", "arrival"],
                    "description": (
                        "Which end of the trip this is. Given, the shortlist is kept as a "
                        "decision the traveller can see and change on its own."
                    ),
                },
                "max_ground_travel_minutes": {"type": ["integer", "null"], "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["location"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_flights",
        "description": (
            "Priced flight options. Pass several origins to compare airports in one call. "
            "Results are ranked on price, stops, duration, timing and airport drive time, "
            "and come with a structured trade-off between the best and the cheapest. "
            "Check live_mode: false means the provider's test environment, and those fares "
            "are not real."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IATA codes, e.g. ['SFO','SJC','OAK'].",
                },
                "destinations": {"type": "array", "items": {"type": "string"}},
                "departure_date": {"type": "string", "description": "ISO date."},
                "return_date": {"type": ["string", "null"], "description": "ISO date."},
                "adults": {"type": "integer", "minimum": 1, "maximum": 9},
                "children": {"type": "integer", "minimum": 0, "maximum": 9},
                "cabin": {
                    "type": "string",
                    "enum": ["economy", "premium_economy", "business"],
                },
                "max_stops": {"type": ["integer", "null"], "minimum": 0, "maximum": 3},
                "max_price_per_person": {"type": ["number", "null"]},
                "override_booked": {
                    "type": "boolean",
                    "description": (
                        "Search even though the flights are already booked. Only when the "
                        "traveller asked for that in so many words."
                    ),
                },
            },
            "required": ["origins", "destinations", "departure_date"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "recommend_hotel_areas",
        "description": (
            "Rank neighbourhoods to stay in, by REAL travel time from each to the places this "
            "trip actually visits. Call this BEFORE search_hotels - choosing an area is its "
            "own decision and comes first. Needs some places to be stored already, because "
            "that is what an area's convenience is measured against. Returns mean and "
            "worst-case travel minutes per area; present those figures, do not invent your own."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "suggested_areas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Neighbourhoods worth considering, e.g. ['Shinjuku','Ginza']. They are "
                        "geocoded and ranked alongside areas derived from the trip itself."
                    ),
                },
                "mode": {"type": "string", "enum": ["walking", "transit", "driving"]},
                "limit": {"type": "integer", "minimum": 2, "maximum": 6},
                "select_top": {
                    "type": "boolean",
                    "description": (
                        "Select the top area outright. Only when the traveller has already "
                        "said to just pick one; otherwise present the ranking and let them "
                        "choose."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_hotels",
        "description": (
            "Priced hotels inside the chosen neighbourhood. Requires an area: pass area_name, "
            "or select one with recommend_hotel_areas first. Returns each hotel's ratings "
            "SEPARATELY - a star category and a guest rating are different measurements and "
            "must never be merged or compared with each other. Prices are per booking site, "
            "so name the site when quoting one. Check live_mode."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "check_in": {"type": "string", "description": "ISO date."},
                "check_out": {"type": "string", "description": "ISO date."},
                "area_name": {
                    "type": ["string", "null"],
                    "description": "Neighbourhood to search. Defaults to the trip's chosen area.",
                },
                "adults": {"type": "integer", "minimum": 1, "maximum": 16},
                "rooms": {"type": "integer", "minimum": 1, "maximum": 8},
                "max_nightly_price": {"type": ["number", "null"]},
                "min_rating": {"type": ["number", "null"], "minimum": 0, "maximum": 5},
                "min_star_category": {"type": ["number", "null"], "minimum": 0, "maximum": 5},
                "bypass_area_decision": {
                    "type": "boolean",
                    "description": (
                        "Skip the neighbourhood step. Only when the traveller named a specific "
                        "hotel or told you to search the whole city. Requires bypass_reason."
                    ),
                },
                "bypass_reason": {"type": ["string", "null"]},
                "override_booked": {
                    "type": "boolean",
                    "description": (
                        "Search even though the hotel is already booked. Only when the "
                        "traveller asked for that in so many words."
                    ),
                },
            },
            "required": ["check_in", "check_out"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "review_group_preferences",
        "description": (
            "Who wants what, and where they disagree. Call this ONCE before recommending "
            "anything to a group of two or more; never on a solo trip, where there is "
            "nobody to disagree with. Resolves each traveller's stored profile into the "
            "trip (overrides win) and returns every preference conflict with each person's "
            "position stated separately. NEVER report a group score without also reporting "
            "its split - an option three people love and one hates has the same average as "
            "one everybody finds mediocre, and they are not the same trip."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "resolve_missing": {
                    "type": "boolean",
                    "description": (
                        "Fill in preferences for travellers who have a linked profile but no "
                        "snapshot yet. Defaults to true; it only adds what is missing."
                    ),
                },
                "raise_questions": {
                    "type": "boolean",
                    "description": (
                        "Record blocking conflicts as open questions the group must answer. "
                        "Defaults to true. The trip cannot be marked ready until they are."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "refresh_traveler_preferences",
        "description": (
            "Check whether anyone's stored profile has changed since this trip copied it. "
            "Called plain it reports the differences and changes NOTHING. Call it again with "
            "confirm=true only after the user has seen the differences and agreed. Applying "
            "marks the affected decisions stale; it never silently replans anything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "Apply the differences. Only after showing them to the user and "
                        "getting agreement."
                    ),
                },
                "traveler_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Limit to these travellers. Omit for everyone.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "propose_next_step",
        "description": (
            "Put a fork to the traveller as buttons they can press. Use this the moment a "
            "search comes back empty, an option turns out to be impossible, or you are "
            "about to write 'the next step would be...' - describing a next step and then "
            "ending your turn leaves them with nothing to click. Always include a way to "
            "leave it for now: a question with only one usable answer is not a question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The fork, in one sentence. 'Fly into O'Hare instead?'",
                },
                "detail": {
                    "type": ["string", "null"],
                    "description": (
                        "Why you are asking, from what the tools returned. Never a reason "
                        "composed after the fact."
                    ),
                },
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["select_option", "set_aside", "resume", "none"],
                                "description": (
                                    "select_option settles a decision on one of its existing "
                                    "options; set_aside parks a part with a reason; resume "
                                    "picks a parked part back up; none just carries on."
                                ),
                            },
                            "decision": {
                                "type": ["string", "null"],
                                "description": "For select_option: 'arrival_airport', 'hotel'...",
                            },
                            "option_id": {
                                "type": ["string", "null"],
                                "description": "For select_option: an option that already exists.",
                            },
                            "part": {
                                "type": ["string", "null"],
                                "enum": ["flights", "lodging", None],
                                "description": "For set_aside / resume.",
                            },
                            "note": {
                                "type": ["string", "null"],
                                "description": "What this choice costs, in their terms.",
                            },
                        },
                        "required": ["label", "action"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["question", "choices"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "record_constraints",
        "description": (
            "Turn a requirement the traveller stated into one or more constraints the trip "
            "carries - 'back by 9pm on the 23rd', 'no long walks', 'nothing over $200 a "
            "night'. Read brief.notes for what they actually said. Their words stay exactly "
            "as they wrote them: a constraint is your reading of them, never a replacement, "
            "so never rewrite or clear the notes because you have recorded one. Mark hard "
            "only what cannot be traded off; everything else is soft."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "constraints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "enum": [
                                    "budget", "flight", "hotel", "food", "mobility",
                                    "schedule", "activity", "transport", "other",
                                ],
                            },
                            "description": {
                                "type": "string",
                                "description": "One sentence, close to how they put it.",
                            },
                            "type": {"type": "string", "enum": ["hard", "soft"]},
                            "traveler_id": {
                                "type": ["string", "null"],
                                "description": "Whose requirement this is, if only one person's.",
                            },
                        },
                        "required": ["category", "description", "type"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["constraints"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "record_stated_preference",
        "description": (
            "Record something a traveller SAID about themselves that should outlast this "
            "trip - 'I hate queueing', 'I'm not a morning person'. It stores a signal and "
            "changes no profile and no trip: when enough evidence agrees across trips, a "
            "card asks the traveller, and only the traveller answers. On a group trip you "
            "must know who said it; if you cannot tell, ask - never guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "traveler_id": {
                    "type": "string",
                    "description": "Who said it. On a solo trip, the one traveller.",
                },
                "preference_key": {
                    "type": "string",
                    "enum": [
                        "avoid_early_mornings",
                        "relaxed_pace",
                        "packed_pace",
                        "parking_sensitive",
                        "dislikes_queueing",
                    ],
                },
                "quote": {
                    "type": "string",
                    "description": "Their words, verbatim - this is what 'Why?' will show them later.",
                },
            },
            "required": ["traveler_id", "preference_key", "quote"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "review_learned_preferences",
        "description": (
            "What the system has learned or is starting to suspect about each traveller, "
            "with the evidence behind it. Read-only: you may mention a proposable pattern "
            "once and point at the Travel DNA card; you can never accept, dismiss or apply "
            "one - only the traveller can."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "traveler_id": {
                    "type": "string",
                    "description": "Limit to one traveller. Omit for everyone.",
                },
            },
            "required": [],
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
        "name": "replace_item",
        "description": (
            "Swap one stop for the best alternative this trip already knows about, keeping "
            "its slot. Ranks candidates on how easy they are to arrive at - measured walk "
            "from the car park, and whether the day's forecast argues against being "
            "outdoors - so this is the tool for 'the parking there is bad, find somewhere "
            "easier'. Refuses rather than leaving a hole when nothing fits; search for "
            "somewhere new and try again."
        ),
        "parameters": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
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
                "proposal_id": {
                    "type": ["string", "null"],
                    "description": (
                        "From generate_itinerary or replan_day. Omit to commit only the "
                        "decisions and places gathered this turn."
                    ),
                },
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
            "required": ["reason"],
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
    # Airports found this turn, so flight ranking can use real drive times.
    airports: list = field(default_factory=list)
    # offer_ref -> option, so a chosen flight can be stored without the model
    # re-serializing it.
    flight_options: dict = field(default_factory=dict)
    # evidence_id -> EvidenceRecord discovered this turn, written alongside the
    # places it backs so an option's evidence_refs always resolve.
    pending_evidence: dict = field(default_factory=dict)
    # decision name -> serialized Decision built this turn, e.g. "hotel_area".
    # Held rather than written: every mutation goes through the patch engine.
    pending_decisions: dict = field(default_factory=dict)
    # traveler_id -> serialized TravelerPreferences resolved this turn.
    pending_traveler_prefs: dict = field(default_factory=dict)
    # OpenQuestions raised by blocking conflicts. Stored rather than derived,
    # because an answer has to survive the turn that gave it.
    pending_questions: list = field(default_factory=list)
    # Reads profiles so a snapshot can be made. Planning never reads them.
    profiles: Any = None
    # Records learning signals immediately - a signal is a fact about what
    # happened, not a proposal, so it takes no pending buffer (the _commit_now
    # lesson: staged work the model must remember to apply sometimes is not).
    learning: Any = None
    # decision name -> why it is now questionable, from a confirmed refresh.
    pending_stale: dict = field(default_factory=dict)
    # Brief facts learned this turn, as patch operations on /brief/...
    pending_brief_ops: list = field(default_factory=list)
    # Serialized ClarificationQuestions to ask the traveller.
    pending_clarifications: list = field(default_factory=list)
    # entity_id -> serialized ArrivalContext measured this turn.
    pending_arrival: dict = field(default_factory=dict)
    # Set only by the deterministic check in _update_trip_brief. The model never
    # gets to declare the brief ready; "confirmed" is the traveller's alone.
    pending_intake_status: str | None = None

    def budget_left(self) -> int:
        return max(0, self.settings.planning_search_budget - self.searches_used)


# Said by every tool that shortlists something. It is true because the runner
# flushes staged work at the end of the turn - before that it was a hope, and a
# live turn ran twelve tools, promised "the cards below", and left none.
SHORTLIST_SAVED = (
    "This shortlist is saved with the trip at the end of your turn - you do not need "
    "apply_trip_patch for it. Tell the traveller the cards are just below and let them pick."
)


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
        "note": (
            "Saved with the trip at the end of your turn; no apply_trip_patch needed."
            if stored
            else "Nothing was stored, because store was false."
        ),
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
    """State as the model believes it to be, including what this turn staged.

    Preferences resolved a moment ago have to be visible to conflict detection
    in the same turn, or the first `review_group_preferences` on a trip would
    report a group with no opinions.
    """
    staged_anything = (
        context.pending_entity_ops
        or context.pending_traveler_prefs
        or context.pending_brief_ops
        or context.pending_clarifications
        or context.pending_arrival
    )
    if not staged_anything:
        return context.state

    working = context.state.model_copy(deep=True)
    for entity in context.pending_entity_ops:
        working.entities[entity.entity_id] = entity
    for traveler in working.travelers:
        staged = context.pending_traveler_prefs.get(traveler.traveler_id)
        if staged is not None:
            traveler.preferences = TravelerPreferences.model_validate(staged)

    # Brief facts recorded a moment ago have to count toward "what is still
    # missing", or the tool would report a gap it had just been told how to fill.
    if context.pending_brief_ops:
        document = working.model_dump(mode="json")
        for operation in context.pending_brief_ops:
            jp.set_value(document, operation["path"], operation["value"])
        working = TripState.model_validate(document)
    for question in context.pending_clarifications:
        working.intake.questions.append(ClarificationQuestion.model_validate(question))
    for entity_id, arrival in context.pending_arrival.items():
        working.arrival[entity_id] = ArrivalContext.model_validate(arrival)
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
            f"times from these - verify with Google first. {SHORTLIST_SAVED}"
        ),
    }


def _slug(text: str, limit: int = 24) -> str:
    cleaned = "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
    return "_".join(cleaned)[:limit].strip("_")


def _shortlist_key(args: dict[str, Any]) -> str:
    """A stable name for one shortlist, so re-running a search updates it.

    Derived from what was asked rather than randomly generated: searching
    "izakaya" in "Asakusa, Tokyo" twice should refine one shortlist, not
    accumulate two that disagree.
    """
    parts = [_slug(args.get("query", "places")), _slug(args.get("near", ""))]
    return "_".join(part for part in parts if part) or "places"


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

    # The same places, seen through each traveller's food preferences. Ranking
    # happens once inside the pipeline; this only says who each result suits.
    working = _working_state(context)
    travelers, gaps = effective(working)

    # The confirmed-violation rule: an option a provider positively asserts
    # cannot suit somebody's diet is filtered out of a *new* shortlist, with the
    # removal named. Only confirmed_false - an unknown is never filtered, it is
    # raised as a question by the conflict layer. No current provider emits
    # confirmed_false; the branch exists so the tri-state means something.
    restricted = any(prefs.food.dietary_restrictions for prefs in travelers.values())
    if restricted:
        denied = [
            rec
            for rec in outcome.recommendations
            if (entity := outcome.entities.get(rec.entity_id)) is not None
            and entity.serves_vegetarian == "confirmed_false"
        ]
        if denied:
            names_dropped = ", ".join(outcome.entities[rec.entity_id].name for rec in denied)
            outcome.recommendations = [rec for rec in outcome.recommendations if rec not in denied]
            outcome.warnings.append(
                f"dropped {len(denied)} option(s) confirmed unsuitable for a stated dietary "
                f"restriction: {names_dropped}. Unverified places are kept and raised as "
                f"questions instead."
            )
    group_by_place: dict[str, Any] = {}
    if travelers and outcome.recommendations:
        group_by_place = {
            item.option.place_id: item
            for item in rank_places_for_group(
                [rec.ranked.place for rec in outcome.recommendations],
                travelers=travelers,
                names=traveler_names(working),
                worst_weight=context.settings.group_worst_weight,
            )
        }

    # Persist the ranking, not just the places.
    #
    # Until this existed, discovery stored the entities and the evidence and
    # threw the reasoning away at the end of the turn - so nothing downstream
    # could answer "why this one?" without inventing an answer, which is exactly
    # what INVARIANTS.md forbids. The shortlist goes in the *same* atomic batch
    # as the entities and evidence it points at, because referential integrity
    # requires both to resolve.
    if outcome.recommendations:
        context.pending_decisions[f"place_shortlists.{_shortlist_key(args)}"] = Decision[
            PlaceOption
        ](
            status="shortlisted",
            rationale=f"{args['query']} in {args['near']}",
            updated_at=utcnow(),
            options=[
                DecisionOption[PlaceOption](
                    data=PlaceOption(
                        entity_id=rec.entity_id,
                        purpose=args["query"],
                        why=(rec.ranked.pros[0] if rec.ranked.pros else None),
                    ),
                    status="shortlisted" if position < 3 else "candidate",
                    score=rec.ranked.score,
                    group_score=(
                        group_by_place[rec.ranked.place.place_id].group
                        if rec.ranked.place.place_id in group_by_place
                        else None
                    ),
                    pros=rec.ranked.pros,
                    cons=rec.ranked.cons,
                    evidence_refs=rec.evidence_ids,
                )
                for position, rec in enumerate(outcome.recommendations)
            ],
        ).model_dump(mode="json")

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
                **_group_view(group_by_place.get(rec.ranked.place.place_id)),
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
        "warnings": [*outcome.warnings, *gaps],
    }


# Flights and rooms are both priced per person, so a guessed headcount produces
# a plausible number for a trip nobody is taking. This is the one place party
# size is a hard precondition rather than a preference.
_PARTY_UNKNOWN = {
    "error": "this trip does not say how many people are travelling",
    "code": "invalid_request",
    "hint": (
        "Ask how many adults (and children) are going, save it with update_trip_brief, "
        "then search again. Do not assume one, and do not quote a fare you had to invent "
        "a passenger count for."
    ),
}


def _refuse_if_booked(
    context: ToolContext, name: str, args: dict[str, Any]
) -> dict[str, Any] | None:
    """Stop searching for something the traveller has already paid for.

    Checked before the provider call, not after: a booked trip should not be
    spending Duffel or SerpApi quota re-pricing a decision nobody is going to
    change. `booked` is a fact recorded by the traveller, so only the traveller
    can wave it aside - hence an explicit argument rather than a judgement call.
    """
    if args.get("override_booked"):
        return None
    decision = getattr(context.state.decisions, name, None)
    if decision is None or not decision.booked:
        return None
    reference = f" (reference {decision.booked_reference})" if decision.booked_reference else ""
    return {
        "already_booked": True,
        "message": (
            f"the {name} for this trip are already booked{reference}, so nothing was searched"
        ),
        "hint": (
            "Say so rather than offering alternatives. If the traveller explicitly wants to "
            "look anyway, call this again with override_booked: true."
        ),
    }


def _airport_pros(airport: AirportOption) -> list[str]:
    """Nothing. An airport's case is entirely in its figures.

    There used to be two bullets here and both made the card worse: the drive
    time, rounded to the minute, turned 21.5 and 21.8 into the same "22 min",
    and `serves {city}` is identical for every airport of one city - so ORD and
    MDW rendered as the same card twice. Both facts are metrics now, where they
    keep their precision and sit beside the distance that separates them. A
    figure shown as both a pill and a bullet is noise; shown only as a bullet,
    it was a lie about precision.
    """
    return []


def _airport_cons(airport: AirportOption) -> list[str]:
    if airport.ground_travel_source == "routes_api":
        return []
    return ["drive time was not measured, so this airport cannot be compared on access"]


def _commit_now(operations: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    """Write these immediately, through the one commit path.

    The stage-then-apply split exists so the model can revise a *proposal*
    before committing it. Intake has no such judgement to exercise: recording a
    fact the traveller stated, and asking them a question, are the work itself,
    not a suggestion about it.

    Two live runs proved the split was the wrong shape here. The agent recorded
    the answers, composed exactly the right questions, replied "已经记录", and
    never called apply_trip_patch - so nothing was saved and the workspace
    showed an empty screen beside a chat message listing three questions.
    Strengthening the prompt did not fix it the second time either.

    Nothing is weakened by this: `__patches__` goes to the same `_apply_all`,
    the same `repo.apply_patches`, the same gates, atomicity, revision bump and
    reload. The only thing that changes is that the model cannot forget.
    """
    return {
        "__patches__": [
            {"operations": operations, "scope": None, "reason": reason, "unlock_targets": []}
        ]
    }


_DATE_FIELDS = ("start_date", "end_date", "earliest_date", "latest_date")


def _brief_ops(state: TripState, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Only the fields actually supplied. Absence is not an instruction to clear."""
    operations: list[dict[str, Any]] = []

    # Dates get their year worked out here, against the clock, so a year the
    # model was unsure about is never a question the traveller has to answer.
    today = today_at(state)
    args = dict(args)
    for name in _DATE_FIELDS:
        raw = args.get(name)
        if isinstance(raw, str) and raw.strip():
            resolved = resolve_date(raw, today)
            args[name] = resolved.isoformat() if resolved else None

    simple: dict[str, str] = {
        "destination_city": "/brief/destination/city",
        "destination_region": "/brief/destination/region",
        "destination_country": "/brief/destination/country",
        "destination_flexible": "/brief/destination/flexible",
        "origin_city": "/brief/origin/city",
        "start_date": "/brief/dates/start",
        "end_date": "/brief/dates/end",
        "earliest_date": "/brief/dates/earliest",
        "latest_date": "/brief/dates/latest",
        "duration_nights": "/brief/dates/duration_nights",
        "adults": "/brief/party/adults",
        "children": "/brief/party/children",
        "rooms": "/brief/party/rooms",
        "budget_total_per_person": "/brief/budget/total_per_person",
        "budget_hotel_per_night": "/brief/budget/hotel_per_night",
        "pace": "/brief/pace",
        "notes": "/brief/notes",
    }
    for key, path in simple.items():
        if key in args and args[key] is not None:
            operations.append({"op": "set", "path": path, "value": args[key]})

    if args.get("origin_airport_codes"):
        operations.append(
            {
                "op": "set",
                "path": "/brief/origin/airport_codes",
                "value": args["origin_airport_codes"],
            }
        )
    if args.get("priorities"):
        operations.append({"op": "set", "path": "/brief/priorities", "value": args["priorities"]})

    for part, value in (args.get("scope") or {}).items():
        if part in SCOPE_LABELS and value:
            operations.append({"op": "set", "path": f"/brief/scope/{part}", "value": value})
    return operations


async def _update_trip_brief(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Write down what the traveller has told us about the trip.

    Until this existed the agent had no way to persist anything it learned - the
    brief was only ever editable from the web form - so an intake conversation
    could ask perfect questions and lose every answer at the end of the turn.
    """
    # A country is not a destination. "United States" was recorded as the
    # destination of a Maui trip, which leaves nothing for the hotel and area
    # services - both read brief.destination.city - to work with.
    named = args.get("destination_city") or args.get("destination_region")
    already = context.state.brief.destination
    if args.get("destination_country") and not (named or already.city or already.region):
        return {
            "error": "a country on its own is not a destination",
            "hint": (
                "Put the place they named in destination_city, or destination_region if it "
                "is wider than a city - Maui, the Amalfi Coast. Country goes alongside it, "
                "never instead of it. If they have not named anywhere, leave the "
                "destination open and set destination_flexible."
            ),
        }

    operations = _brief_ops(context.state, args)
    if not operations:
        return {"error": "nothing to record", "hint": "pass the facts the traveller gave you"}

    context.pending_brief_ops.extend(operations)

    # Whether the brief is complete enough to show is decided here, from the
    # state, not by the model announcing it - and by the same function the
    # confirm endpoint and the workspace button use, so all three agree.
    working = _working_state(context)
    if ready_to_confirm(working) and working.intake.status == "collecting":
        context.pending_intake_status = "awaiting_confirmation"

    # Both facts are already in the buffers, so committing the whole context
    # writes exactly these operations - and takes anything an earlier tool
    # staged along with them, which the blanket clear would otherwise discard.
    return _commit_staged(context, "record what the traveller said")


async def _ask_clarifications(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Ask for what is missing - only what is missing, and only once."""
    asked = args.get("questions") or []
    if not asked:
        return {"error": "no questions given"}

    # Nothing outstanding means nothing to ask. Enforced rather than asked for,
    # because a model with a question in mind will find a reason: the first live
    # run of a fully-specified trip still produced "which area is your hotel in?"
    # - useful, perhaps, but not something planning was waiting on.
    working = _working_state(context)
    gaps = missing_blocking(working)
    if not gaps:
        return {
            "asked": 0,
            "error": "nothing is blocking planning, so there is nothing to ask",
            "hint": (
                "Everything needed is already recorded. Show the traveller the summary and "
                "wait for them to confirm it. Anything else you are curious about can be "
                "asked later, once planning is under way."
            ),
        }

    # Every question has to name an outstanding requirement. Exact match on a
    # stable id, not a prefix test on a JSON pointer: a prefix test cannot tell
    # "/brief/party" from "/brief/party/adults" and is wrong in one direction or
    # the other. The live run of an undecided-destination trip asked for the
    # party size alongside two real gaps - reasonable-looking, and exactly the
    # interruption the traveller was promised they would not get.
    outstanding = {gap.requirement_id: gap for gap in gaps}
    warranted = [raw for raw in asked if raw.get("requirement_id") in outstanding]
    if not warranted:
        return {
            "asked": 0,
            "error": "every question must name an outstanding requirement_id",
            "outstanding": [
                {"requirement_id": gap.requirement_id, "why": gap.why} for gap in gaps
            ],
        }

    existing = {question.question for question in context.state.intake.questions}
    staged: list[dict[str, Any]] = []
    for raw in warranted[:3]:
        text = (raw.get("question") or "").strip()
        if not text or text in existing:
            continue
        question = ClarificationQuestion(
            question=text,
            kind=raw.get("kind", "text"),
            requirement_id=raw["requirement_id"],
            fills=outstanding[raw["requirement_id"]].field,
            choices=[
                ClarificationChoice(
                    value=choice.get("value", choice.get("label", "")),
                    label=choice.get("label", choice.get("value", "")),
                    description=choice.get("description"),
                )
                for choice in (raw.get("choices") or [])
            ],
        )
        existing.add(text)
        staged.append(question.model_dump(mode="json"))

    if not staged:
        return {
            "asked": 0,
            "note": "every one of those has already been asked; wait for the answers.",
        }

    context.pending_clarifications.extend(staged)
    return _commit_staged(context, f"ask {len(staged)} clarification(s)")


async def _get_weather_context(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Weather for the trip's days, each labelled with what kind of claim it is."""
    state = _working_state(context)
    days = state.itinerary.days
    if not days:
        return {"error": "this trip has no days yet"}

    anchor = next(
        (
            state.entities[item.entity_id]
            for _day, item in state.itinerary.iter_items()
            if item.entity_id in state.entities
        ),
        next(iter(state.entities.values()), None),
    )
    if anchor is None:
        return {"error": "this trip knows of no places yet, so there is nowhere to look up"}

    contexts = await context.toolbox.weather.context_for(
        [day.date for day in days],
        lat=anchor.lat,
        lng=anchor.lng,
        today=today_at(state),
    )

    for index, day in enumerate(days):
        found = contexts.get(day.date)
        if found is not None:
            context.pending_brief_ops.append(
                {
                    "op": "set",
                    "path": f"/itinerary/days/{index}/weather",
                    "value": found.model_dump(mode="json"),
                }
            )

    return {
        "days": [
            {
                "date": day.date.isoformat(),
                "kind": contexts[day.date].kind,
                "summary": contexts[day.date].headline(),
                "method": (
                    contexts[day.date].norm.describe() if contexts[day.date].norm else None
                ),
            }
            for day in days
            if day.date in contexts
        ],
        "rule": (
            "A 'forecast' is about that date and you may plan around it. A "
            "'historical_norm' is about the season - say what is typical and suggest a "
            "backup, but never tell the traveller what the weather will do on a date."
        ),
        "note": "Saved with the trip. Nothing else to do.",
    }


async def _search_airports(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if context.toolbox.airports is None:
        return {"error": "airport search is not configured"}

    result = await context.toolbox.airports.search_airports(
        SearchAirportsInput(
            location=args["location"],
            max_ground_travel_minutes=args.get("max_ground_travel_minutes"),
            limit=min(int(args.get("limit", 6)), 10),
        )
    )
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}

    context.airports = list(result.results)

    # Which airport you fly out of is a decision in its own right - people change
    # it without changing the flight ("we'd rather drive to SFO") - so it is kept
    # as one rather than dissolving into a scoring dimension inside the fare
    # search. Without a role we cannot say which end it belongs to, so we keep
    # nothing rather than filing it under a guess.
    role = args.get("role")
    if role in ("departure", "arrival") and result.results:
        context.pending_decisions[f"{role}_airport"] = Decision[AirportOption](
            status="shortlisted",
            rationale=f"airports near {args['location']}",
            updated_at=utcnow(),
            options=[
                DecisionOption[AirportOption](
                    data=airport,
                    status="shortlisted" if position < 3 else "candidate",
                    pros=_airport_pros(airport),
                    cons=_airport_cons(airport),
                )
                for position, airport in enumerate(result.results)
            ],
        ).model_dump(mode="json")

    return {
        "airports": [
            {
                "iata": airport.iata,
                "name": airport.name,
                "city": airport.city,
                "distance_km": airport.distance_km,
                "drive_minutes": airport.ground_travel_minutes,
                "drive_source": airport.ground_travel_source,
            }
            for airport in result.results
        ],
        "warnings": result.warnings,
        "note": f"Drive times come from the Routes API. Never estimate one yourself. {SHORTLIST_SAVED}",
    }


async def _search_flights(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if context.toolbox.flights is None:
        return {
            "error": "flight search is not configured (no DUFFEL_ACCESS_TOKEN)",
            "hint": "say that flights cannot be searched rather than guessing at fares",
        }

    booked = _refuse_if_booked(context, "flights", args)
    if booked is not None:
        return booked

    # A fare is quoted per passenger, so an assumed passenger count produces a
    # real-looking price for a trip nobody is taking. Ask instead.
    adults = args.get("adults", context.state.brief.party.adults)
    if adults is None:
        return _PARTY_UNKNOWN

    try:
        spec = SearchFlightsInput(
            origins=args["origins"],
            destinations=args["destinations"],
            departure_date=date_type.fromisoformat(args["departure_date"]),
            return_date=(
                date_type.fromisoformat(args["return_date"]) if args.get("return_date") else None
            ),
            adults=adults,
            children=int(args.get("children", context.state.brief.party.children or 0)),
            cabin=args.get("cabin", "economy"),
            max_stops=args.get("max_stops"),
            max_price_per_person=args.get("max_price_per_person"),
        )
    except (KeyError, ValueError) as exc:
        return {"error": f"invalid flight search: {exc}"}

    result = await context.toolbox.flights.search_flights(spec)
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}
    if result.found_nothing:
        # Record the empty result rather than returning quietly. The trip used
        # to keep nothing at all from a search that found nothing, so "no
        # airline flies this route on these dates" and "nobody has looked yet"
        # were the same state - absence read as never-asked. The decision is
        # staged with no options, which is exactly what was true.
        searched = (
            f"{'/'.join(spec.origins)} to {'/'.join(spec.destinations)} "
            f"on {spec.departure_date.isoformat()}"
        )
        context.pending_decisions["flights"] = Decision[FlightOptionData](
            status="researching",
            rationale=f"searched {searched}; no airline offered it",
            updated_at=utcnow(),
            options=[],
        ).model_dump(mode="json")
        return {
            "offers": [],
            "note": (
                f"No airline offered {searched}. The search itself worked, and that is "
                "now recorded on the trip. Do not stop here: offer the traveller the "
                "choices with propose_next_step - other airports, other dates, or "
                "leaving flights aside for now."
            ),
            "warnings": result.warnings,
        }

    # Two scorings, answering two questions. `rank_flights` says what is good
    # about the flight itself - price, stops, duration are facts about the
    # aircraft, not opinions. The group pass says who it is good *for*.
    #
    # The brief-level red-eye answer feeds the base ranking. It used to be a
    # blank FlightPreferences() here, which made the intake quick-pick a stated
    # preference that moved nothing - the ledger's oldest failure shape.
    working = _working_state(context)
    ranked = rank_flights(
        result.results,
        preferences=FlightPreferences(avoid_red_eye=bool(working.brief.avoid_red_eye)),
        airports=context.airports,
        limit=6,
    )
    travelers, gaps = effective(working)
    names = traveler_names(working)

    group_by_ref: dict[str, Any] = {}
    group_warnings: list[str] = list(gaps)
    if travelers:
        grouped, airline_warnings = rank_flights_for_group(
            result.results,
            travelers=travelers,
            names=names,
            airports=context.airports,
            worst_weight=context.settings.group_worst_weight,
            trip_avoid_red_eye=bool(working.brief.avoid_red_eye),
        )
        group_warnings.extend(airline_warnings)
        group_by_ref = {item.option.offer_ref: item for item in grouped}
        # The group's order wins where there is a group to have one.
        ranked.sort(
            key=lambda item: (
                -group_ranking_value(group_by_ref[item.option.offer_ref].group)
                if item.option.offer_ref in group_by_ref
                else 0.0,
                item.option.offer_ref,
            )
        )

    profile = context.state.travelers[0] if context.state.travelers else None
    context.flight_options = {item.option.offer_ref: item.option for item in ranked}

    trade_off = None
    cheapest = cheapest_of(ranked)
    if ranked and cheapest and cheapest.option.offer_ref != ranked[0].option.offer_ref:
        trade_off = explain_choice(ranked[0], cheapest, airports=context.airports)

    sandbox = [item for item in ranked if not item.option.live_mode]

    # Persist the ranking, not just the fares.
    #
    # Until this existed, `context.flight_options` was the only record of a
    # flight search and it died with the turn - so the trip could carry a chosen
    # flight with nothing behind it, and "why this one?" had no stored answer.
    # The decision slot and its audit event have existed since M5; nothing ever
    # wrote them.
    if ranked:
        context.pending_decisions["flights"] = Decision[FlightOptionData](
            status="shortlisted",
            rationale=(
                f"{'/'.join(spec.origins)} to {'/'.join(spec.destinations)} "
                f"on {spec.departure_date.isoformat()}"
            ),
            updated_at=utcnow(),
            options=[
                DecisionOption[FlightOptionData](
                    data=item.option,
                    status="shortlisted" if position < 3 else "candidate",
                    score=item.score,
                    group_score=(
                        group_by_ref[item.option.offer_ref].group
                        if item.option.offer_ref in group_by_ref
                        else None
                    ),
                    pros=item.pros,
                    cons=item.cons,
                )
                for position, item in enumerate(ranked)
            ],
        ).model_dump(mode="json")

    payload: dict[str, Any] = {
        "offers": [
            {
                "offer_ref": item.option.offer_ref,
                "live_mode": item.option.live_mode,
                "origin": item.option.origin,
                "destination": item.option.destination,
                "price_per_person": (item.option.price_per_person or item.option.price).amount,
                "currency": item.currency,
                "stops": item.option.stops,
                "duration_minutes": item.option.duration_minutes,
                "airlines": item.option.airlines,
                "departure_at": item.option.departure_at.isoformat()
                if item.option.departure_at
                else None,
                "score": item.score.total,
                "dimensions": item.score.dimensions,
                "pros": item.pros,
                "cons": item.cons,
                "search_url": item.option.search_url,
                **_group_view(group_by_ref.get(item.option.offer_ref)),
            }
            for item in ranked
        ],
        "warnings": [*result.warnings, *group_warnings],
        "profile_used": profile.name if profile else None,
        "note": SHORTLIST_SAVED if ranked else "no flights matched; the search itself worked",
    }

    if trade_off is not None:
        payload["trade_off"] = {
            "recommended": trade_off.recommended_ref,
            "cheapest": trade_off.alternative_ref,
            "statements": trade_off.statements,
            "note": (
                "Every figure here came from the provider or the Routes API. Use these "
                "words; do not compute your own."
            ),
        }

    if sandbox:
        payload["live_mode"] = False
        payload["disclaimer"] = SANDBOX_DISCLAIMER

    return payload


async def _recommend_hotel_areas(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    working = _working_state(context)

    result = await context.toolbox.hotel_areas.recommend_areas(
        working,
        suggested_areas=args.get("suggested_areas") or None,
        mode=args.get("mode", "transit"),
        limit=min(int(args.get("limit", 4)), 6),
    )
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}
    if result.found_nothing:
        return {"areas": [], "warnings": result.warnings, "note": "no candidate area was found"}

    select_top = bool(args.get("select_top"))
    decision = build_area_decision(result.results, select_best=select_top)
    context.pending_decisions["hotel_area"] = decision.model_dump(mode="json")

    return {
        "areas": [
            {
                "area_name": area.candidate.area_name,
                "origin": area.candidate.origin,
                "mode": area.mode,
                "mean_minutes_to_anchors": area.mean_minutes,
                "worst_minutes_to_anchors": area.worst_minutes,
                "anchors_reachable": f"{area.reachable}/{area.anchor_count}",
                "score": area.score.total,
                "dimensions": area.score.dimensions,
                "pros": area.pros,
                "cons": area.cons,
                "sources": area.signal.source_urls if area.signal else [],
            }
            for area in result.results
        ],
        "selected": decision.selected_option_id is not None,
        "warnings": result.warnings,
        "note": (
            "Travel minutes came from the Routes API. Present the ranking with those figures "
            f"and let the traveller choose. {SHORTLIST_SAVED} "
            "Only after an area is chosen does search_hotels make sense."
        ),
    }


async def _search_hotels(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if context.toolbox.hotels is None:
        return {
            "error": "hotel search is not configured (no SERPAPI_API_KEY)",
            "hint": (
                "recommend_hotel_areas still works - it needs only Google. Say that hotel "
                "prices cannot be searched rather than guessing at them."
            ),
        }

    booked = _refuse_if_booked(context, "hotel", args)
    if booked is not None:
        return booked

    working = _working_state(context)
    party = working.brief.party

    # Same reason as flights: room rates are quoted for a number of guests.
    adults = args.get("adults", party.adults)
    if adults is None:
        return _PARTY_UNKNOWN

    try:
        spec = SearchHotelsInput(
            city=working.brief.destination.city,
            area_name=args.get("area_name"),
            check_in=date_type.fromisoformat(args["check_in"]),
            check_out=date_type.fromisoformat(args["check_out"]),
            adults=int(adults),
            children=int(party.children or 0),
            rooms=int(args.get("rooms", party.rooms or 1)),
            max_nightly_price=args.get("max_nightly_price") or working.brief.budget.hotel_per_night,
            min_rating=args.get("min_rating"),
            min_star_category=args.get("min_star_category"),
            currency=working.brief.budget.currency,
            bypass_area_decision=bool(args.get("bypass_area_decision")),
            bypass_reason=args.get("bypass_reason"),
        )
    except (KeyError, ValueError) as exc:
        return {"error": f"invalid hotel search: {exc}"}

    result = await context.toolbox.hotels.search_hotels(spec, state=working)
    if not result.ok:
        return {"error": result.error.message, "code": result.error.code}
    if result.found_nothing:
        # Same as the flight search: an empty result is a finding, and the trip
        # keeps it. Without this, "nothing available in that area on those
        # dates" was indistinguishable from "nobody has looked".
        context.pending_decisions["hotel"] = Decision[HotelOptionData](
            status="researching",
            rationale=f"searched {spec.area_name or 'the chosen area'}; nothing was available",
            updated_at=utcnow(),
            options=[],
        ).model_dump(mode="json")
        return {
            "hotels": [],
            "note": (
                "Nothing was available, and that is now recorded on the trip. The search "
                "itself worked. Do not stop here: offer the choices with propose_next_step "
                "- another neighbourhood, other dates, or leaving the hotel aside for now."
            ),
            "warnings": result.warnings,
        }

    shortlist = await context.toolbox.hotels.shortlist(
        result.results, state=working, spec=spec, preferences=HotelPreferences(), size=5
    )
    context.pending_entity_ops.extend(shortlist.entities)

    travelers, gaps = effective(working)
    group_by_ref: dict[str, Any] = {}
    if travelers and shortlist.ranked:
        grouped = rank_hotels_for_group(
            [item.option for item in shortlist.ranked],
            travelers=travelers,
            names=traveler_names(working),
            worst_weight=context.settings.group_worst_weight,
        )
        group_by_ref = {(item.option.offer_ref or item.option.name): item for item in grouped}
        shortlist.ranked.sort(
            key=lambda item: (
                -group_ranking_value(group_by_ref[item.option.offer_ref or item.option.name].group),
                item.option.name,
            )
        )

    decision = build_hotel_decision(shortlist.ranked)
    context.pending_decisions["hotel"] = decision.model_dump(mode="json")

    payload: dict[str, Any] = {
        "hotels": [
            {
                "name": item.option.name,
                "entity_id": item.option.entity_id,
                "area_name": item.option.area_name,
                "live_mode": item.option.live_mode,
                "nightly_price": item.nightly,
                "currency": item.currency,
                # Two lists, never one number. A star category and a guest
                # rating are different claims and are rendered as such.
                "ratings": describe_ratings(item.option),
                "prices_by_site": describe_prices(item.option),
                "mean_minutes_to_anchors": item.option.mean_route_minutes(),
                "score": item.score.total,
                "dimensions": item.score.dimensions,
                "pros": item.pros,
                "cons": item.cons,
                "search_url": item.option.search_url,
                **_group_view(group_by_ref.get(item.option.offer_ref or item.option.name)),
            }
            for item in shortlist.ranked
        ],
        "warnings": [*result.warnings, *shortlist.warnings, *gaps],
        "note": (
            "Ratings are listed separately on purpose. Never average them, and never compare "
            "a star category against a guest score - they measure different things. Quote a "
            f"price with the site that offered it. {SHORTLIST_SAVED}"
        ),
    }

    if shortlist.ranked and len(shortlist.ranked) > 1:
        trade_off = explain_hotel_choice(shortlist.ranked[0], shortlist.ranked[1])
        payload["trade_off"] = {
            "recommended": trade_off.recommended_ref,
            "alternative": trade_off.alternative_ref,
            "statements": trade_off.statements,
            "close_call": trade_off.close_call,
            "note": (
                "Nothing measured meaningfully separates these two. Say so rather than "
                "presenting the first as a winner."
                if trade_off.close_call
                else "Every figure here came from a tool. Use these words."
            ),
        }

    if not context.toolbox.hotels.live_mode:
        payload["live_mode"] = False
        payload["disclaimer"] = HOTEL_SANDBOX_DISCLAIMER

    return payload


def _group_view(ranked: Any) -> dict[str, Any]:
    """A group score as the model sees it.

    `describe()` rather than the bare total, and the per-traveller scores
    alongside it. A number the model can quote without its split is a number
    that will be quoted without its split.
    """
    if ranked is None:
        return {}
    group = ranked.group
    return {
        # The verdict sentence first, and the raw number only alongside the two
        # things that qualify it. A bare `group_score` invited exactly the
        # quotation this milestone forbids.
        "group_verdict": group.describe(),
        "group_score": group.total,
        "group_score_evidence_coverage": group.coverage,
        "group_worst_weight": group.worst_weight,
        "per_traveler": {group.name_of(tid): value for tid, value in group.per_traveler.items()},
        "group_is_split": group.is_split,
        "worst_served": group.name_of(group.worst_traveler_id),
        "group_cons": ranked.cons,
    }


async def _load_profiles(context: ToolContext) -> tuple[dict[str, Any], list[str]]:
    """Stored profiles for the travellers who link to one."""
    profiles: dict[str, Any] = {}
    problems: list[str] = []
    if context.profiles is None:
        return profiles, ["profiles are not available in this context"]

    from app.db.repository import ProfileNotFound

    for traveler in context.state.travelers:
        if not traveler.profile_id:
            continue
        try:
            profiles[traveler.traveler_id] = await context.profiles.get(traveler.profile_id)
        except ProfileNotFound:
            problems.append(
                f"{traveler.name} links to profile {traveler.profile_id!r}, which no longer exists"
            )
    return profiles, problems


async def _review_group_preferences(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    state = context.state

    if len(state.travelers) < 2:
        # A group of one has nobody to disagree with. Answering immediately
        # keeps this milestone invisible on a solo trip rather than spending a
        # planning round on it - which, measured live, is enough to derail one.
        only = state.travelers[0].name if state.travelers else None
        return {
            "travelers": [{"name": only}] if only else [],
            "conflicts": [],
            "blocking_count": 0,
            "note": (
                f"{only} is travelling alone, so there is nothing to reconcile. "
                if only
                else "This trip has no travellers listed. "
            )
            + "Get on with planning; do not call this again for this trip.",
        }

    profiles, problems = await _load_profiles(context)

    resolved_now: list[str] = []
    if args.get("resolve_missing", True):
        for traveler in state.travelers:
            if traveler.preferences is not None or traveler.traveler_id not in profiles:
                continue
            preferences = resolve(traveler, profiles[traveler.traveler_id])
            context.pending_traveler_prefs[traveler.traveler_id] = preferences.model_dump(
                mode="json"
            )
            resolved_now.append(traveler.name)

    # Conflicts are computed against what the trip *will* hold once this turn's
    # patch lands, so a first resolve does not report an empty group.
    working = _working_state(context)
    conflicts = detect_conflicts(working)
    _, gaps = effective(working)

    if args.get("raise_questions", True):
        known = {question.question for question in state.open_questions}
        for conflict in conflicts:
            if not conflict.blocking:
                continue
            text = f"[{conflict.conflict_id}] {conflict.question()}"
            if text not in known:
                context.pending_questions.append(
                    OpenQuestion(question=text, blocking=True, asked_at=utcnow()).model_dump(
                        mode="json"
                    )
                )

    return {
        "travelers": [
            {
                "traveler_id": traveler.traveler_id,
                "name": traveler.name,
                "profile_id": traveler.profile_id,
                "resolved": (
                    traveler.preferences is not None
                    or traveler.traveler_id in context.pending_traveler_prefs
                ),
                "overridden_for_this_trip": (
                    traveler.preferences.overridden_paths if traveler.preferences else []
                ),
            }
            for traveler in state.travelers
        ],
        "resolved_this_turn": resolved_now,
        "conflicts": [
            {
                "conflict_id": conflict.conflict_id,
                "kind": conflict.kind,
                "severity": conflict.severity,
                "summary": conflict.summary,
                # Per person. Do not average these.
                "positions": conflict.positions,
                "affects": conflict.affects[:8],
                "resolution_options": conflict.resolution_options,
            }
            for conflict in conflicts
        ],
        "blocking_count": sum(1 for conflict in conflicts if conflict.blocking),
        "warnings": problems + gaps,
        "note": (
            "Name the people who disagree and what each of them wants. Never present a "
            "group average as if it were everyone's view, and never say a trip is ready "
            "while a blocking conflict is unanswered. Call apply_trip_patch to store this."
        ),
    }


async def _refresh_traveler_preferences(
    context: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    state = context.state
    profiles, problems = await _load_profiles(context)
    wanted = set(args.get("traveler_ids") or [])

    diffs = [
        diff_profile(traveler, profiles[traveler.traveler_id])
        for traveler in state.travelers
        if traveler.traveler_id in profiles and (not wanted or traveler.traveler_id in wanted)
    ]
    moving = [diff for diff in diffs if diff.has_effect]

    payload: dict[str, Any] = {
        "diffs": [
            {
                "traveler_id": diff.traveler_id,
                "traveler_name": diff.traveler_name,
                "summary": diff.describe(),
                "changes": [
                    {"path": change.path, "before": change.before, "after": change.after}
                    for change in diff.changes
                ],
                "not_refreshed": diff.not_refreshed,
                "affects": diff.affects,
            }
            for diff in diffs
        ],
        "warnings": problems,
    }

    if not moving:
        payload["applied"] = False
        payload["note"] = "nothing in anyone's profile would change this trip"
        return payload

    if not args.get("confirm"):
        payload["applied"] = False
        payload["note"] = (
            "Nothing has changed. Show these differences to the user, say which decisions "
            "they would make stale, and call again with confirm=true only if they agree."
        )
        return payload

    for diff in moving:
        traveler = next(t for t in state.travelers if t.traveler_id == diff.traveler_id)
        context.pending_traveler_prefs[diff.traveler_id] = resolve(
            traveler, profiles[diff.traveler_id]
        ).model_dump(mode="json")

    decisions, days = stale_targets(state, moving)
    context.pending_stale = {
        name: (
            f"resolved preferences changed for "
            f"{', '.join(sorted({d.traveler_name for d in moving}))}"
        )
        for name in decisions
    }

    payload["applied"] = True
    payload["stale_decisions"] = decisions
    payload["stale_days"] = [day.isoformat() for day in days]
    payload["note"] = (
        "Marked stale, not rebuilt. Tell the user which decisions are now questionable and "
        "let them choose whether to redo any of them."
    )
    return payload


async def _propose_next_step(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Turn a fork into buttons. Committed at once, because it must be seen.

    Every choice is checked against what the trip actually holds: a proposal
    offering an option that does not exist would render a button that cannot
    be pressed, which is the failure this tool exists to end wearing different
    clothes.
    """
    question = (args.get("question") or "").strip()
    if not question:
        return {"error": "no question given"}

    raw_choices = args.get("choices") or []
    if len(raw_choices) < 2:
        return {
            "error": "a proposal needs at least two choices",
            "hint": (
                "One of them should let the traveller leave this for now - set_aside for "
                "flights or lodging, or none to simply carry on."
            ),
        }

    if any(not p.answered and p.question == question for p in context.state.proposals):
        return {"asked": 0, "note": "that is already waiting for them; do not ask it twice"}

    choices: list[ProposalChoice] = []
    for raw in raw_choices:
        choice = ProposalChoice(
            label=(raw.get("label") or "").strip(),
            action=raw.get("action", "none"),
            decision=raw.get("decision"),
            option_id=raw.get("option_id"),
            part=raw.get("part"),
            note=raw.get("note"),
        )
        if not choice.label:
            return {"error": "every choice needs a label"}

        if choice.action == "select_option":
            decision = dict(context.state.decisions.iter_decisions()).get(choice.decision or "")
            if decision is None:
                return {
                    "error": f"no decision {choice.decision!r} on this trip",
                    "decisions": [name for name, _ in context.state.decisions.iter_decisions()],
                }
            known = {option.option_id: label_for(option.data) for option in decision.options}
            if choice.option_id not in known:
                return {
                    "error": (
                        f"decision {choice.decision!r} has no option {choice.option_id!r}; "
                        "propose one that already exists rather than inventing it"
                    ),
                    "options": known,
                }
        elif choice.action in ("set_aside", "resume") and choice.part not in ("flights", "lodging"):
            return {"error": f"{choice.action} needs part 'flights' or 'lodging'"}

        choices.append(choice)

    proposal = AgentProposal(
        question=question, detail=args.get("detail"), choices=choices
    )
    context.pending_brief_ops.append(
        {"op": "add", "path": "/proposals/-", "value": proposal.model_dump(mode="json")}
    )
    payload = _commit_staged(context, f"ask: {question[:60]}")
    payload["note"] = (
        "Asked. The card is directly below your reply with a button per choice - say what "
        "the trade-off is in a sentence and let them press one. Do not also ask them to "
        "type an answer."
    )
    return payload


async def _record_constraints(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Store what the traveller requires, derived from what they said.

    Committed as it goes, like the intake tools: a requirement someone stated
    is a fact about the trip, not a proposal about it. Nothing here touches
    `brief.notes` - the words are the record and the constraints are a reading
    of them, so a misreading can always be corrected from the original. That is
    the same rule that keeps Google's facts and people's opinions in different
    places.

    Nothing could create a constraint before this: the model could see them in
    its state summary and the prompt could call them non-negotiable, but there
    was no tool and no endpoint that made one.
    """
    asked = args.get("constraints") or []
    if not asked:
        return {"error": "no constraints given"}

    existing = {c.description.strip().lower() for c in context.state.constraints}
    staged: list[dict[str, Any]] = []
    for raw in asked:
        description = (raw.get("description") or "").strip()
        if not description or description.lower() in existing:
            continue
        traveler_id = raw.get("traveler_id")
        known = {t.traveler_id for t in context.state.travelers}
        if traveler_id and traveler_id not in known:
            return {
                "error": f"no traveler {traveler_id!r} on this trip",
                "travelers": sorted(known),
            }
        existing.add(description.lower())
        staged.append(
            TripConstraint(
                category=raw.get("category", "other"),
                description=description,
                type=raw.get("type", "soft"),
                scope="traveler" if traveler_id else "trip",
                traveler_id=traveler_id,
                # They said it; nobody inferred it.
                source="user_explicit",
            ).model_dump(mode="json")
        )

    if not staged:
        return {"recorded": 0, "note": "every one of those is already recorded"}

    context.pending_brief_ops.extend(
        {"op": "add", "path": "/constraints/-", "value": constraint} for constraint in staged
    )
    return _commit_staged(context, f"record {len(staged)} constraint(s)")


_NEVER_APPLY_NOTE = (
    "Recorded. If this becomes a proposable pattern, the Travel DNA card asks the "
    "traveller; only the traveller answers - never apply it yourself."
)


async def _record_stated_preference(
    context: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Store one stated preference as a learning signal. Nothing else moves.

    The signal is committed immediately rather than staged: it is a fact about
    what was said, not a proposal awaiting apply_trip_patch - and staged work
    the model must remember to apply sometimes is not (the intake lesson).
    """
    traveler_id = args.get("traveler_id", "")
    traveler = next(
        (t for t in context.state.travelers if t.traveler_id == traveler_id), None
    )
    if traveler is None:
        known = [
            {"traveler_id": t.traveler_id, "name": t.name} for t in context.state.travelers
        ]
        return {
            "error": f"no traveler {traveler_id!r} on this trip",
            "travelers": known,
            "note": "Attribution is the whole point: name the person who said it, or ask.",
        }
    if not traveler.profile_id:
        return {
            "recorded": False,
            "note": (
                f"{traveler.name} has no stored profile, so nothing durable can be "
                "recorded. Say so plainly and carry on - you cannot create one, and "
                "offering to would be promising something you cannot do. A profile is "
                "chosen when a trip is created."
            ),
        }
    if context.learning is None:
        return {"error": "learning storage is not available in this run"}

    key = args.get("preference_key")
    if key not in CATALOGUE:
        return {"error": f"unknown preference_key {key!r}", "known": sorted(CATALOGUE)}

    signal = LearningSignal(
        profile_id=traveler.profile_id,
        trip_id=context.state.trip_id,
        preference_key=key,
        strength="strong",  # said in words, not inferred from a click
        source="stated",
        context={
            "quote": args.get("quote", ""),
            "trip_title": context.state.metadata.title,
        },
    )
    await context.learning.record(signal)

    profile = None
    if context.profiles is not None:
        try:
            profile = await context.profiles.get(traveler.profile_id)
        except Exception:  # noqa: BLE001 - a dangling id degrades, not crashes
            profile = None

    pattern: dict[str, Any] | None = None
    if profile is not None:
        signals = await context.learning.list_for_profile(traveler.profile_id)
        hypotheses = derive_hypotheses(profile, signals, settings=context.settings)
        match = next((h for h in hypotheses if h.preference_key == key), None)
        if match is not None:
            pattern = {
                "hypothesis_id": match.hypothesis_id,
                "strength": match.strength,
                "confidence": match.confidence,
                "status": match.status,
                "trips": len(match.trip_ids),
            }

    return {
        "recorded": {
            "traveler": traveler.name,
            "preference_key": key,
            "quote": signal.context["quote"],
        },
        "pattern": pattern,
        "note": _NEVER_APPLY_NOTE,
    }


async def _review_learned_preferences(
    context: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    """What the evidence currently says about each traveller. Read-only."""
    if context.learning is None or context.profiles is None:
        return {"error": "learning storage is not available in this run"}

    wanted = args.get("traveler_id")
    travelers = [
        t
        for t in context.state.travelers
        if t.profile_id and (wanted is None or t.traveler_id == wanted)
    ]
    if wanted and not travelers:
        return {"error": f"no traveler {wanted!r} with a stored profile on this trip"}

    report: list[dict[str, Any]] = []
    for traveler in travelers:
        try:
            profile = await context.profiles.get(traveler.profile_id)
        except Exception:  # noqa: BLE001
            report.append({"traveler": traveler.name, "error": "profile not found"})
            continue
        signals = await context.learning.list_for_profile(traveler.profile_id)
        hypotheses = derive_hypotheses(profile, signals, settings=context.settings)
        report.append(
            {
                "traveler": traveler.name,
                "traveler_id": traveler.traveler_id,
                "learned": {
                    path: {"summary": rec.summary, "accepted_at": rec.accepted_at.isoformat()}
                    for path, rec in profile.learned.items()
                },
                "patterns": [
                    {
                        "hypothesis_id": h.hypothesis_id,
                        "preference_key": h.preference_key,
                        "status": h.status,
                        "strength": h.strength,
                        "confidence": h.confidence,
                        "evidence": [e.line for e in h.evidence],
                    }
                    for h in hypotheses
                ],
                "dismissed_count": sum(1 for h in hypotheses if h.status == "dismissed"),
            }
        )

    return {"travelers": report, "note": _NEVER_APPLY_NOTE}


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

    # Each pair is fetched in the mode the validator will look it up under.
    # Fetching one mode and reading another is not a near miss - it is a
    # guaranteed miss, and it made every leg over 1.5 km permanently unknown.
    state = _working_state(context)
    by_mode: dict[str, list[tuple[str, str]]] = {}
    for day in proposal.days:
        scheduled = [item.entity_id for item in day.items if item.entity_id in known]
        for origin, destination in zip(scheduled, scheduled[1:], strict=False):
            if origin == destination:
                continue
            mode = mode_between(state, known.get(origin), known.get(destination))
            if (origin, destination, mode) in context.travel.minutes:
                continue
            by_mode.setdefault(mode, []).append((origin, destination))

    measured = 0
    for mode, pairs in by_mode.items():
        origins = sorted({origin for origin, _ in pairs})
        destinations = sorted({destination for _, destination in pairs})
        result = await context.toolbox.routes.get_routes(
            GetRoutesInput(
                origins=[LocationRef(entity_id=eid) for eid in origins],
                destinations=[LocationRef(entity_id=eid) for eid in destinations],
                mode=mode,
            ),
            entities=known,
        )
        if not result.ok:
            continue
        for leg in result.results:
            if leg.status != "ok" or leg.duration_seconds is None:
                continue
            key = (origins[leg.origin_index], destinations[leg.destination_index], mode)
            context.travel.minutes[key] = leg.duration_seconds / 60.0
            if leg.distance_meters is not None:
                context.travel.meters[key] = float(leg.distance_meters)
        measured += len(result.results)
    return measured


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


# Enough to choose between, without spending a Places search and a Routes call
# on every place the trip has ever heard of.
MAX_PARKING_LOOKUPS = 5


async def _ensure_parking(context: ToolContext, candidates: list) -> int:
    """Measure parking for the places we are about to rank.

    Without this the ranking has parking data for scheduled stops only, so it
    could avoid a known-bad option but never choose a known-good one - and a
    swap traded a measured sixteen-minute walk for an unmeasured one, which is
    not "easier", only less known.
    """
    unknown = [
        entity
        for entity in candidates
        if entity.entity_id not in context.state.arrival
        and entity.entity_id not in context.pending_arrival
    ][:MAX_PARKING_LOOKUPS]
    if not unknown or context.toolbox is None:
        return 0

    mode = str(long_haul_mode(context.state))
    for entity in unknown:
        found = await context.toolbox.parking.context_for(context.state, entity, mode=mode)
        context.pending_arrival[entity.entity_id] = found.model_dump(mode="json")
    return len(unknown)


async def _replace_item(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Swap one stop for the best alternative the trip already knows.

    Exposed because the agent could not do it. `substitute_item` has existed
    since the P0 pass, ranks on parking and the day's forecast, and was reachable
    only from a button - so "replace this, the parking is bad" could be served
    only by `replan_day` dropping the stop and leaving a hole. Removing
    something is not replacing it.
    """
    item_id = args.get("item_id")
    if not item_id:
        return {"error": "item_id is required"}

    working = _working_state(context)
    day = next((d for d, i in working.itinerary.iter_items() if i.item_id == item_id), None)
    if day is not None:
        await _ensure_hours(context, [i.entity_id for i in day.items if i.entity_id])

    await _ensure_parking(context, substitution_candidates(_working_state(context), item_id))
    proposal = substitute_item(_working_state(context), item_id, travel=context.travel)
    if proposal.is_empty:
        return {
            "replaced": False,
            "reason": proposal.summary,
            "hint": (
                "Nothing the trip already knows fits that slot. Search for somewhere new "
                "first, or say plainly that there is no alternative rather than dropping "
                "the stop and leaving a hole."
            ),
        }

    if await _ensure_routes(context, proposal):
        proposal = substitute_item(_working_state(context), item_id, travel=context.travel)

    _record_substitution(context, proposal)
    context.proposals.put(proposal)
    return _proposal_view(proposal)


def _record_substitution(context: ToolContext, proposal: ItineraryProposal) -> None:
    """Keep why the swap was made, so `Why?` can answer later.

    The reasoning is computed either way - it is in the proposal summary. Not
    storing it meant a stop chosen for a measured two-minute walk over a
    sixteen-minute one reported "no stored decision recommended this place",
    which is the provenance gap this project keeps finding in new places.
    """
    working = _working_state(context)
    # The replacement is the item on the proposed day that is not on the stored
    # one. Comparing ids is exact; comparing titles is not.
    stored_ids = {item.item_id for _d, item in context.state.itinerary.iter_items()}
    chosen = next(
        (
            item
            for day in proposal.days
            for item in day.items
            if item.item_id not in stored_ids and item.entity_id
        ),
        None,
    )
    if chosen is None:
        return

    arrival = working.arrival.get(chosen.entity_id)
    pros: list[str] = []
    cons: list[str] = []
    if arrival is not None and arrival.parking.is_known:
        walk = arrival.overhead_minutes
        pros.append(
            f"{walk:.0f} min on foot from the car park" if walk is not None
            else "parking is known here"
        )
    else:
        cons.append("parking here has not been verified")
    # The summary already states the trade-off in the terms it was decided on.
    if "—" in proposal.summary:
        pros.append(proposal.summary.split("—", 1)[1].strip())

    # The ranking that actually chose it, in the terms it was decided on.
    # Penalties are friction, so 0 is the best a place can do; the total is
    # stated as the ordinary higher-is-better score everything else uses.
    entity = working.entities.get(chosen.entity_id)
    day = next((d for d in working.itinerary.days if chosen in d.items), None)
    parking_penalty = arrival_penalty(working, entity) if entity else 1.0
    rain_penalty = weather_penalty(working, entity, day) if entity and day else 0.0
    score = DecisionScore(
        total=round(max(0.0, 1.0 - (parking_penalty + rain_penalty) / 3.0), 3),
        dimensions={"arrival": round(parking_penalty, 2), "weather": round(rain_penalty, 2)},
        # Half coverage when the walk was never measured: the number rests on
        # less than it looks like it does.
        coverage=1.0 if (arrival and arrival.parking.is_known) else 0.5,
        notes="friction, not quality: 0 penalty scores 1.0",
    )

    key = f"place_shortlists.replacement_{_slug(chosen.title)}"
    context.pending_decisions[key] = Decision[PlaceOption](
        status="selected",
        rationale=proposal.summary,
        updated_at=utcnow(),
        options=[
            DecisionOption[PlaceOption](
                option_id=f"opt_{chosen.item_id}",
                data=PlaceOption(entity_id=chosen.entity_id, purpose="replacement"),
                status="selected",
                score=score,
                pros=pros,
                cons=cons,
            )
        ],
    ).model_dump(mode="json")


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


def staged_operations(context: ToolContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Everything this turn has staged, as `(entity_ops, other_ops)`.

    Places and the evidence backing them come back separately because they
    have to land *before* anything that references them: a restaurant
    decision's options carry `entity_id` and `evidence_refs`, and
    `check_integrity` rejects the whole patch if either dangles. The two lists
    are one atomic unit - concatenate them, never commit one without the other.

    Shared by `_apply_trip_patch`, by the intake tools that commit as they go,
    and by the runner's end-of-turn flush, so there is exactly one description
    of what "staged" means.
    """
    # Decisions are the one thing worth committing without an itinerary
    # proposal: recommending neighbourhoods produces a Decision and no days.
    other_ops = [
        _decision_op(context.state, name, value)
        for name, value in context.pending_decisions.items()
    ]
    other_ops += _stale_ops(context)

    # Preference snapshots and the questions a blocking conflict raises. Both go
    # in unscoped, alongside decisions: they are facts about the group, not
    # about any one day.
    index_of = {
        traveler.traveler_id: position for position, traveler in enumerate(context.state.travelers)
    }
    other_ops += [
        {"op": "set", "path": f"/travelers/{index_of[traveler_id]}/preferences", "value": value}
        for traveler_id, value in context.pending_traveler_prefs.items()
        if traveler_id in index_of
    ]
    other_ops += [
        {"op": "add", "path": "/open_questions/-", "value": question}
        for question in context.pending_questions
    ]

    # Intake: the brief facts learned this turn, the questions to ask, and the
    # status if the deterministic check said the brief is now complete. All
    # unscoped - the brief is not about any one day.
    other_ops += list(context.pending_brief_ops)
    other_ops += [
        {
            "op": "set" if entity_id in context.state.arrival else "add",
            "path": f"/arrival/{entity_id}",
            "value": arrival,
        }
        for entity_id, arrival in context.pending_arrival.items()
    ]
    other_ops += [
        {"op": "add", "path": "/intake/questions/-", "value": question}
        for question in context.pending_clarifications
    ]
    if context.pending_intake_status:
        other_ops.append(
            {"op": "set", "path": "/intake/status", "value": context.pending_intake_status}
        )

    # Places discovered or refreshed this turn have to land before anything
    # references them. Built unconditionally: they are staged work in their own
    # right, and a version of this that computed them only on the proposal
    # branch threw away thirty discovered places whenever the model committed
    # without an itinerary proposal - which it does routinely, having just
    # gathered them.
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

    # The destination's IANA zone, taken from the places themselves.
    # `TripBrief.timezone` is what `today_at` reads to answer "what is today
    # where they are going", and nothing had ever written it - so every trip
    # that has ever existed worked out its dates in UTC, while every entity in
    # the database sat there carrying the right zone. Set once and never
    # overwritten: a trip's zone is a fact about it, not a running vote.
    if context.state.brief.timezone is None:
        zone = _majority_timezone(context)
        if zone:
            other_ops.append({"op": "set", "path": "/brief/timezone", "value": zone})

    return entity_ops, other_ops


def _majority_timezone(context: ToolContext) -> str | None:
    """The zone most of this trip's places are in.

    Most common rather than first: a trip with a stopover has places in two
    zones, and the one the itinerary is written in is the one it has most of.
    """
    tally: dict[str, int] = {}
    known = list(context.state.entities.values()) + list(context.pending_entity_ops)
    for entity in known:
        zone = getattr(entity, "timezone", None)
        if zone:
            tally[zone] = tally.get(zone, 0) + 1
    if not tally:
        return None
    return sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]


def _commit_staged(context: ToolContext, reason: str) -> dict[str, Any]:
    """Commit everything staged so far, immediately, through the one path.

    Takes the *whole* context rather than a caller's own operations because
    `_apply_all` clears every buffer on a successful commit, not just the ones
    the plan consumed. A tool that committed only its own work would therefore
    wipe whatever an earlier tool had staged and never written - so a commit
    drains the context, and the blanket clear becomes correct by construction.
    """
    entity_ops, other_ops = staged_operations(context)
    return _commit_now(entity_ops + other_ops, reason)


async def _apply_trip_patch(context: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    proposal_id = args.get("proposal_id") or ""
    proposal = context.proposals.get(proposal_id)

    entity_ops, decision_ops = staged_operations(context)
    reason = args.get("reason", "agent update")

    if proposal is None:
        # Asking for a specific proposal that is not there is an error, even
        # with other work staged. Quietly committing the places instead and
        # reporting success would hide that the itinerary never landed - a
        # failure wearing a success's clothes.
        if proposal_id:
            return {
                "error": (
                    f"no such proposal {proposal_id!r}; nothing was applied. Call "
                    f"generate_itinerary or replan_day and use the proposal_id it returns."
                ),
                "applied": False,
            }

        # No proposal asked for: commit whatever this turn staged.
        staged = entity_ops + decision_ops
        if not staged:
            return {
                "error": "no such proposal; call generate_itinerary or replan_day first",
                "applied": False,
            }
        return {
            "__patches__": [
                {
                    "operations": staged,
                    "scope": None,
                    "reason": reason,
                    "unlock_targets": args.get("unlock_targets", []),
                }
            ]
        }

    itinerary_ops = [op.model_dump(mode="json") for op in proposal.operations]

    plans: list[dict[str, Any]] = []

    # A day-scoped patch may add places but not rewrite existing ones - so
    # refreshing a venue's opening hours cannot ride along inside it. Updating
    # Google's facts about a place is not "changing day 3" anyway; it is its own
    # operation, and splitting it keeps the scope guarantee absolute rather than
    # negotiable.
    rewrites_existing = any(op["op"] == "set" for op in entity_ops)
    if proposal.scope is not None and (rewrites_existing or decision_ops):
        plans.append(
            {
                "operations": entity_ops + decision_ops,
                "scope": None,
                "reason": f"refresh place facts before: {reason}",
            }
        )
        entity_ops = []
        decision_ops = []

    plans.append(
        {
            "operations": entity_ops + decision_ops + itinerary_ops,
            "scope": proposal.scope.model_dump(mode="json") if proposal.scope else None,
            "reason": reason,
            "unlock_targets": args.get("unlock_targets", []),
        }
    )

    return {"__patches__": plans, "proposal_id": proposal.proposal_id}


def _decision_op(state: TripState, name: str, value: Any) -> dict[str, Any]:
    """One decision as a patch operation.

    `name` is either a singleton ("hotel_area") or a keyed one
    ("place_shortlists.izakaya_asakusa"). Whether the op is `add` or `set`
    depends on whether it is already there - `add` onto an existing key would
    still work here, but saying which one this is keeps the audit event honest.
    """
    if "." in name:
        group, _, key = name.partition(".")
        exists = key in (getattr(state.decisions, group, None) or {})
        path = f"/decisions/{group}/{key}"
    else:
        exists = getattr(state.decisions, name, None) is not None
        path = f"/decisions/{name}"
    return {"op": "set" if exists else "add", "path": path, "value": value}


def _stale_ops(context: ToolContext) -> list[dict[str, Any]]:
    """Flag decisions a confirmed preference refresh has called into question.

    Flagged, never rebuilt. Whether a settled decision is worth redoing on new
    preferences is the group's call, and silently rescoring one would be the
    "why did my trip change?" problem this project was built to avoid.
    """
    ops: list[dict[str, Any]] = []
    for name, reason in context.pending_stale.items():
        path = "/decisions/" + name.replace(".", "/")
        ops.append({"op": "set", "path": f"{path}/stale_reason", "value": reason})
    return ops


HANDLERS = {
    "update_trip_brief": _update_trip_brief,
    "ask_clarifications": _ask_clarifications,
    "research_web": _research_web,
    "review_group_preferences": _review_group_preferences,
    "refresh_traveler_preferences": _refresh_traveler_preferences,
    "propose_next_step": _propose_next_step,
    "record_constraints": _record_constraints,
    "record_stated_preference": _record_stated_preference,
    "review_learned_preferences": _review_learned_preferences,
    "discover_restaurants": _discover_restaurants,
    "get_weather_context": _get_weather_context,
    "search_airports": _search_airports,
    "search_flights": _search_flights,
    "recommend_hotel_areas": _recommend_hotel_areas,
    "search_hotels": _search_hotels,
    "search_places": _search_places,
    "get_place_details": _get_place_details,
    "get_routes": _get_routes,
    "generate_itinerary": _generate_itinerary,
    "replace_item": _replace_item,
    "replan_day": _replan_day,
    "validate_itinerary": _validate_itinerary,
    "apply_trip_patch": _apply_trip_patch,
}


# Everything that reaches a provider, directly or otherwise. `generate_itinerary`
# and `replan_day` look local but call Google through _ensure_hours/_ensure_routes,
# and are meaningless before a brief exists anyway.
#
# Everything absent from this set stays open, because intake needs it:
# apply_trip_patch writes the brief, update_trip_brief and ask_clarifications are
# the intake itself, and the preference tools read stored profiles only.
RESEARCH_TOOLS = frozenset(
    {
        "search_places",
        "get_place_details",
        "get_routes",
        "research_web",
        "discover_restaurants",
        "search_airports",
        "search_flights",
        "get_weather_context",
        "recommend_hotel_areas",
        "search_hotels",
        "generate_itinerary",
        "replan_day",
        "replace_item",
    }
)


def _refuse_until_confirmed(context: ToolContext, name: str) -> dict[str, Any] | None:
    """Hold research until the traveller has confirmed what we are planning.

    A refusal, not a failure: the model gets a sentence telling it what to do
    instead, and its turn continues. That is how "ask questions" happens -
    having no research worth calling, it asks and then stops.

    Checked in one place rather than at eleven call sites, so a tool added later
    is gated by default instead of by remembering.
    """
    if name not in RESEARCH_TOOLS or research_allowed(context.state):
        return None
    gaps = missing_blocking(context.state)
    return {
        "intake_incomplete": True,
        "message": (
            f"{name} is on hold: this trip's brief has not been confirmed yet, so nothing "
            "has been searched."
        ),
        "still_needed": [{"field": gap.field, "why": gap.why} for gap in gaps],
        "hint": (
            "Save what you already know with update_trip_brief. If something on the list "
            "above is genuinely unknown, ask for it with ask_clarifications - one to three "
            "questions, nothing you were already told. Then stop and let the traveller "
            "confirm the summary; you cannot confirm it for them."
        ),
    }


async def dispatch(context: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name!r}"}
    held = _refuse_until_confirmed(context, name)
    if held is not None:
        return held
    try:
        return await handler(context, arguments)
    except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the turn
        return {"error": f"{type(exc).__name__}: {exc}"}


def serialize(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
