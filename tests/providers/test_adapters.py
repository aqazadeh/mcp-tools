"""Provider adapters against a scripted HTTP transport: the requests they send and how they read answers."""

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mcp_tools.config import Settings
from mcp_tools.core.catalog.loader import load_catalog
from mcp_tools.core.jobs.service import GenerationRequest
from mcp_tools.core.media.fetch import SafeFetcher
from mcp_tools.core.platform import Platform
from mcp_tools.providers.base import MediaInput, OutputRef, ProviderError, ProviderRequest
from mcp_tools.providers.openai import OpenAIProvider
from mcp_tools.providers.replicate import ReplicateProvider
from tests.helpers.media_samples import png_bytes

pytestmark = pytest.mark.anyio
CATALOG = load_catalog()
PNG = png_bytes(32, 32)


class Script:
    """Records requests and answers them from a handler."""

    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.requests: list[httpx2.Request] = []
        self._handler = handler

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        return self._handler(request)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)


def request_for(
    model_id: str, tmp_path: Path, *, medias: list[tuple[str, bytes]] | None = None, **fields: Any
) -> ProviderRequest:
    inputs = []
    for number, (role, data) in enumerate(medias or []):
        path = tmp_path / f"in{number}.png"
        path.write_bytes(data)
        inputs.append(
            MediaInput(role=role, media_id=f"med_{number}", kind="image", mime="image/png", path=path, size=len(data))
        )
    return ProviderRequest(
        job_id="job_1",
        model=CATALOG.get(model_id),
        prompt=fields.pop("prompt", "a fox"),
        aspect_ratio=fields.pop("aspect_ratio", None),
        duration=fields.pop("duration", None),
        params=fields,
        medias=tuple(inputs),
    )


def stream_response(data: bytes) -> httpx2.Response:
    async def chunks():
        yield data

    return httpx2.Response(200, content=chunks())


async def read_output(provider: Any, ref: OutputRef) -> bytes:
    async with provider.open_output(ref) as chunks:
        return b"".join([c async for c in chunks])


def fetcher_for(files: dict[str, bytes]) -> SafeFetcher:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert "authorization" not in request.headers  # the token never leaves the API host
        return stream_response(files[request.url.path])

    return SafeFetcher(resolver=resolve, transport_factory=lambda: httpx2.MockTransport(handler))


# -- Replicate ---------------------------------------------------------------------------------
def replicate(script: Script, files: dict[str, bytes] | None = None) -> ReplicateProvider:
    return ReplicateProvider("r8_secret_token_value", fetcher_for(files or {}), transport=script.transport)


async def test_replicate_creates_a_prediction_on_the_model_endpoint(tmp_path: Path) -> None:
    script = Script(lambda r: httpx2.Response(201, json={"id": "pred1", "status": "starting"}))
    provider = replicate(script)
    submitted = await provider.submit(
        request_for("flux_schnell", tmp_path, aspect_ratio="16:9", output_format="png", seed=7)
    )
    assert (submitted.remote_id, submitted.result) == ("pred1", None)
    (sent,) = script.requests
    assert (sent.method, sent.url.path) == ("POST", "/v1/models/black-forest-labs/flux-schnell/predictions")
    assert sent.headers["authorization"] == "Bearer r8_secret_token_value" and "prefer" not in sent.headers
    assert json.loads(sent.content) == {
        "input": {"prompt": "a fox", "aspect_ratio": "16:9", "output_format": "png", "seed": 7}
    }


async def test_replicate_passes_small_files_inline_and_uploads_large_ones(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/files":
            assert b'name="content"' in request.content and b"image/png" in request.content
            return httpx2.Response(201, json={"id": "f1", "urls": {"get": "https://api.replicate.com/v1/files/f1"}})
        return httpx2.Response(201, json={"id": "pred2"})

    script = Script(handler)
    provider = replicate(script)
    await provider.submit(request_for("flux_kontext_pro", tmp_path, medias=[("image", PNG)], output_format="png"))
    body = json.loads(script.requests[-1].content)["input"]
    assert body["input_image"] == "data:image/png;base64," + base64.b64encode(PNG).decode() and "image" not in body

    big = PNG + b"\x00" * (300 * 1024)
    await provider.submit(request_for("flux_kontext_pro", tmp_path, medias=[("image", big)], output_format="png"))
    assert [r.url.path for r in script.requests[1:]] == [
        "/v1/files",
        "/v1/models/black-forest-labs/flux-kontext-pro/predictions",
    ]
    assert json.loads(script.requests[-1].content)["input"]["input_image"] == "https://api.replicate.com/v1/files/f1"


async def test_replicate_maps_prediction_states() -> None:
    answers: list[dict[str, Any]] = [
        {"status": "starting"},
        {"status": "processing"},
        {
            "status": "succeeded",
            "output": ["https://replicate.delivery/a/out.png", "https://replicate.delivery/a/2.png"],
        },
        {"status": "succeeded", "output": "https://replicate.delivery/a/out.mp4"},
        {"status": "succeeded", "output": None},
        {"status": "failed", "error": "NSFW content detected. Try a different prompt."},
        {"status": "failed", "error": "CUDA out of memory"},
        {"status": "canceled"},
    ]
    script = Script(lambda r: httpx2.Response(200, json={"id": "p", **(answers.pop(0) if answers else {})}))
    provider = replicate(script)
    states = [await provider.poll("p", {}) for _ in range(8)]
    assert [s.state for s in states] == [
        "running", "running", "succeeded", "succeeded", "failed", "moderated", "failed", "canceled",
    ]  # fmt: skip
    assert [o.url for o in states[2].outputs] == [
        "https://replicate.delivery/a/out.png",
        "https://replicate.delivery/a/2.png",
    ]
    assert states[4].error_code == "no_output" and states[6].error_message == "CUDA out of memory"
    assert script.requests[0].url.path == "/v1/predictions/p"

    await provider.cancel("p", {})
    assert (script.requests[-1].method, script.requests[-1].url.path) == ("POST", "/v1/predictions/p/cancel")


@pytest.mark.parametrize(
    ("status", "body", "code", "ambiguous"),
    [
        (500, {"detail": "boom"}, "provider_error", True),
        (422, {"title": "Invalid", "detail": "prompt is required"}, "provider_rejected", False),
        (401, {"detail": "bad token r8_secret_token_value"}, "provider_auth", False),
        (402, {"detail": "billing required"}, "provider_billing", False),
        (429, {"detail": "slow down"}, "rate_limited", False),
    ],
)
async def test_replicate_classifies_create_failures(
    tmp_path: Path, status: int, body: dict[str, str], code: str, ambiguous: bool
) -> None:
    script = Script(lambda r: httpx2.Response(status, json=body))
    with pytest.raises(ProviderError) as caught:
        await replicate(script).submit(request_for("flux_schnell", tmp_path))
    assert (caught.value.code, caught.value.ambiguous) == (code, ambiguous)
    assert "r8_secret_token_value" not in caught.value.message and len(script.requests) == 1  # a write is never retried


async def test_network_failures_are_ambiguous_only_when_the_request_may_have_left(tmp_path: Path) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    def drop(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("no answer")

    with pytest.raises(ProviderError) as not_sent:
        await replicate(Script(refuse)).submit(request_for("flux_schnell", tmp_path))
    with pytest.raises(ProviderError) as maybe_sent:
        await replicate(Script(drop)).submit(request_for("flux_schnell", tmp_path))
    assert (not_sent.value.ambiguous, maybe_sent.value.ambiguous) == (False, True)

    flaky = [httpx2.Response(503), httpx2.Response(200, json={"id": "p", "status": "processing"})]
    script = Script(lambda r: flaky.pop(0))
    assert (await replicate(script).poll("p", {})).state == "running" and len(script.requests) == 2  # reads are retried
    with pytest.raises(ProviderError) as gone:
        await replicate(Script(lambda r: httpx2.Response(404, json={"detail": "not found"}))).poll("p", {})
    assert gone.value.retryable is False


async def test_replicate_downloads_only_from_its_own_hosts() -> None:
    def api(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        return stream_response(b"from-api")

    provider = replicate(Script(api), {"/a/out.png": b"from-cdn"})
    assert await read_output(provider, OutputRef(url="https://replicate.delivery/a/out.png")) == b"from-cdn"
    assert await read_output(provider, OutputRef(url="https://xyz.replicate.delivery/a/out.png")) == b"from-cdn"
    assert await read_output(provider, OutputRef(url="https://api.replicate.com/v1/files/f1/download")) == b"from-api"
    assert await read_output(provider, OutputRef(url="data:image/png;base64," + base64.b64encode(PNG).decode())) == PNG
    for url in (
        "https://evil.test/out.png",
        "https://replicate.delivery.evil.test/x",
        "http://replicate.delivery/a/out.png",
    ):
        with pytest.raises(ProviderError) as refused:
            await read_output(provider, OutputRef(url=url))
        assert refused.value.code == "output_unavailable" and refused.value.retryable is False


# -- OpenAI ------------------------------------------------------------------------------------
def openai(script: Script) -> OpenAIProvider:
    return OpenAIProvider("sk-secret-key-value", project="proj_1", transport=script.transport)


async def test_openai_image_generation_is_synchronous_json(tmp_path: Path) -> None:
    script = Script(lambda r: httpx2.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}))
    provider = openai(script)
    submitted = await provider.submit(
        request_for("gpt_image_2", tmp_path, aspect_ratio="3:2", quality="high", output_format="png")
    )
    (sent,) = script.requests
    assert sent.url.path == "/v1/images/generations" and sent.headers["openai-project"] == "proj_1"
    assert json.loads(sent.content) == {
        "model": "gpt-image-2", "prompt": "a fox", "size": "1536x1024",
        "quality": "high", "output_format": "png", "n": 1,
    }  # fmt: skip
    assert submitted.remote_id is None and submitted.result is not None and submitted.result.state == "succeeded"
    ref = submitted.result.outputs[0]
    assert await read_output(provider, ref) == PNG
    with pytest.raises(ProviderError):  # handed over once, then gone from memory
        await read_output(provider, ref)


async def test_openai_image_edit_sends_reference_files_as_multipart(tmp_path: Path) -> None:
    script = Script(lambda r: httpx2.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}))
    await openai(script).submit(
        request_for(
            "gpt_image_2", tmp_path, medias=[("image", PNG), ("image", PNG)], quality="medium", output_format="png"
        )
    )
    (sent,) = script.requests
    assert sent.url.path == "/v1/images/edits" and sent.headers["content-type"].startswith("multipart/form-data")
    assert sent.content.count(b'name="image[]"') == 2 and b'name="prompt"' in sent.content
    assert b'name="model"\r\n\r\ngpt-image-2' in sent.content


async def test_openai_speech_returns_audio_bytes(tmp_path: Path) -> None:
    script = Script(lambda r: httpx2.Response(200, content=b"ID3-audio"))
    provider = openai(script)
    submitted = await provider.submit(
        request_for("gpt_4o_mini_tts", tmp_path, prompt="Hello there", voice="coral", response_format="mp3", speed=1.0)
    )
    (sent,) = script.requests
    assert sent.url.path == "/v1/audio/speech"
    assert json.loads(sent.content) == {
        "model": "gpt-4o-mini-tts", "input": "Hello there", "voice": "coral", "response_format": "mp3", "speed": 1.0,
    }  # fmt: skip
    assert submitted.result is not None and await read_output(provider, submitted.result.outputs[0]) == b"ID3-audio"


async def test_openai_video_is_asynchronous_multipart(tmp_path: Path) -> None:
    answers: list[httpx2.Response] = [
        httpx2.Response(200, json={"id": "video_1", "status": "queued"}),
        httpx2.Response(200, json={"id": "video_1", "status": "in_progress", "progress": 40}),
        httpx2.Response(200, json={"id": "video_1", "status": "completed", "progress": 100}),
        stream_response(b"mp4-bytes"),
        httpx2.Response(200, json={"status": "failed", "error": {"code": "moderation_blocked", "message": "no"}}),
        httpx2.Response(200, json={"status": "failed", "error": {"code": "internal", "message": "oops"}}),
    ]
    script = Script(lambda r: answers.pop(0))
    provider = openai(script)
    submitted = await provider.submit(
        request_for("sora_2", tmp_path, aspect_ratio="9:16", duration=8, medias=[("start_image", PNG)])
    )
    created = script.requests[0]
    assert created.url.path == "/v1/videos" and created.headers["content-type"].startswith("multipart/form-data")
    assert b'name="seconds"\r\n\r\n8\r\n' in created.content and b'name="size"\r\n\r\n720x1280' in created.content
    assert b'name="model"\r\n\r\nsora-2' in created.content and b'name="input_reference"; filename=' in created.content
    assert b'name="duration"' not in created.content and submitted.remote_id == "video_1"

    running = await provider.poll("video_1", {})
    done = await provider.poll("video_1", {})
    assert (running.state, running.progress, done.state) == ("running", 0.4, "succeeded")
    assert await read_output(provider, done.outputs[0]) == b"mp4-bytes"
    assert script.requests[-1].url.path == "/v1/videos/video_1/content"
    assert (await provider.poll("video_1", {})).state == "moderated"
    assert (await provider.poll("video_1", {})).state == "failed"


async def test_openai_content_refusals_become_moderated(tmp_path: Path) -> None:
    refusal = {"error": {"message": "Your request was rejected by the safety system.", "code": "moderation_blocked"}}
    with pytest.raises(ProviderError) as caught:
        await openai(Script(lambda r: httpx2.Response(400, json=refusal))).submit(request_for("gpt_image_2", tmp_path))
    assert caught.value.code == "content_policy" and not caught.value.ambiguous
    with pytest.raises(ProviderError) as plain:
        bad = {"error": {"message": "Unknown parameter", "code": "unknown_parameter"}}
        await openai(Script(lambda r: httpx2.Response(400, json=bad))).submit(request_for("gpt_image_2", tmp_path))
    assert plain.value.code == "unknown_parameter"


# -- through the whole pipeline ------------------------------------------------------------------
async def test_a_real_adapter_runs_through_the_job_engine(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if b"forbidden" in request.content:
            return httpx2.Response(400, json={"error": {"message": "blocked", "code": "moderation_blocked"}})
        return httpx2.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes(48, 32)).decode()}]})

    script = Script(handler)
    settings = Settings(data_dir=tmp_path / "data", log_level="WARNING", initial_credits=1_000)
    platform = Platform(settings, providers=[OpenAIProvider("sk-secret-key-value", transport=script.transport)])
    async with platform.running():
        assert platform.catalog.public(platform.catalog.get("gpt_image_2"))["available"] is True

        async def run(prompt: str) -> Any:
            outcome = await platform.jobs.create(
                [GenerationRequest(0, {"model": "gpt_image_2", "prompt": prompt, "quality": "low"})],
                principal=platform.principal,
                tool="generate_image",
                output_type="image",
            )
            waited = await platform.jobs.wait(
                [j.job_id for j in outcome.jobs], timeout=10, principal=platform.principal
            )
            return outcome, waited.jobs[0]

        outcome, job = await run("a fox")
        assert outcome.credits_total == 14 and job.status == "completed"
        assert (job.results[0].width, job.results[0].height, job.results[0].mime) == (48, 32, "image/png")

        _, refused = await run("something forbidden")
        assert refused.status == "moderated" and refused.error == {"code": "content_policy", "message": "blocked"}
        async with platform.store.read_tx() as conn:
            assert (await platform.ledger.balance(conn, "ws_local")).balance == 1_000 - 14
