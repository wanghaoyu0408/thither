"""Stopping a turn, and watching one run.

The contract under test: a stop is honoured at the next safe seam - before a
round, mid-round by abandoning the model call, or between tool calls - and it
never tears anything. Whatever committed before the stop stands; nothing after
it starts. And a stop must say what it did, not pretend the turn finished.
"""

import asyncio

from app.agent import run_control
from app.agent.run_control import RunControl, RunInProgress
from app.agent.runner import AgentRunner
from app.config import Settings
from app.db.repository import TripRepository
from app.models.itinerary_plan import ItineraryProposal
from app.models.patch import PatchOperation
from app.providers.openai_llm import LLMToolCall, LLMTurn
from app.services.proposal_store import ProposalStore
from tests.conftest import sample_state


def settings() -> Settings:
    return Settings(
        openai_api_key="test", database_url="sqlite+aiosqlite:///:memory:", agent_max_iterations=4
    )


class BlockingLLM:
    """A model whose round never returns - unless abandoned."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0
        self.abandoned = False

    async def respond(self, *, instructions, conversation, tools):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.abandoned = True
            raise


class ScriptedLLM:
    def __init__(self, turns: list[LLMTurn]) -> None:
        self._turns = list(turns)
        self.calls = 0

    async def respond(self, *, instructions, conversation, tools):
        self.calls += 1
        if self._turns:
            return self._turns.pop(0)
        return LLMTurn(text="done")


def apply_call(proposal_id: str, call_id: str) -> LLMToolCall:
    return LLMToolCall(
        call_id=call_id,
        name="apply_trip_patch",
        arguments={"proposal_id": proposal_id, "reason": "test"},
    )


def cancel_during_first_tool(control: RunControl) -> asyncio.Task:
    """Set the stop flag while the first tool is in flight.

    `begin_tool` marks the tool before dispatch starts, and dispatch's own
    awaits yield to the loop - so this fires *during* the tool, proving the
    tool is allowed to finish and the stop lands at the seam after it.
    """

    async def watch():
        while control.current_tool is None:
            await asyncio.sleep(0)
        control.cancel()

    return asyncio.ensure_future(watch())


# --- the registry -------------------------------------------------------------


def test_a_second_run_for_the_same_trip_is_refused():
    run_control.begin("trip_reg1")
    try:
        try:
            run_control.begin("trip_reg1")
            raise AssertionError("a second begin() should have been refused")
        except RunInProgress:
            pass
        # A different trip is not held hostage by the first one's turn.
        run_control.begin("trip_reg2")
        run_control.finish("trip_reg2")
    finally:
        run_control.finish("trip_reg1")
    # And finishing frees the name for the next turn.
    run_control.begin("trip_reg1")
    run_control.finish("trip_reg1")


def test_a_snapshot_reports_progress_without_trip_state():
    control = RunControl("trip_snap")
    control.begin_iteration(2)
    control.begin_tool("discover_restaurants")
    control.end_tool("discover_restaurants", 1234, True)
    control.begin_tool("generate_itinerary")

    snap = control.snapshot()

    assert snap["running"] is True
    assert snap["iteration"] == 2
    assert snap["cancelled"] is False
    assert snap["tools_done"] == [
        {"name": "discover_restaurants", "milliseconds": 1234, "ok": True}
    ]
    assert snap["current_tool"]["name"] == "generate_itinerary"
    # Telemetry only: nothing here is trip state.
    assert "entities" not in snap and "brief" not in snap


# --- the seams a stop can land at ---------------------------------------------


async def test_a_stop_before_the_round_never_calls_the_model(session):
    llm = ScriptedLLM([LLMTurn(text="never")])
    runner = AgentRunner(llm, toolbox=None, session=session, settings=settings())
    control = RunControl("trip_pre")
    control.cancel()

    run = await runner.run(sample_state(), "plan it", control=control)

    assert run.cancelled is True
    assert llm.calls == 0
    assert run.tools == []


async def test_cancel_abandons_a_round_in_flight(session):
    """The model round is the long pole; a stop must not wait it out."""
    llm = BlockingLLM()
    runner = AgentRunner(llm, toolbox=None, session=session, settings=settings())
    control = run_control.begin("trip_midround")
    try:
        task = asyncio.ensure_future(runner.run(sample_state(), "plan it", control=control))
        await asyncio.wait_for(llm.started.wait(), timeout=2)

        control.cancel()
        run = await asyncio.wait_for(task, timeout=2)
    finally:
        run_control.finish("trip_midround")

    assert llm.abandoned is True, "the in-flight round should have been cancelled"
    assert run.cancelled is True
    assert "Stopped at your request" in run.reply
    assert "Nothing had been saved yet" in run.reply
    assert run.changed_state is False


async def test_cancel_during_a_tool_lets_it_finish_and_stops_the_next(session):
    """Two tool calls in one round: the stop arrives while the first runs.

    The first is allowed to complete - interrupting a commit mid-flight is the
    one thing a stop must never do - and the second never starts.
    """
    state = await TripRepository(session).create(sample_state())
    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_a",
            trip_id=state.trip_id,
            base_revision=state.revision,
            operations=[PatchOperation(op="set", path="/status", value="planning")],
            summary="first",
        )
    )
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_b",
            trip_id=state.trip_id,
            base_revision=state.revision + 1,
            operations=[PatchOperation(op="set", path="/brief/notes", value="b landed")],
            summary="second",
        )
    )
    llm = ScriptedLLM(
        [LLMTurn(tool_calls=[apply_call("prop_a", "c1"), apply_call("prop_b", "c2")])]
    )
    runner = AgentRunner(
        llm, toolbox=None, session=session, settings=settings(), proposals=proposals
    )
    control = RunControl(state.trip_id)

    watcher = cancel_during_first_tool(control)
    run = await runner.run(state, "do both", control=control)
    await watcher

    assert run.cancelled is True
    assert len(run.tools) == 1, "the second tool must never have started"
    assert run.changed_state is True, "the first tool's commit stands"
    assert "1 change(s) had already been saved" in run.reply

    persisted = await TripRepository(session).get(state.trip_id)
    assert persisted.status == "planning"
    assert persisted.brief.notes != "b landed"


async def test_a_cancelled_turn_reports_what_it_saved_and_skips_the_next_round(session):
    state = await TripRepository(session).create(sample_state())
    proposals = ProposalStore()
    proposals.put(
        ItineraryProposal(
            proposal_id="prop_1",
            trip_id=state.trip_id,
            base_revision=state.revision,
            operations=[PatchOperation(op="set", path="/status", value="planning")],
            summary="mark it",
        )
    )
    llm = ScriptedLLM(
        [LLMTurn(tool_calls=[apply_call("prop_1", "c1")]), LLMTurn(text="round two")]
    )
    runner = AgentRunner(
        llm, toolbox=None, session=session, settings=settings(), proposals=proposals
    )
    control = RunControl(state.trip_id)

    watcher = cancel_during_first_tool(control)
    run = await runner.run(state, "start planning", control=control)
    await watcher

    assert run.cancelled is True
    assert run.changed_state is True
    assert llm.calls == 1, "round two must never have been requested"
    assert (await TripRepository(session).get(state.trip_id)).status == "planning"


async def test_an_uncontrolled_run_is_unchanged(session):
    """No control object, no new behaviour - the plain path stays the plain path."""
    llm = ScriptedLLM([LLMTurn(text="fine")])
    runner = AgentRunner(llm, toolbox=None, session=session, settings=settings())

    run = await runner.run(sample_state(), "hello")

    assert run.reply == "fine"
    assert run.cancelled is False
