"""The agent loop, driven by a scripted model (no key, no network).

What matters here is not what a model would say but what the runner does with
it: that every write goes through the patch engine, that a rejected patch is
reported back rather than swallowed, and that a confused model cannot loop
forever or leave the trip half-changed.
"""

from datetime import date

import pytest

from app.agent.context import summarize
from app.agent.runner import AgentRunner
from app.agent.tool_registry import TOOL_SCHEMAS
from app.config import Settings
from app.models.itinerary_plan import ItineraryProposal
from app.models.patch import PatchOperation, PatchScope
from app.models.tool import ToolError
from app.providers.openai_llm import LLMToolCall, LLMTurn
from app.services.proposal_store import ProposalStore
from tests.conftest import sample_state


class ScriptedLLM:
    """Replays a fixed list of turns and records what it was shown."""

    def __init__(self, turns: list[LLMTurn]) -> None:
        self._turns = list(turns)
        self.calls = 0
        self.conversations: list[list] = []
        self.instructions: list[str] = []

    async def respond(self, *, instructions, conversation, tools):
        self.calls += 1
        self.instructions.append(instructions)
        self.conversations.append(list(conversation))
        if self._turns:
            return self._turns.pop(0)
        return LLMTurn(text="done")


def say(text: str) -> LLMTurn:
    return LLMTurn(text=text)


def call(name: str, arguments: dict, call_id: str = "call_1") -> LLMTurn:
    return LLMTurn(tool_calls=[LLMToolCall(call_id=call_id, name=name, arguments=arguments)])


@pytest.fixture
def settings():
    return Settings(
        openai_api_key="test", database_url="sqlite+aiosqlite:///:memory:", agent_max_iterations=4
    )


async def make_runner(session, llm, settings, proposals=None):
    return AgentRunner(llm, toolbox=None, session=session, settings=settings, proposals=proposals)


# --- plain replies -----------------------------------------------------------


async def test_a_reply_with_no_tool_calls_ends_the_turn(session, settings):
    llm = ScriptedLLM([say("Tokyo in October is a good idea.")])
    runner = await make_runner(session, llm, settings)
    state = sample_state()

    run = await runner.run(state, "Any thoughts on Tokyo?")

    assert run.reply == "Tokyo in October is a good idea."
    assert run.iterations == 1
    assert run.changed_state is False
    assert llm.calls == 1


async def test_the_model_is_shown_the_trip_state_not_asked_to_remember_it(session, settings):
    llm = ScriptedLLM([say("ok")])
    runner = await make_runner(session, llm, settings)
    state = sample_state()

    await runner.run(state, "what have we got?")

    shown = llm.conversations[0][-1]["content"]
    assert state.trip_id in shown
    assert "Tokyo" in shown
    assert "authoritative" in shown


async def test_the_system_prompt_carries_the_rules_that_matter(session, settings):
    llm = ScriptedLLM([say("ok")])
    runner = await make_runner(session, llm, settings)

    await runner.run(sample_state(), "hello")

    instructions = llm.instructions[0]
    assert "TripState is the source of truth" in instructions
    assert "Never modify a locked item" in instructions
    assert "do not book" in instructions.lower()


# --- state summary -----------------------------------------------------------


def test_the_summary_marks_locked_items():
    from app.models import LockRecord

    state = sample_state()
    state.locks = [
        LockRecord(
            lock_id="lock_1",
            target_kind="itinerary_item",
            target_id="item_dinner",
            reason="booked",
        )
    ]

    summary = summarize(state)
    items = summary["itinerary"][0]["items"]

    assert any(item["locked"] for item in items)
    assert summary["locks"][0]["reason"] == "booked"


def test_the_summary_carries_rejections_so_they_are_not_re_suggested():
    from app.models import RejectionRecord

    state = sample_state()
    state.rejections = [
        RejectionRecord(
            rejection_id="rej_1", target_kind="entity", target_id="ent_x", label="Ichiran"
        )
    ]

    assert summarize(state)["rejections"][0]["label"] == "Ichiran"


def test_the_summary_prefers_scheduled_places_when_truncating():
    state = sample_state()
    from tests.conftest import make_entity

    for index in range(80):
        entity = make_entity(f"ent_filler_{index:03d}", f"Filler {index}")
        state.entities[entity.entity_id] = entity

    summary = summarize(state)
    listed = {row["entity_id"] for row in summary["entities"]}

    assert summary["entities_truncated"] > 0
    # ent_cafe and ent_museum are on the itinerary and must survive truncation.
    assert {"ent_cafe", "ent_museum"} <= listed


# --- patches -----------------------------------------------------------------


async def test_an_applied_proposal_moves_the_revision(session, settings):
    from app.db.repository import TripRepository

    state = await TripRepository(session).create(sample_state())
    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_1",
            trip_id=state.trip_id,
            base_revision=state.revision,
            operations=[PatchOperation(op="set", path="/status", value="planning")],
            summary="mark it as planning",
        )
    )

    llm = ScriptedLLM(
        [
            call("apply_trip_patch", {"proposal_id": "prop_1", "reason": "start planning"}),
            say("Marked as planning."),
        ]
    )
    runner = await make_runner(session, llm, settings, proposals)

    run = await runner.run(state, "let's start")

    assert run.revision_after == state.revision + 1
    assert run.changed_state is True
    assert run.patches[0].applied is True
    assert (await TripRepository(session).get(state.trip_id)).status == "planning"


async def test_a_rejected_patch_is_reported_back_to_the_model(session, settings):
    """The model must see *why*, or it will retry the same broken patch."""
    from app.db.repository import TripRepository

    state = await TripRepository(session).create(sample_state())
    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_bad",
            trip_id=state.trip_id,
            base_revision=state.revision,
            operations=[PatchOperation(op="set", path="/status", value="teleporting")],
            summary="nonsense",
        )
    )

    llm = ScriptedLLM(
        [
            call("apply_trip_patch", {"proposal_id": "prop_bad", "reason": "oops"}),
            say("That did not work; the status value was not valid."),
        ]
    )
    runner = await make_runner(session, llm, settings, proposals)

    run = await runner.run(state, "change the status")

    assert run.patches[0].applied is False
    assert run.revision_after == state.revision

    fed_back = llm.conversations[1][-1]["output"]
    assert "SCHEMA_INVALID" in fed_back
    assert "do not retry this patch unchanged" in fed_back


async def test_a_scope_violation_reaches_the_model_as_such(session, settings):
    from app.db.repository import TripRepository
    from app.models import ItineraryDay

    base = sample_state()
    base.itinerary.days.append(ItineraryDay(date=date(2026, 10, 4)))
    state = await TripRepository(session).create(base)

    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_wide",
            trip_id=state.trip_id,
            base_revision=state.revision,
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-04"),
            operations=[
                PatchOperation(op="set", path="/itinerary/days/0/theme", value="Elsewhere")
            ],
            summary="claims day 2, edits day 1",
        )
    )

    llm = ScriptedLLM(
        [call("apply_trip_patch", {"proposal_id": "prop_wide", "reason": "tidy up"}), say("no")]
    )
    runner = await make_runner(session, llm, settings, proposals)

    run = await runner.run(state, "tidy day 2")

    assert run.patches[0].applied is False
    assert "SCOPE_VIOLATION" in llm.conversations[1][-1]["output"]


async def test_a_scoped_replan_splits_off_its_entity_refresh(settings):
    """Refreshing a venue's hours is not "changing day 3".

    A day-scoped patch may add places but not rewrite existing ones, so a
    replan that had also refreshed opening hours was rejected outright. The
    refresh now goes first as its own unscoped patch, which keeps the scope
    guarantee absolute instead of negotiable.
    """
    from app.agent.tool_registry import ToolContext, _apply_trip_patch

    state = sample_state()
    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_scoped",
            trip_id=state.trip_id,
            base_revision=state.revision,
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-03"),
            operations=[PatchOperation(op="set", path="/itinerary/days/0/theme", value="Quieter")],
            summary="calm day 1 down",
        )
    )

    context = ToolContext(state=state, toolbox=None, proposals=proposals, settings=settings)
    # A refreshed copy of a place the trip already knows, as _ensure_hours produces.
    context.pending_entity_ops.append(state.entities["ent_cafe"].model_copy(update={"rating": 4.9}))

    result = await _apply_trip_patch(
        context, {"proposal_id": "prop_scoped", "reason": "make day 1 easier"}
    )
    plans = result["__patches__"]

    assert len(plans) == 2
    assert plans[0]["scope"] is None
    assert plans[0]["operations"][0]["path"] == "/entities/ent_cafe"
    assert "refresh place facts" in plans[0]["reason"]

    assert plans[1]["scope"]["target_id"] == "2026-10-03"
    assert all("/entities/" not in op["path"] for op in plans[1]["operations"])


async def test_an_unscoped_proposal_stays_a_single_patch(settings):
    """Splitting when there is no scope would burn a revision for nothing."""
    from app.agent.tool_registry import ToolContext, _apply_trip_patch

    state = sample_state()
    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_open",
            trip_id=state.trip_id,
            base_revision=state.revision,
            operations=[PatchOperation(op="set", path="/status", value="planning")],
            summary="whole trip",
        )
    )

    context = ToolContext(state=state, toolbox=None, proposals=proposals, settings=settings)
    context.pending_entity_ops.append(state.entities["ent_cafe"].model_copy(update={"rating": 4.9}))

    result = await _apply_trip_patch(context, {"proposal_id": "prop_open", "reason": "plan"})

    assert len(result["__patches__"]) == 1


async def test_a_new_place_rides_along_inside_a_scoped_patch(settings):
    """Adding a place is allowed under scope, so it needs no separate patch."""
    from app.agent.tool_registry import ToolContext, _apply_trip_patch
    from tests.conftest import make_entity

    state = sample_state()
    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_scoped",
            trip_id=state.trip_id,
            base_revision=state.revision,
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-03"),
            operations=[PatchOperation(op="set", path="/itinerary/days/0/theme", value="New")],
            summary="day 1",
        )
    )

    context = ToolContext(state=state, toolbox=None, proposals=proposals, settings=settings)
    context.pending_entity_ops.append(make_entity("ent_brand_new", "Somewhere New"))

    result = await _apply_trip_patch(context, {"proposal_id": "prop_scoped", "reason": "x"})

    assert len(result["__patches__"]) == 1
    assert result["__patches__"][0]["operations"][0]["op"] == "add"


async def test_an_unknown_proposal_id_is_an_error_not_a_write(session, settings):
    from app.db.repository import TripRepository

    state = await TripRepository(session).create(sample_state())
    llm = ScriptedLLM(
        [call("apply_trip_patch", {"proposal_id": "nope", "reason": "x"}), say("sorry")]
    )
    runner = await make_runner(session, llm, settings)

    run = await runner.run(state, "apply it")

    assert run.patches == []
    assert run.revision_after == state.revision
    assert "no such proposal" in llm.conversations[1][-1]["output"]


# --- resilience --------------------------------------------------------------


async def test_the_loop_stops_at_the_iteration_cap(session, settings):
    """A model stuck calling tools must not spend forever."""
    llm = ScriptedLLM([call("validate_itinerary", {}, f"call_{i}") for i in range(20)])
    runner = await make_runner(session, llm, settings)

    run = await runner.run(sample_state(), "check it")

    assert run.iterations == settings.agent_max_iterations
    assert llm.calls == settings.agent_max_iterations
    assert "ran out of steps" in run.reply
    # Being cut off mid-task must be visible, not inferable. Without the flag a
    # truncated turn looks exactly like a finished one to anything that reads
    # only `error` - which is how an unfinished plan gets reported as done.
    assert run.hit_iteration_limit is True
    assert run.error is None


async def test_a_finished_turn_is_not_flagged_as_cut_short(session, settings):
    llm = ScriptedLLM([say("all done")])
    runner = await make_runner(session, llm, settings)

    run = await runner.run(sample_state(), "check it")

    assert run.hit_iteration_limit is False
    assert run.reply == "all done"


async def test_words_spoken_before_the_cap_are_kept_and_qualified(session, settings):
    """Returning a working turn's last words as its conclusion would mislead.

    So whatever the model managed to say is preserved, with the truncation
    stated alongside it rather than instead of it.
    """
    turns = [
        LLMTurn(
            text="Here is what I have so far.",
            tool_calls=[LLMToolCall(call_id=f"call_{i}", name="validate_itinerary", arguments={})],
        )
        for i in range(20)
    ]
    runner = await make_runner(session, ScriptedLLM(turns), settings)

    run = await runner.run(sample_state(), "check it")

    assert run.hit_iteration_limit is True
    assert "Here is what I have so far." in run.reply
    assert "ran out of steps" in run.reply


async def test_a_model_outage_changes_nothing_and_says_so(session, settings):
    llm = ScriptedLLM(
        [LLMTurn(error=ToolError(code="provider_unavailable", message="503", provider="openai"))]
    )
    runner = await make_runner(session, llm, settings)
    state = sample_state()

    run = await runner.run(state, "plan my trip")

    assert run.error == "503"
    assert run.changed_state is False
    assert "not changed anything" in run.reply


async def test_a_tool_that_raises_is_reported_not_fatal(session, settings):
    llm = ScriptedLLM([call("replan_day", {"date": "not-a-date"}), say("bad date")])
    runner = await make_runner(session, llm, settings)

    run = await runner.run(sample_state(), "fix day 3")

    assert run.tools[0].ok is False
    assert run.reply == "bad date"


async def test_an_unknown_tool_name_is_handled(session, settings):
    llm = ScriptedLLM([call("book_the_flight", {}), say("I cannot book anything.")])
    runner = await make_runner(session, llm, settings)

    run = await runner.run(sample_state(), "book it")

    assert run.tools[0].ok is False
    assert "unknown tool" in llm.conversations[1][-1]["output"]


# --- the schemas the model is handed -----------------------------------------


def test_no_booking_tool_is_exposed():
    names = {tool["name"] for tool in TOOL_SCHEMAS}

    assert not any("book" in name or "pay" in name for name in names)
    assert "apply_trip_patch" in names
    assert "replan_day" in names


def test_every_tool_schema_is_well_formed():
    for tool in TOOL_SCHEMAS:
        assert tool["type"] == "function"
        assert tool["name"] and tool["description"]
        assert tool["parameters"]["type"] == "object"
        for required in tool["parameters"].get("required", []):
            assert required in tool["parameters"]["properties"], tool["name"]


def test_the_run_log_records_what_the_turn_did(session, settings):
    from app.agent.runner import AgentRun, ToolRecord

    run = AgentRun(trip_id="trip_1", revision_before=3, revision_after=4)
    run.tools.append(ToolRecord(name="replan_day", arguments={}, milliseconds=12, ok=True))

    log = run.as_log()

    assert log["revision_before"] == 3
    assert log["revision_after"] == 4
    assert log["tools"][0]["name"] == "replan_day"
