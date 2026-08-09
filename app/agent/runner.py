"""The agent loop.

Deliberately thin. It moves messages and tool results around, applies patches
through the same engine everything else uses, and keeps a structured record of
what happened (spec section 39). All the judgement lives in the prompt; all the
arithmetic lives in the services.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import summarize
from app.agent.prompts import build_instructions
from app.agent.tool_registry import TOOL_SCHEMAS, ToolContext, dispatch, serialize
from app.config import Settings, get_settings
from app.db.repository import ProfileRepository, TripRepository
from app.models.patch import PatchResult, TripPatch
from app.models.trip import TripState
from app.providers.openai_llm import LLMClient
from app.services.proposal_store import ProposalStore
from app.services.toolbox import Toolbox


@dataclass
class ToolRecord:
    name: str
    arguments: dict[str, Any]
    milliseconds: int
    ok: bool
    detail: str = ""


@dataclass
class AgentRun:
    """Everything worth logging about one turn (spec section 39)."""

    trip_id: str
    revision_before: int
    revision_after: int
    reply: str = ""
    tools: list[ToolRecord] = field(default_factory=list)
    patches: list[PatchResult] = field(default_factory=list)
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    response_ids: list[str] = field(default_factory=list)
    error: str | None = None

    # True when the loop was cut off mid-task rather than the model finishing.
    # Kept apart from `error`, which means the turn failed: work may well have
    # been applied before the cap hit. Without this flag, running out of steps
    # is indistinguishable from a normal completion to anything reading the
    # result - which is how an unfinished plan comes to look like a finished one.
    hit_iteration_limit: bool = False

    @property
    def changed_state(self) -> bool:
        return self.revision_after != self.revision_before

    def as_log(self) -> dict[str, Any]:
        return {
            "trip_id": self.trip_id,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "iterations": self.iterations,
            "tools": [
                {"name": t.name, "ms": t.milliseconds, "ok": t.ok, "detail": t.detail}
                for t in self.tools
            ],
            "patches_applied": sum(1 for p in self.patches if p.applied),
            "patches_rejected": [[e.code for e in p.errors] for p in self.patches if not p.applied],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "response_ids": self.response_ids,
            "error": self.error,
        }


class AgentRunner:
    def __init__(
        self,
        llm: LLMClient,
        toolbox: Toolbox,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        proposals: ProposalStore | None = None,
    ) -> None:
        self._llm = llm
        self._toolbox = toolbox
        self._repo = TripRepository(session)
        # Profiles live in their own table. Reading them is how a snapshot gets
        # made in the first place; planning still only ever reads the snapshot.
        self._profiles = ProfileRepository(session)
        self._settings = settings or get_settings()
        self._proposals = proposals or ProposalStore()

    async def run(
        self,
        state: TripState,
        message: str,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> AgentRun:
        run = AgentRun(
            trip_id=state.trip_id,
            revision_before=state.revision,
            revision_after=state.revision,
        )
        context = ToolContext(
            state=state,
            toolbox=self._toolbox,
            proposals=self._proposals,
            settings=self._settings,
            profiles=self._profiles,
        )

        conversation: list[Any] = list(history or [])
        conversation.append(
            {
                "role": "user",
                "content": (
                    f"Current trip state (authoritative):\n"
                    f"{json.dumps(summarize(state), ensure_ascii=False, default=str)}\n\n"
                    f"User says: {message}"
                ),
            }
        )

        for iteration in range(1, self._settings.agent_max_iterations + 1):
            run.iterations = iteration

            turn = await self._llm.respond(
                instructions=build_instructions(),
                conversation=conversation,
                tools=TOOL_SCHEMAS,
            )
            run.input_tokens += turn.input_tokens
            run.output_tokens += turn.output_tokens
            if turn.response_id:
                run.response_ids.append(turn.response_id)

            if turn.error is not None:
                run.error = turn.error.message
                run.reply = (
                    "I could not reach the language model just now, so I have not changed "
                    "anything. Please try again in a moment."
                )
                return run

            conversation.extend(turn.output_items)

            if not turn.wants_tools:
                run.reply = turn.text or ""
                return run

            for call in turn.tool_calls:
                started = time.perf_counter()
                result = await dispatch(context, call.name, call.arguments)
                elapsed = int((time.perf_counter() - started) * 1000)

                # apply_trip_patch hands back patch plans rather than applying
                # them itself, so every write goes through one code path here.
                if "__patches__" in result:
                    result = await self._apply_all(context, run, result["__patches__"])

                run.tools.append(
                    ToolRecord(
                        name=call.name,
                        arguments=call.arguments,
                        milliseconds=elapsed,
                        ok="error" not in result,
                        detail=str(result.get("error", ""))[:200],
                    )
                )
                conversation.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": serialize(result),
                    }
                )

        # Falling out of the loop means the model was still working. Whatever it
        # had already said is kept, but the truncation is stated either way -
        # silently returning a half-finished turn's last words as if they were
        # its conclusion would be the worst of both.
        run.hit_iteration_limit = True
        cut_short = (
            f"I ran out of steps working on that after {run.iterations} rounds. Anything "
            "already applied is safe and complete - nothing was left half-applied - but I "
            "had more to do. Tell me which part to focus on and I will do that one thing."
        )
        run.reply = f"{turn.text}\n\n{cut_short}" if turn.text else cut_short
        return run

    async def _apply_all(
        self, context: ToolContext, run: AgentRun, plans: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Apply patch plans in order, stopping at the first rejection.

        Each plan is built against the revision left by the previous one, so a
        refresh-then-replan pair cannot conflict with itself.
        """
        outcome: dict[str, Any] = {"applied": False}
        for plan in plans:
            if not plan["operations"]:
                continue
            patch = TripPatch(
                base_revision=context.state.revision,
                reason=plan["reason"],
                actor="agent",
                operations=plan["operations"],
                scope=plan.get("scope"),
                unlock_targets=plan.get("unlock_targets", []),
            )
            outcome = await self._apply(context, run, patch)
            if not outcome.get("applied"):
                return outcome
        return outcome

    async def _apply(self, context: ToolContext, run: AgentRun, patch: TripPatch) -> dict[str, Any]:
        result = await self._repo.apply_patch(context.state.trip_id, patch)
        run.patches.append(result)

        if not result.applied:
            return {
                "applied": False,
                "errors": [
                    {
                        "code": error.code,
                        "message": error.message,
                        "details": error.details,
                    }
                    for error in result.errors
                ],
                "hint": "fix the cause and propose again; do not retry this patch unchanged",
            }

        context.state = result.state
        context.pending_entity_ops.clear()
        context.pending_evidence.clear()
        context.pending_decisions.clear()
        context.pending_traveler_prefs.clear()
        context.pending_questions.clear()
        run.revision_after = result.revision

        return {
            "applied": True,
            "revision": result.revision,
            "warnings": [w.message for w in result.warnings],
        }
