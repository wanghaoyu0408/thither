"""Trip intake: ask only what is needed, then wait to be told to start.

The agent used to research on its first turn, because nothing stopped it. These
pin the behaviour that replaced that - and the one that matters most is the last
one: no provider is touched before the traveller confirms, asserted by giving
the agent a provider that explodes rather than by reading a message and hoping.
"""

from datetime import date

from app.agent.tool_registry import (
    ToolContext,
    _ask_clarifications,
    _search_flights,
    _update_trip_brief,
    dispatch,
)
from app.config import Settings
from app.db.repository import TripRepository
from app.models.constraint import TripConstraint
from app.models.entity import PlaceEntity
from app.models.intake import ClarificationQuestion
from app.models.trip import TripDates, TripState
from app.services import json_pointer as jp
from app.services.intake_service import (
    intake_view,
    missing_blocking,
    research_allowed,
)
from app.services.proposal_store import ProposalStore


def blank_trip() -> TripState:
    """A trip as the create form leaves it when the user typed a sentence."""
    return TripState.new(title="Maui")


def context_for(state: TripState, toolbox=None) -> ToolContext:
    return ToolContext(
        state=state, toolbox=toolbox, proposals=ProposalStore(), settings=Settings()
    )


class ExplodingToolbox:
    """Every provider raises. Reaching one is the failure being tested for."""

    class _Boom:
        def __getattr__(self, name):
            async def explode(*args, **kwargs):
                raise AssertionError(f"a provider was called before confirmation: {name}")

            return explode

    def __getattr__(self, name):
        return self._Boom()


# --- acceptance 1: only the necessary questions -------------------------------


async def test_a_vague_request_blocks_on_when_and_what_only():
    """"我和老婆想去 Maui 玩几天" - the destination and the party are stated."""
    state = blank_trip()
    context = context_for(state)

    await _update_trip_brief(
        context, {"destination_city": "Maui", "adults": 2, "notes": "我和老婆想去 Maui 玩几天"}
    )
    gaps = missing_blocking(_applied(state, context))

    kinds = sorted({gap.kind for gap in gaps})
    # When they are going, and whether to shop for flights and a hotel. Not the
    # destination, which they gave; not the party, which they gave.
    assert kinds == ["dates", "scope"]
    assert not any(gap.field.startswith("/brief/destination") for gap in gaps)
    assert not any(gap.field.startswith("/brief/party") for gap in gaps)


def _applied(state: TripState, context: ToolContext) -> TripState:
    """The state as it would be once this turn's brief ops land."""
    document = state.model_dump(mode="json")
    for operation in context.pending_brief_ops:
        jp.set_value(document, operation["path"], operation["value"])
    return TripState.model_validate(document)


# --- acceptance 2: nothing left to ask ---------------------------------------


async def fully_specified(context: ToolContext) -> TripState:
    """"我和老婆 8/10-8/14 去 Maui，机酒订了，有租车，想吃东西看海，不早起"."""
    await _update_trip_brief(
        context,
        {
            "destination_city": "Maui",
            "start_date": "2026-08-10",
            "end_date": "2026-08-14",
            "adults": 2,
            "priorities": ["food", "beaches", "scenic drives"],
            "pace": "relaxed",
            "scope": {
                "flights": "already_arranged",
                "lodging": "already_arranged",
                "rental_car": "already_arranged",
            },
        },
    )
    return _applied(context.state, context)


async def test_a_complete_request_asks_nothing_and_goes_straight_to_confirmation():
    context = context_for(blank_trip())

    reply = await fully_specified(context)

    assert missing_blocking(reply) == []
    # Decided by the state, not announced by the model.
    assert context.pending_intake_status == "awaiting_confirmation"
    assert context.pending_clarifications == [], "nothing should have been asked"

    view = intake_view(reply)
    assert view.already_arranged == ["Flights", "Hotel", "Rental car"]
    assert view.will_not_search == ["Flights", "Hotel"]
    assert "Activities" in view.will_plan and "Restaurants" in view.will_plan
    assert view.headline == "Maui · Aug 10–Aug 14"
    assert view.travelers == "2 adults"
    assert view.ready_to_confirm is True


# --- acceptance 4: an open destination is a decision, not a gap ---------------


async def test_an_undecided_destination_is_not_a_missing_field():
    """"10月从湾区出去四天，想暖和一点" - they want us to choose."""
    context = context_for(blank_trip())

    await _update_trip_brief(
        context,
        {
            "destination_flexible": True,
            "origin_city": "San Francisco Bay Area",
            "earliest_date": "2026-10-01",
            "latest_date": "2026-10-31",
            "duration_nights": 4,
            "adults": 2,
            "priorities": ["somewhere warm"],
            "scope": {"flights": "plan", "lodging": "plan"},
        },
    )
    state = _applied(context.state, context)

    gaps = missing_blocking(state)
    assert not any(gap.field.startswith("/brief/destination") for gap in gaps), (
        "choosing where to go is the agent's job, not a question for the traveller"
    )
    # The window is enough to plan against; exact dates were never given.
    assert not any(gap.kind == "dates" for gap in gaps)
    assert state.brief.dates.bounded is True
    assert state.brief.dates.decided is False
    assert intake_view(state).headline == "Destination undecided · 4 nights, around October"


# --- acceptance 5: no provider before confirmation ----------------------------


async def test_no_provider_is_touched_before_the_brief_is_confirmed():
    context = context_for(blank_trip(), toolbox=ExplodingToolbox())

    for tool in ("search_places", "discover_restaurants", "search_flights", "search_hotels"):
        result = await dispatch(context, tool, {"query": "beaches", "near": "Maui"})
        assert result["intake_incomplete"] is True, tool
        assert "has not been confirmed" in result["message"]
        assert result["still_needed"], "a refusal has to say what it is waiting for"


async def test_the_gate_lifts_once_the_brief_is_confirmed():
    context = context_for(blank_trip(), toolbox=ExplodingToolbox())
    state = await fully_specified(context)
    state.intake.status = "confirmed"
    context.state = state
    context.pending_brief_ops.clear()

    # No longer held - and therefore reaches the exploding provider, which is
    # exactly the proof that the gate was what stopped it before.
    result = await dispatch(context, "search_places", {"query": "beaches", "near": "Maui"})

    assert "intake_incomplete" not in result
    assert "a provider was called" in result.get("error", ""), result


async def test_a_trip_that_predates_intake_still_researches(session):
    """Work already done implies a brief; locking it behind a confirmation
    nobody was asked for would be a migration dressed up as a rule."""
    state = blank_trip()
    state.entities["ent_beach"] = PlaceEntity(
        entity_id="ent_beach", name="Makena Beach", lat=20.63, lng=-156.44
    )

    assert research_allowed(state) is True

    # And recording a brief fact does not gate it. Moving to
    # `awaiting_confirmation` used to defeat the grandfather clause, so the
    # agent blocked itself the moment it wrote something down.
    state.intake.status = "awaiting_confirmation"
    assert research_allowed(state) is True

    # An outstanding question does hold it, because something is genuinely
    # waiting on the traveller. "Outstanding" means it names a gap that is still
    # open - which every question does, since `ask_clarifications` will not ask
    # one that does not.
    dates = ClarificationQuestion(question="When exactly?", requirement_id="dates")
    state.intake.questions.append(dates)
    assert research_allowed(state) is False

    # And once that gap closes by some other route - said in the chat and
    # recorded to the brief - the question is spent and stops holding anything,
    # even though nobody ever typed an answer into it.
    state.brief.dates = TripDates(start=date(2026, 8, 10), end=date(2026, 8, 14))
    assert "dates" not in {gap.requirement_id for gap in missing_blocking(state)}
    # Spent, not answered. We never learned what they would have typed, only
    # that it stopped mattering, and forging an answer would say otherwise.
    assert dates.answered is False
    assert research_allowed(state) is True

    # But a new trip that intake has actually touched is held.
    fresh = blank_trip()
    assert research_allowed(fresh) is False


# --- questions ---------------------------------------------------------------


async def test_nothing_is_asked_once_nothing_is_blocking():
    """Enforced, not requested. Given a fully-specified trip the model still
    produced "which area is your hotel in?" - useful, maybe, but not something
    planning was waiting on, and acceptance says that trip gets zero questions."""
    context = context_for(blank_trip())
    await fully_specified(context)

    reply = await _ask_clarifications(
        context,
        {"questions": [{"question": "Which area is your hotel in?", "kind": "text",
                       "requirement_id": "lodging_area"}]},
    )

    assert reply["asked"] == 0
    assert "nothing is blocking" in reply["error"]
    assert "__patches__" not in reply


async def test_a_question_about_something_not_blocking_is_dropped():
    """Party size is not a blocking gap - it is asked for by the flight and
    hotel searches that need it, when they need it. The live run slipped one in
    alongside two real questions, which is the interruption this prevents."""
    context = context_for(blank_trip())

    reply = await _ask_clarifications(
        context,
        {
            "questions": [
                {
                    "question": "When are you going?",
                    "kind": "dates",
                    "requirement_id": "dates",
                },
                # Party size is not an outstanding requirement, so this is dropped.
                {"question": "How many of you?", "kind": "text", "requirement_id": "party"},
            ]
        },
    )

    kept = [op["value"]["question"] for op in reply["__patches__"][0]["operations"]]
    assert kept == ["When are you going?"]


async def test_questions_are_asked_once_and_capped_at_three():
    context = context_for(blank_trip())

    first = await _ask_clarifications(
        context,
        {
            "questions": [
                {
                    "question": "When are you going?",
                    "kind": "dates",
                    "requirement_id": "dates",
                },
                {
                    "question": "Shall I look for flights?",
                    "kind": "single_choice",
                    "requirement_id": "scope.flights",
                    "choices": [
                        {"value": "plan", "label": "Please look"},
                        {"value": "already_arranged", "label": "Already booked"},
                    ],
                },
                {
                    "question": "And a hotel?",
                    "kind": "single_choice",
                    "requirement_id": "scope.lodging",
                    "choices": [{"value": "plan", "label": "Please look"}],
                },
                {"question": "A fourth one", "kind": "text", "requirement_id": "dates"},
            ]
        },
    )

    staged = first["__patches__"][0]["operations"]
    assert len(staged) == 3, "at most three at a time"

    context.state.intake.questions.extend(
        ClarificationQuestion.model_validate(op["value"]) for op in staged
    )
    again = await _ask_clarifications(
        context,
        {"questions": [{"question": "When are you going?", "kind": "dates",
                       "requirement_id": "dates"}]},
    )

    assert "__patches__" not in again, "the same question must not be asked twice"
    assert again["asked"] == 0


async def test_a_flight_search_refuses_an_invented_passenger_count():
    """A fare is quoted per passenger; assuming one prices a trip nobody takes."""
    state = blank_trip()
    state.intake.status = "confirmed"
    context = context_for(state, toolbox=ExplodingToolbox())

    result = await _search_flights(
        context,
        {"origins": ["SFO"], "destinations": ["OGG"], "departure_date": "2026-08-10"},
    )

    assert "how many people" in result["error"]
    assert state.brief.party.adults is None, "an unknown count must never become 1"


# --- persistence --------------------------------------------------------------


async def test_intake_writes_itself_rather_than_waiting_to_be_applied(session):
    """Twice in a live run the agent recorded the answers, composed the right
    questions, said "已经记录", and never called apply_trip_patch - leaving an
    empty screen beside a chat message listing three questions. These tools
    commit through the same path, so the model cannot forget."""
    repo = TripRepository(session)
    stored = await repo.create(blank_trip())
    context = context_for(stored)

    recorded = await _update_trip_brief(
        context, {"destination_city": "Maui", "adults": 2, "scope": {"flights": "already_arranged"}}
    )
    asked = await _ask_clarifications(
        context,
        {"questions": [{"question": "When are you going?", "kind": "dates",
                       "requirement_id": "dates"}]},
    )

    paths = [op["path"] for op in recorded["__patches__"][0]["operations"]]
    assert "/brief/destination/city" in paths
    assert "/brief/party/adults" in paths
    assert "/brief/scope/flights" in paths
    # A commit drains the whole context, so calling these two back to back
    # without the runner in between re-sends the brief facts alongside the
    # question. Harmless - they are the same `set` ops with the same values -
    # and in a real turn the runner clears the buffers after each commit, so
    # each fact is written once. What matters is that the question is in there.
    assert "/intake/questions/-" in [
        op["path"] for op in asked["__patches__"][0]["operations"]
    ]


async def test_a_commit_mid_turn_does_not_discard_earlier_staged_places(session):
    """`_apply_all` clears every buffer on success, not just the ones its plan
    consumed. So a tool that committed only its own work would wipe whatever an
    earlier tool had staged and never written - thirty discovered places gone
    because the traveller happened to mention a date afterwards. A commit
    therefore drains the whole context, which is what makes the blanket clear
    correct rather than lucky."""
    from tests.conftest import make_entity

    repo = TripRepository(session)
    stored = await repo.create(blank_trip())
    context = context_for(stored)

    # A search ran first and staged a place.
    context.pending_entity_ops.append(make_entity("ent_found", "Fuglen Tokyo"))

    plan = await _update_trip_brief(context, {"destination_city": "Maui", "adults": 2})

    paths = [op["path"] for op in plan["__patches__"][0]["operations"]]
    assert "/entities/ent_found" in paths, "the earlier search's place must ride along"
    assert "/brief/destination/city" in paths


async def test_the_brief_reaches_the_store_through_the_normal_gates(session):
    """Committing itself does not mean committing differently: the same
    apply_patches, the same validation, the same revision bump and reload."""
    repo = TripRepository(session)
    stored = await repo.create(blank_trip())
    context = context_for(stored)
    plan = await _update_trip_brief(context, {"destination_city": "Maui", "adults": 2})

    from app.models.patch import TripPatch

    result = await repo.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="intake",
            actor="agent",
            operations=plan["__patches__"][0]["operations"],
        ),
    )

    assert result.applied is True
    persisted = await repo.get(stored.trip_id)
    assert persisted.brief.destination.city == "Maui"
    assert persisted.brief.party.adults == 2
    assert persisted.revision == stored.revision + 1


# --- what they asked for, and what we made of it -------------------------------


async def test_a_stated_requirement_becomes_a_constraint_without_losing_its_words(session):
    """Their words are the record; a constraint is a reading of them.

    Nothing in the repo could create a `TripConstraint` before this - the model
    saw them in its state summary and the prompt called them non-negotiable,
    and there was no tool and no endpoint that made one. So "anything else I
    should know" had nowhere structured to land.

    A misreading can be corrected from the original sentence. Nothing can be
    recovered from a paraphrase, which is why the notes are never rewritten to
    match what was recorded.
    """
    from app.agent.tool_registry import _record_constraints
    from app.models.patch import TripPatch

    repo = TripRepository(session)
    said = "back by 9pm on the 23rd, and no long walks - my mother is coming"
    state = blank_trip()
    state.brief.notes = said
    stored = await repo.create(state)
    context = context_for(stored)

    plan = await _record_constraints(context, {"constraints": [
        {"category": "schedule", "description": "back at the hotel by 21:00 on the 23rd",
         "type": "hard"},
        {"category": "mobility", "description": "no long walks", "type": "soft"},
    ]})

    result = await repo.apply_patch(
        stored.trip_id,
        TripPatch(base_revision=stored.revision, reason="constraints", actor="agent",
                  operations=plan["__patches__"][0]["operations"]),
    )
    assert result.applied is True

    persisted = await repo.get(stored.trip_id)
    assert [c.description for c in persisted.constraints] == [
        "back at the hotel by 21:00 on the 23rd",
        "no long walks",
    ]
    assert [c.type for c in persisted.constraints] == ["hard", "soft"]
    # They said it; nobody inferred it.
    assert {c.source for c in persisted.constraints} == {"user_explicit"}
    # And the sentence they actually typed is untouched.
    assert persisted.brief.notes == said


async def test_the_same_requirement_is_not_recorded_twice(session):
    from app.agent.tool_registry import _record_constraints

    stored = await TripRepository(session).create(blank_trip())
    context = context_for(stored)
    context.state.constraints = [
        TripConstraint(category="mobility", description="No long walks", type="soft",
                       source="user_explicit")
    ]

    result = await _record_constraints(context, {"constraints": [
        {"category": "mobility", "description": "no long walks", "type": "soft"},
    ]})

    assert result["recorded"] == 0
    assert "already recorded" in result["note"]


async def test_a_constraint_cannot_be_pinned_on_someone_who_is_not_on_the_trip(session):
    from app.agent.tool_registry import _record_constraints

    stored = await TripRepository(session).create(blank_trip())
    context = context_for(stored)

    result = await _record_constraints(context, {"constraints": [
        {"category": "food", "description": "no shellfish", "type": "hard",
         "traveler_id": "trv_nobody"},
    ]})

    assert "error" in result
    assert context.pending_brief_ops == []
