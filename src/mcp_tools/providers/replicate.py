"""Replicate adapter: predictions API over plain HTTPS.

Wire contract taken from the official client's source (replicate 2.0.0b4 / 1.0.7):
  POST /v1/models/{owner}/{name}/predictions   {"input": {...}}             official models
  POST /v1/predictions                         {"version": ..., "input"}    versioned models
  GET  /v1/predictions/{id}        POST /v1/predictions/{id}/cancel
  POST /v1/files  (multipart part "content")  -> urls.get, passed as an input value
Statuses: starting | processing | succeeded | failed | canceled.
`Prefer: wait` is deliberately not used: a create call must return quickly so the remote id
is saved before anything else can go wrong.
"""

import base64
import binascii
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx2

from mcp_tools.core.catalog.loader import Availability
from mcp_tools.core.errors import ApiError
from mcp_tools.core.media.fetch import SafeFetcher
from mcp_tools.providers.base import (
    JsonDict,
    MediaInput,
    OutputRef,
    PollResult,
    ProviderError,
    ProviderRequest,
    Submitted,
    provider_input,
)
from mcp_tools.providers.http import ProviderHttp, clip

BASE_URL = "https://api.replicate.com/v1"
API_HOST = "api.replicate.com"
OUTPUT_HOSTS = ("replicate.delivery",)
INLINE_LIMIT = 256 * 1024  # the API's guidance: data URLs up to 256 KB, uploaded files above
MAX_OUTPUT_BYTES = 600 * 1024 * 1024
_VERSION_ID = re.compile(r"^[0-9a-f]{64}$")
_MODERATION_HINTS = ("nsfw", "safety", "content policy", "content moderation", "flagged", "sensitive")


class ReplicateProvider:
    name = "replicate"

    def __init__(
        self,
        token: str | None,
        fetcher: SafeFetcher,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._token = token
        self._fetcher = fetcher
        self._http = ProviderHttp(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token or ''}", "Accept": "application/json"},
            secret=token,
            transport=transport,
        )

    def availability(self) -> Availability:
        if not self._token:
            return Availability(False, "REPLICATE_API_TOKEN is not set")
        return Availability(True)

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- inputs ------------------------------------------------------------------------------
    async def _file_value(self, media: MediaInput) -> str:
        if media.size <= INLINE_LIMIT:
            data = await anyio.Path(media.path).read_bytes()
            return f"data:{media.mime};base64,{base64.b64encode(data).decode('ascii')}"
        try:
            with media.path.open("rb") as handle:
                body = await self._http.json(
                    "POST",
                    "/files",
                    write=True,
                    files={"content": (media.path.name, handle, media.mime)},
                    timeout=300.0,
                )
        except ProviderError as exc:
            # Nothing was generated yet, so an upload failure is never an ambiguous submit.
            raise ProviderError(exc.code, exc.message) from exc
        url = (body.get("urls") or {}).get("get")
        if not isinstance(url, str):
            raise ProviderError("provider_error", "the provider did not return a file URL")
        return url

    # -- contract ----------------------------------------------------------------------------
    async def submit(self, request: ProviderRequest) -> Submitted:
        files = {m.media_id: await self._file_value(m) for m in request.medias}
        body: JsonDict = {"input": provider_input(request, files)}
        ref = request.model.provider_ref
        owner_name, _, version = ref.partition(":")
        if version or _VERSION_ID.match(ref):
            body["version"] = ref
            path = "/predictions"
        else:
            path = f"/models/{owner_name}/predictions"
        prediction = await self._http.json("POST", path, write=True, json=body)
        remote_id = prediction.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise ProviderError("provider_error", "the provider accepted the request without an id", ambiguous=True)
        return Submitted(remote_id=remote_id, state={}, retry_after=1.0)

    async def poll(self, remote_id: str, state: JsonDict) -> PollResult:
        prediction = await self._http.json("GET", f"/predictions/{remote_id}", write=False)
        status = prediction.get("status")
        if status in ("starting", "processing"):
            return PollResult("running")
        if status == "succeeded":
            urls = _output_urls(prediction.get("output"))
            if not urls:
                return PollResult("failed", error_code="no_output", error_message="the model returned no file")
            return PollResult("succeeded", progress=1.0, outputs=tuple(OutputRef(url=u) for u in urls))
        if status == "canceled":
            return PollResult("canceled", error_code="canceled", error_message="canceled at the provider")
        message = clip(prediction.get("error") or "the model run failed")
        if any(hint in message.lower() for hint in _MODERATION_HINTS):
            return PollResult("moderated", error_code="content_policy", error_message=message)
        return PollResult("failed", error_code="provider_error", error_message=message)

    async def cancel(self, remote_id: str, state: JsonDict) -> None:
        await self._http.request("POST", f"/predictions/{remote_id}/cancel", write=True)

    @asynccontextmanager
    async def open_output(self, ref: OutputRef) -> AsyncIterator[AsyncIterator[bytes]]:
        url = ref.url or ""
        if url.startswith("data:"):
            try:
                data = base64.b64decode(url.split(",", 1)[1], validate=False)
            except (IndexError, binascii.Error, ValueError) as exc:
                raise ProviderError("invalid_output", "the provider returned an unreadable inline file") from exc

            async def once() -> AsyncIterator[bytes]:
                yield data

            yield once()
            return
        if urlsplit(url).hostname == API_HOST:
            # Files on the API host need this account's token; the token is sent to that host only.
            async with self._http.stream(url) as chunks:
                yield chunks
            return
        try:
            async with self._fetcher.stream(url, max_bytes=MAX_OUTPUT_BYTES, allowed_hosts=OUTPUT_HOSTS) as chunks:
                yield chunks
        except ApiError as exc:
            # Output files are deleted about an hour after the run: a refusal here is final.
            raise ProviderError(
                "output_unavailable", exc.message, retryable="could not be fetched" in exc.message
            ) from exc

    # -- diagnostics -------------------------------------------------------------------------
    async def describe_model(self, provider_ref: str) -> JsonDict:
        """The model's input schema as the provider publishes it (for `mcp-tools doctor --live`)."""
        owner_name = provider_ref.partition(":")[0]
        model = await self._http.json("GET", f"/models/{owner_name}", write=False)
        schema = ((model.get("latest_version") or {}).get("openapi_schema") or {}).get("components", {})
        properties = (schema.get("schemas", {}).get("Input") or {}).get("properties") or {}
        return {"inputs": sorted(properties), "official": bool(model.get("is_official"))}


def _output_urls(output: Any) -> list[str]:
    """Outputs are untyped: a URL, a list of URLs, or an object whose values are URLs."""
    if isinstance(output, str):
        return [output] if output.startswith(("https://", "data:")) else []
    if isinstance(output, list):
        return [u for item in output for u in _output_urls(item)]
    if isinstance(output, dict):
        return [u for item in output.values() for u in _output_urls(item)]
    return []
