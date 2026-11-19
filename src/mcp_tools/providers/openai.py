"""OpenAI adapter: images, speech and video over plain HTTPS.

Wire contract taken from the official client's source (openai 3.55.1):
  POST /v1/images/generations  JSON       -> data[0].b64_json            (synchronous)
  POST /v1/images/edits        multipart, reference files as "image[]"   (synchronous)
  POST /v1/audio/speech        JSON       -> raw audio bytes             (synchronous)
  POST /v1/videos              multipart  -> {id, status}                (asynchronous)
  GET  /v1/videos/{id}         status queued | in_progress | completed | failed, progress 0-100
  GET  /v1/videos/{id}/content -> MP4 bytes
The video API has no cancel call; a job cancelled here simply stops being polled.
"""

import base64
import binascii
import secrets
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from typing import Any

import httpx2

from mcp_tools.core.catalog.loader import Availability
from mcp_tools.providers.base import (
    JsonDict,
    OutputRef,
    PollResult,
    ProviderError,
    ProviderRequest,
    Submitted,
    provider_input,
)
from mcp_tools.providers.http import ProviderHttp, clip

BASE_URL = "https://api.openai.com/v1"
# Which error codes mean "refused for content" is not typed in the API description; these are the
# names it uses elsewhere. Anything else is reported as a plain failure (also refunded).
MODERATION_CODES = frozenset(
    {"moderation_blocked", "content_policy_violation", "image_content_policy_violation", "content_filter"}
)
IMAGE_SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}
VIDEO_SIZES = {"16:9": "1280x720", "9:16": "720x1280"}
_CHUNK = 256 * 1024
_MAX_INLINE_RESULTS = 32


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str | None,
        *,
        organization: str | None = None,
        project: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._api_key = api_key
        headers = {"Authorization": f"Bearer {api_key or ''}"}
        if organization:
            headers["OpenAI-Organization"] = organization
        if project:
            headers["OpenAI-Project"] = project
        self._http = ProviderHttp(
            base_url=base_url,
            headers=headers,
            secret=api_key,
            transport=transport,
            timeout=300.0,
            moderation_codes=MODERATION_CODES,
        )
        self._results: dict[str, bytes] = {}  # finished synchronous outputs waiting to be stored

    def availability(self) -> Availability:
        if not self._api_key:
            return Availability(False, "OPENAI_API_KEY is not set")
        return Availability(True)

    async def aclose(self) -> None:
        self._results.clear()
        await self._http.aclose()

    def _finished(self, data: bytes) -> Submitted:
        if len(self._results) >= _MAX_INLINE_RESULTS:
            self._results.pop(next(iter(self._results)))
        token = secrets.token_hex(8)
        self._results[token] = data
        return Submitted(
            remote_id=None,
            result=PollResult("succeeded", progress=1.0, outputs=(OutputRef(state={"inline": token}),)),
        )

    # -- submit ------------------------------------------------------------------------------
    async def submit(self, request: ProviderRequest) -> Submitted:
        kind = request.model.output_type
        if kind == "image":
            return await self._image(request)
        if kind == "audio":
            return await self._speech(request)
        return await self._video(request)

    def _fields(self, request: ProviderRequest, sizes: dict[str, str]) -> JsonDict:
        """Provider fields without files; the aspect ratio becomes one of the API's sizes."""
        bare = ProviderRequest(
            job_id=request.job_id,
            model=request.model,
            prompt=request.prompt,
            aspect_ratio=None,
            duration=request.duration,
            params=request.params,
        )
        fields = provider_input(bare, {})
        fields["model"] = request.model.provider_ref
        if request.aspect_ratio:
            size = sizes.get(request.aspect_ratio)
            if size is None:
                raise ProviderError("invalid_input", f"aspect ratio {request.aspect_ratio} has no size for this model")
            fields["size"] = size
        return fields

    async def _image(self, request: ProviderRequest) -> Submitted:
        fields = self._fields(request, IMAGE_SIZES)
        fields["n"] = 1
        if not request.medias:
            body = await self._http.json("POST", "/images/generations", write=True, json=fields)
        else:
            with ExitStack() as stack:
                files = [
                    ("image[]", (m.path.name, stack.enter_context(m.path.open("rb")), m.mime)) for m in request.medias
                ]
                form = {name: str(value) for name, value in fields.items()}
                body = await self._http.json("POST", "/images/edits", write=True, data=form, files=files)
        items = body.get("data")
        encoded = items[0].get("b64_json") if isinstance(items, list) and items and isinstance(items[0], dict) else None
        if not isinstance(encoded, str):
            raise ProviderError("no_output", "the provider returned no image")
        try:
            return self._finished(base64.b64decode(encoded, validate=False))
        except (binascii.Error, ValueError) as exc:
            raise ProviderError("invalid_output", "the provider returned an unreadable image") from exc

    async def _speech(self, request: ProviderRequest) -> Submitted:
        fields = self._fields(request, {})
        fields["input"] = fields.pop("prompt", request.prompt or "")
        fields.setdefault("response_format", "mp3")
        response = await self._http.request(
            "POST", "/audio/speech", write=True, json=fields, headers={"Accept": "application/octet-stream"}
        )
        if not response.content:
            raise ProviderError("no_output", "the provider returned no audio")
        return self._finished(response.content)

    async def _video(self, request: ProviderRequest) -> Submitted:
        fields = self._fields(request, VIDEO_SIZES)
        if "seconds" in fields:
            fields["seconds"] = str(fields["seconds"])  # the API takes "4" | "8" | "12" as strings
        # Always multipart, even without a file: (None, value) parts force that encoding.
        parts: list[tuple[str, Any]] = [(name, (None, str(value))) for name, value in fields.items()]
        with ExitStack() as stack:
            for media in request.medias[:1]:
                handle = stack.enter_context(media.path.open("rb"))
                parts.append(("input_reference", (media.path.name, handle, media.mime)))
            video = await self._http.json("POST", "/videos", write=True, files=parts)
        remote_id = video.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise ProviderError("provider_error", "the provider accepted the request without an id", ambiguous=True)
        return Submitted(remote_id=remote_id, state={}, retry_after=5.0)

    # -- poll / cancel / output --------------------------------------------------------------
    async def poll(self, remote_id: str, state: JsonDict) -> PollResult:
        video = await self._http.json("GET", f"/videos/{remote_id}", write=False)
        status = video.get("status")
        if status in ("queued", "in_progress"):
            progress = video.get("progress")
            fraction = min(0.99, max(0.0, progress / 100)) if isinstance(progress, (int, float)) else None
            return PollResult("running", progress=fraction, retry_after=5.0)
        if status == "completed":
            return PollResult("succeeded", progress=1.0, outputs=(OutputRef(state={"video_id": remote_id}),))
        raw_error = video.get("error")
        error: JsonDict = raw_error if isinstance(raw_error, dict) else {}
        code = str(error.get("code") or error.get("type") or "")
        message = clip(error.get("message") or "the video could not be generated")
        if code in MODERATION_CODES or "moderation" in code or "policy" in code:
            return PollResult("moderated", error_code="content_policy", error_message=message)
        return PollResult("failed", error_code="provider_error", error_message=message)

    async def cancel(self, remote_id: str, state: JsonDict) -> None:
        return None

    @asynccontextmanager
    async def open_output(self, ref: OutputRef) -> AsyncIterator[AsyncIterator[bytes]]:
        token = ref.state.get("inline")
        if token is not None:
            data = self._results.pop(str(token), None)
            if data is None:
                raise ProviderError("output_unavailable", "the finished result is no longer in memory")

            async def chunks() -> AsyncIterator[bytes]:
                for start in range(0, len(data), _CHUNK):
                    yield data[start : start + _CHUNK]

            yield chunks()
            return
        video_id = ref.state.get("video_id")
        if not video_id:
            raise ProviderError("invalid_output", "the provider result has no file")
        async with self._http.stream(f"/videos/{video_id}/content", headers={"Accept": "application/binary"}) as stream:
            yield stream

    # -- diagnostics -------------------------------------------------------------------------
    async def describe_model(self, provider_ref: str) -> JsonDict:
        model = await self._http.json("GET", f"/models/{provider_ref}", write=False)
        return {"id": model.get("id"), "owned_by": model.get("owned_by")}
