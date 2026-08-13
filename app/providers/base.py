"""Shared plumbing for external providers.

Providers speak HTTP and raise. Services catch and translate into
`ToolResult.error`, so nothing above the service layer ever sees an exception
from a third party - and nothing below it invents a fallback value.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings
from app.models.tool import ToolError, ToolErrorCode

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.75


class ProviderError(Exception):
    """A provider call failed. Never means "no results"."""

    code: ToolErrorCode = "unknown"
    retryable = False

    def __init__(self, message: str, provider: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code

    def as_tool_error(self) -> ToolError:
        return ToolError(
            code=self.code,
            message=self.message,
            provider=self.provider,
            retryable=self.retryable,
        )


class ProviderUnavailable(ProviderError):
    code: ToolErrorCode = "provider_unavailable"
    retryable = True


class ProviderTimeout(ProviderError):
    code: ToolErrorCode = "timeout"
    retryable = True


class ProviderRateLimited(ProviderError):
    code: ToolErrorCode = "rate_limited"
    retryable = True


class ProviderAuthFailed(ProviderError):
    code: ToolErrorCode = "auth_failed"
    retryable = False


class ProviderBadRequest(ProviderError):
    code: ToolErrorCode = "invalid_request"
    retryable = False


class BudgetExhausted(ProviderError):
    """This turn has made every paid call it is allowed to make."""

    code: ToolErrorCode = "budget_exhausted"
    # Never retryable. The ceiling will be just as reached on the second
    # attempt, and a retryable error invites exactly the loop this exists to
    # stop.
    retryable = False


# What one turn may spend
# -----------------------
# Two caps existed before this and neither bounded money. `agent_max_iterations`
# bounds *rounds*, but one round can emit any number of tool calls - a real turn
# was measured at 22 calls across 14 rounds. `planning_search_budget` bounds
# *searches*, but only the three Places-family tools consulted it; the twelve
# others that reach a paid provider, flights and hotels among them, counted
# nothing.
#
# So this counts the only thing that is actually billed - a call that reaches
# the network - at the one function every paid provider goes through. Nothing
# above has to remember to check, which is the whole point: a tool added later
# cannot escape the budget by forgetting to ask for it. Sprinkling a check
# through twelve handlers is how a guard ends up present everywhere except the
# one place it was needed, which this codebase has now paid for three times.
#
# Cache hits never reach `request_json`, so a turn answered from the registry
# spends nothing. What is counted is money, not intent.
#
# The agent's own model calls are deliberately absent: they are already bounded
# by the round cap and by the per-turn token ceiling in the runner, and a third
# counter for the same spend would just be another thing to keep in agreement.
# The research model is counted, because it is reachable through
# `recommend_hotel_areas` without passing any tool budget - it meters itself in
# openai_research.py, since it speaks to the SDK rather than to `request_json`.


@dataclass
class TurnBudget:
    """Paid provider calls one turn may make, counted per provider.

    A provider with no explicit limit gets `default`, so one added later is
    bounded by default rather than exempt by default.
    """

    limits: dict[str, int] = field(default_factory=dict)
    default: int = 50
    used: dict[str, int] = field(default_factory=dict)

    def limit(self, provider: str) -> int:
        return self.limits.get(provider, self.default)

    def left(self, provider: str) -> int:
        return max(0, self.limit(provider) - self.used.get(provider, 0))

    def spend(self, provider: str) -> None:
        allowed = self.limit(provider)
        spent = self.used.get(provider, 0)
        if spent >= allowed:
            raise BudgetExhausted(
                f"this turn has already used its {allowed} {provider} calls",
                provider,
            )
        self.used[provider] = spent + 1

    def as_log(self) -> dict[str, int]:
        return dict(sorted(self.used.items()))


_CURRENT_BUDGET: ContextVar[TurnBudget | None] = ContextVar("turn_budget", default=None)


def current_budget() -> TurnBudget | None:
    """The budget for the turn on this task, or None where nothing meters."""
    return _CURRENT_BUDGET.get()


@contextmanager
def turn_budget(budget: TurnBudget) -> Iterator[TurnBudget]:
    """Meter every paid call made inside this block.

    A ContextVar rather than a parameter threaded through six providers and a
    dozen services: each turn already runs in its own task, so concurrent turns
    count separately without any of them knowing the others exist.

    Outside such a block - scripts, tests, a one-off measurement - there is no
    budget and nothing is limited. Metering is the agent loop's concern, not a
    property of the HTTP layer.
    """
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)


def budget_for(settings: Settings) -> TurnBudget:
    """Ceilings keyed by provider name exactly as passed to `request_json`."""
    return TurnBudget(
        limits={
            "google_places": settings.turn_google_call_budget,
            "google_routes": settings.turn_google_call_budget,
            "google_weather": settings.turn_google_call_budget,
            # Free, and keyless. Counted anyway: a runaway loop is worth
            # stopping whoever is paying for it.
            "open-meteo": settings.turn_google_call_budget,
            "duffel": settings.turn_flight_call_budget,
            "serpapi_google_hotels": settings.turn_hotel_call_budget,
        },
        default=settings.turn_provider_call_budget,
    )


def build_client(timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout or DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


# One HTTP client for the whole process, so the keep-alive pool survives a
# request. A `Toolbox` used to build its own, and a Toolbox is built per
# request - so every button press opened fresh TCP connections and repeated the
# TLS handshake with Google, for nothing. The pool above is sized for a
# process, not for a single call.
#
# Created lazily rather than at import: an AsyncClient wants a running loop,
# and importing this module must not require one.
_SHARED_CLIENT: httpx.AsyncClient | None = None


def shared_client() -> httpx.AsyncClient:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
        _SHARED_CLIENT = build_client()
    return _SHARED_CLIENT


async def close_shared_client() -> None:
    """Called once on application shutdown. Never by a request."""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is not None and not _SHARED_CLIENT.is_closed:
        await _SHARED_CLIENT.aclose()
    _SHARED_CLIENT = None


def _classify(provider: str, response: httpx.Response) -> ProviderError:
    detail = response.text[:400]
    status = response.status_code
    if status in (401, 403):
        return ProviderAuthFailed(
            f"{provider} rejected the credentials (HTTP {status}). Check that the key is "
            f"valid and the API is enabled. {detail}",
            provider,
            status,
        )
    if status == 429:
        return ProviderRateLimited(f"{provider} rate limit hit. {detail}", provider, status)
    if 400 <= status < 500:
        return ProviderBadRequest(f"{provider} rejected the request: {detail}", provider, status)
    return ProviderUnavailable(f"{provider} returned HTTP {status}: {detail}", provider, status)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One HTTP call with bounded retries on transient failures.

    4xx is never retried - a bad field mask will be just as bad the third time.
    """
    # Charged once per logical call, outside the retry loop: retries are already
    # bounded by MAX_ATTEMPTS, and billing a turn three times for one flaky
    # request would exhaust its budget over someone else's outage.
    budget = current_budget()
    if budget is not None:
        budget.spend(provider)

    last: ProviderError | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.request(
                method, url, headers=headers, json=json_body, params=params
            )
        except httpx.TimeoutException as exc:
            last = ProviderTimeout(f"{provider} timed out: {exc}", provider)
        except httpx.HTTPError as exc:
            last = ProviderUnavailable(f"{provider} transport error: {exc}", provider)
        else:
            if response.is_success:
                return response.json()
            error = _classify(provider, response)
            if not error.retryable:
                raise error
            last = error

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(BACKOFF_SECONDS * attempt)

    assert last is not None
    raise last
