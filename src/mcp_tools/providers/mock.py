"""Keyless mock provider.

Follows the real contract (submit -> remote handle -> poll -> fetch output) so the job
engine is exercised exactly as with a real provider. Everything is derived from the remote
state and the clock, so any process can poll a job another process submitted. Outputs are
real, small media files rendered locally.
"""

import io
import math
import random
import secrets
import struct
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources

import anyio
from PIL import Image, ImageChops, ImageDraw

from mcp_tools.core.catalog.loader import Availability
from mcp_tools.core.clock import Clock
from mcp_tools.providers.base import (
    JsonDict,
    OutputRef,
    PollResult,
    ProviderError,
    ProviderRequest,
    Submitted,
)

_SIZES = {"1k": 512, "2k": 1024}
_DEFAULT_DELAYS_MS = {"image": 800, "video": 2500, "audio": 800}
_SLOW_MS = 60_000
_CHUNK = 64 * 1024


def _dimensions(aspect_ratio: str | None, long_side: int) -> tuple[int, int]:
    try:
        w, h = (float(x) for x in (aspect_ratio or "1:1").split(":"))
    except ValueError:
        w = h = 1.0
    if w >= h:
        return long_side, max(16, round(long_side * h / w / 2) * 2)
    return max(16, round(long_side * w / h / 2) * 2), long_side


def _render_image(spec: JsonDict) -> bytes:
    """A deterministic placeholder drawn with Pillow primitives (fast at any size)."""
    width, height = spec["width"], spec["height"]
    seed = int(spec.get("seed") or 0)
    style = spec.get("style", "gradient")
    r0, g0, b0 = (seed * 53) % 200, (seed * 97) % 200, (seed * 151) % 200
    if style == "noise":
        noise = Image.frombytes("L", (width, height), random.Random(seed).randbytes(width * height))  # noqa: S311
        image = Image.merge(
            "RGB",
            (noise, noise.point([(v * 3) % 256 for v in range(256)]), noise.point([(v * 7) % 256 for v in range(256)])),
        )
    elif style == "checker":
        image = Image.new("RGB", (width, height), (r0, g0, b0))
        draw = ImageDraw.Draw(image)
        for y in range(0, height, 32):
            for x in range((y // 32 % 2) * 32, width, 64):
                draw.rectangle([x, y, x + 31, y + 31], fill=(r0 + 55, g0 + 55, b0 + 55))
    else:
        vertical = Image.linear_gradient("L").resize((width, height))
        horizontal = Image.linear_gradient("L").rotate(90).resize((width, height))
        image = Image.merge(
            "RGB",
            (
                horizontal.point([r0 + v * 55 // 255 for v in range(256)]),
                vertical.point([g0 + v * 55 // 255 for v in range(256)]),
                Image.new("L", (width, height), b0 + 27),
            ),
        )
    draw = ImageDraw.Draw(image)
    label = (spec.get("prompt") or "mock")[:60]
    draw.rectangle([8, 8, width - 8, 40], fill=(0, 0, 0))
    draw.text((14, 16), f"mock - {label}", fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_audio(spec: JsonDict) -> bytes:
    seconds = float(spec.get("duration") or 3)
    rate = 16_000
    frequency = 220.0 if spec.get("voice") == "tenor" else 330.0
    frames = bytearray()
    for n in range(int(seconds * rate)):
        envelope = min(1.0, n / 800, (seconds * rate - n) / 800)
        frames += struct.pack("<h", int(9000 * envelope * math.sin(2 * math.pi * frequency * n / rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def _render_video(_spec: JsonDict) -> bytes:
    return (resources.files("mcp_tools") / "data" / "mock" / "sample.mp4").read_bytes()


def _render_upscale(spec: JsonDict) -> bytes:
    with Image.open(spec["input_path"], formats=["PNG", "JPEG", "GIF", "WEBP"]) as source:
        scale = int(spec.get("scale") or 2)
        resized = source.convert("RGBA").resize((source.width * scale, source.height * scale))
    buffer = io.BytesIO()
    resized.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_cutout(spec: JsonDict) -> bytes:
    """Make near-white pixels transparent (a stand-in for a matting model)."""
    with Image.open(spec["input_path"], formats=["PNG", "JPEG", "GIF", "WEBP"]) as source:
        rgba = source.convert("RGBA")
    red, green, blue, alpha = rgba.split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    keep = darkest.point([0 if v > 235 else 255 for v in range(256)])
    rgba.putalpha(ImageChops.multiply(alpha, keep))
    buffer = io.BytesIO()
    rgba.save(buffer, format="PNG")
    return buffer.getvalue()


_RENDERERS = {
    "image": _render_image,
    "audio": _render_audio,
    "video": _render_video,
    "upscale": _render_upscale,
    "remove_background": _render_cutout,
}


class MockProvider:
    name = "mock"

    def __init__(self, clock: Clock, *, delays_ms: dict[str, int] | None = None) -> None:
        self._clock = clock
        self._delays_ms = {**_DEFAULT_DELAYS_MS, **(delays_ms or {})}

    def availability(self) -> Availability:
        return Availability(True)

    def _spec(self, request: ProviderRequest) -> JsonDict:
        model = request.model
        spec: JsonDict = {"render": model.output_type, "prompt": request.prompt, **request.params}
        if model.task != "generate":
            source = next((m for m in request.medias if m.role == "image"), None)
            if source is None:
                raise ProviderError("invalid_input", "an input image is required")
            spec.update(render=model.task, input_path=str(source.path))
        elif model.output_type == "image":
            long_side = _SIZES.get(str(request.params.get("resolution", "1k")), 512)
            width, height = _dimensions(request.aspect_ratio, long_side)
            spec.update(width=width, height=height, references=len(request.medias))
        else:
            spec["duration"] = request.duration
        return spec

    async def submit(self, request: ProviderRequest) -> Submitted:
        outcome = str(request.params.get("simulate", "success"))
        delay = _SLOW_MS if outcome == "slow" else self._delays_ms.get(request.model.output_type, 800)
        state: JsonDict = {
            "ready_at_ms": self._clock.now_ms() + delay,
            "submitted_at_ms": self._clock.now_ms(),
            "outcome": "success" if outcome == "slow" else outcome,
            "spec": self._spec(request),
        }
        return Submitted(remote_id=f"mock-{secrets.token_hex(8)}", state=state, retry_after=delay / 1000)

    async def poll(self, remote_id: str, state: JsonDict) -> PollResult:
        now = self._clock.now_ms()
        ready_at = int(state.get("ready_at_ms", 0))
        if now < ready_at:
            started = int(state.get("submitted_at_ms", ready_at))
            total = max(1, ready_at - started)
            return PollResult(
                "running",
                progress=round(min(0.99, (now - started) / total), 2),
                retry_after=(ready_at - now) / 1000,
            )
        outcome = state.get("outcome", "success")
        if outcome == "fail":
            return PollResult("failed", error_code="provider_error", error_message="simulated provider failure")
        if outcome == "moderated":
            return PollResult(
                "moderated", error_code="content_policy", error_message="simulated content-policy rejection"
            )
        return PollResult("succeeded", progress=1.0, outputs=(OutputRef(state=dict(state.get("spec", {}))),))

    async def cancel(self, remote_id: str, state: JsonDict) -> None:
        return None

    @asynccontextmanager
    async def open_output(self, ref: OutputRef) -> AsyncIterator[AsyncIterator[bytes]]:
        renderer = _RENDERERS.get(str(ref.state.get("render")))
        if renderer is None:
            raise ProviderError("invalid_output", "the mock output has no renderer")
        if "input_path" in ref.state and not await anyio.Path(ref.state["input_path"]).is_file():
            raise ProviderError("output_unavailable", "the input file is gone")
        data = await anyio.to_thread.run_sync(renderer, ref.state)

        async def chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(data), _CHUNK):
                yield data[start : start + _CHUNK]

        yield chunks()

    async def aclose(self) -> None:
        return None
