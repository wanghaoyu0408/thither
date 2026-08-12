"""Shared plumbing for external providers.

Providers speak HTTP and raise. Services catch and translate into
`ToolResult.error`, so nothing above the service layer ever sees an exception
from a third party - and nothing below it invents a fallback value.
"""

import asyncio
from typing import Any

import httpx

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
