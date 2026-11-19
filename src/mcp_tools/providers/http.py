"""Shared HTTP plumbing for provider adapters.

One place decides how a failed call is classified, because the job engine acts on it:
- `ambiguous` (a write whose request may have reached the provider): never resubmit;
- `retryable` (a read that hit a transient problem): poll or download again later;
- content policy refusals become the `moderated` state, which is refunded and reported as such.
Provider text never goes into log lines or error messages unfiltered: it is truncated and
the credentials are registered for redaction.
"""

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2

from mcp_tools.core.logsafe import redact, register_secret
from mcp_tools.providers.base import ProviderError

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
READ_RETRIES = 2
MAX_ERROR_TEXT = 400


def clip(text: Any) -> str:
    return redact(" ".join(str(text).split()))[:MAX_ERROR_TEXT]


def error_message(response: httpx2.Response) -> tuple[str | None, str]:
    """(provider error code, human message) from an error response of either provider's shape."""
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        return None, clip(response.text) or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):  # OpenAI: {"error": {"message", "type", "code"}}
            return (error.get("code") or error.get("type")), clip(error.get("message") or error)
        if "detail" in body or "title" in body:  # Replicate: {"title", "detail", "status"}
            return None, clip(body.get("detail") or body.get("title"))
        if isinstance(error, str):
            return None, clip(error)
    return None, clip(json.dumps(body))


class ProviderHttp:
    """A small client around httpx2 with provider-aware error mapping."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str],
        secret: str | None,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        moderation_codes: frozenset[str] = frozenset(),
    ) -> None:
        register_secret(secret)
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers=dict(headers),
            transport=transport,
            timeout=httpx2.Timeout(timeout, connect=15.0),
            follow_redirects=False,
        )
        self._moderation_codes = moderation_codes

    async def aclose(self) -> None:
        await self._client.aclose()

    def _failure(self, response: httpx2.Response, *, write: bool) -> ProviderError:
        code, message = error_message(response)
        status = response.status_code
        if code in self._moderation_codes:
            return ProviderError("content_policy", message)
        if status in (401, 403):
            return ProviderError("provider_auth", "the provider rejected this server's credentials")
        if status == 402:
            return ProviderError("provider_billing", message)
        if status == 404:
            return ProviderError("provider_not_found", message)
        if status == 429:
            return ProviderError("rate_limited", message, retryable=not write)
        if status >= 500:
            # A 5xx on a write may or may not have created something.
            return ProviderError("provider_error", message, retryable=not write, ambiguous=write)
        return ProviderError(code or "provider_rejected", message)

    async def request(self, method: str, url: str, *, write: bool, **kwargs: Any) -> httpx2.Response:
        """One call. Reads are retried on transient failures; writes are sent exactly once."""
        attempts = 1 if write else 1 + READ_RETRIES
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                response = await self._client.request(method, url, **kwargs)
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                # Nothing was sent: safe to report as a plain, retryable failure even for a write.
                if last:
                    raise ProviderError("network", "could not connect to the provider", retryable=True) from exc
            except httpx2.HTTPError as exc:
                if last:
                    raise ProviderError(
                        "network", "the connection to the provider failed", retryable=not write, ambiguous=write
                    ) from exc
            else:
                if response.status_code < 400:
                    return response
                if write or last or response.status_code not in RETRY_STATUSES:
                    raise self._failure(response, write=write)
            await anyio.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")

    async def json(self, method: str, url: str, *, write: bool, **kwargs: Any) -> dict[str, Any]:
        response = await self.request(method, url, write=write, **kwargs)
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("provider_error", "the provider sent an unreadable answer", ambiguous=write) from exc
        if not isinstance(body, dict):
            raise ProviderError("provider_error", "the provider sent an unexpected answer", ambiguous=write)
        return body

    @asynccontextmanager
    async def stream(self, url: str, **kwargs: Any) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream a provider-hosted file that needs this provider's credentials."""
        try:
            async with self._client.stream("GET", url, **kwargs) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._failure(response, write=False)

                async def chunks() -> AsyncIterator[bytes]:
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    except httpx2.HTTPError as exc:
                        raise ProviderError("network", "the download was interrupted", retryable=True) from exc

                yield chunks()
        except httpx2.HTTPError as exc:
            raise ProviderError("network", "could not download the output", retryable=True) from exc
