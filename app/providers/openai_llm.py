"""OpenAI Responses API, behind a small interface (spec section 35).

Everything the agent needs from a model is here: send a conversation plus tool
schemas, get back either text or tool calls. Swapping providers means writing
one more class, not touching the runner.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import APIError, AsyncOpenAI

from app.config import Settings, get_settings
from app.models.tool import ToolError


@dataclass
class LLMToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMTurn:
    text: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    # Raw output items, fed straight back so the model sees its own calls.
    output_items: list[Any] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    response_id: str | None = None
    error: ToolError | None = None
    # The model was still writing when it hit `max_output_tokens`. Whatever came
    # back is a fragment, and a fragment presented as an answer is worse than no
    # answer: the reader cannot tell it was cut. Capping output without saying
    # when the cap bit would have been the same defect as a loop that runs out
    # of rounds and looks like it finished.
    truncated: bool = False

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    async def respond(
        self,
        *,
        instructions: str,
        conversation: list[Any],
        tools: list[dict[str, Any]],
    ) -> LLMTurn: ...


class OpenAIClient:
    def __init__(self, api_key: str, model: str, *, settings: Settings | None = None) -> None:
        config = settings or get_settings()
        # The SDK's own defaults are a 600s timeout and two silent retries, so
        # one hung request could hold a turn for half an hour and be paid for
        # three times. Both are stated here instead of inherited.
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=config.openai_request_timeout_seconds,
            max_retries=config.openai_max_retries,
        )
        self._model = model
        self._max_output_tokens = config.openai_max_output_tokens

    @property
    def model(self) -> str:
        return self._model

    async def respond(
        self,
        *,
        instructions: str,
        conversation: list[Any],
        tools: list[dict[str, Any]],
    ) -> LLMTurn:
        try:
            response = await self._client.responses.create(
                model=self._model,
                instructions=instructions,
                input=conversation,
                tools=tools,
                max_output_tokens=self._max_output_tokens,
            )
        except APIError as exc:
            # A dead or misconfigured model must surface as an explicit failure,
            # never as an empty turn the runner might mistake for "nothing to do".
            return LLMTurn(
                error=ToolError(
                    code="provider_unavailable",
                    message=f"{type(exc).__name__}: {exc}",
                    provider="openai",
                    retryable=True,
                )
            )

        calls: list[LLMToolCall] = []
        texts: list[str] = []

        for item in response.output:
            kind = getattr(item, "type", None)
            if kind == "function_call":
                try:
                    arguments = json.loads(item.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"__unparsable__": item.arguments}
                calls.append(LLMToolCall(call_id=item.call_id, name=item.name, arguments=arguments))
            elif kind == "message":
                for part in getattr(item, "content", []) or []:
                    text = getattr(part, "text", None)
                    if text:
                        texts.append(text)

        usage = getattr(response, "usage", None)
        details = getattr(response, "incomplete_details", None)
        return LLMTurn(
            text="\n".join(texts) or None,
            tool_calls=calls,
            output_items=list(response.output),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            response_id=getattr(response, "id", None),
            truncated=getattr(details, "reason", None) == "max_output_tokens",
        )
