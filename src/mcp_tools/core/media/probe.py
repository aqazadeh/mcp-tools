"""Read dimensions and duration, and make thumbnails. Blocking: call through a worker thread.

Parsers are exposed to hostile files, so limits are explicit rather than library defaults:
Pillow may only try the formats on our allowlist (never EPS, which shells out), pixel
counts are checked before decoding, decompression-bomb warnings are errors, and ffprobe
(optional) runs with a protocol whitelist, an argv list and a timeout.
"""

import io
import json
import shutil
import subprocess
import warnings
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

IMAGE_FORMATS = ["PNG", "JPEG", "GIF", "WEBP"]
MAX_IMAGE_PIXELS = 64_000_000
MAX_IMAGE_SIDE = 16_384
PREVIEW_MAX_SIDE = 512
PREVIEW_MAX_BYTES = 100_000
_FFPROBE_TIMEOUT_S = 10


class ProbeError(ValueError):
    """The file is not a usable image / audio / video of the sniffed type."""


@dataclass(frozen=True)
class Probe:
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


def _open_image(path: Path) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(path, formats=IMAGE_FORMATS)
        except Exception as exc:
            raise ProbeError("not a readable PNG, JPEG, GIF or WebP image") from exc
    width, height = image.size
    if width < 1 or height < 1 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        image.close()
        raise ProbeError(f"image side must be 1..{MAX_IMAGE_SIDE} px, got {width}x{height}")
    if width * height > MAX_IMAGE_PIXELS:
        image.close()
        raise ProbeError(f"image has more than {MAX_IMAGE_PIXELS // 1_000_000} megapixels")
    return image


def probe_image(path: Path) -> Probe:
    with _open_image(path) as image:
        return Probe(width=image.size[0], height=image.size[1])


def probe_wav(path: Path) -> Probe:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (wave.Error, EOFError) as exc:
        raise ProbeError("not a readable WAV file") from exc
    return Probe(duration_ms=int(frames * 1000 / rate) if rate else None)


@lru_cache(maxsize=1)
def _ffprobe_binary() -> str | None:
    found = shutil.which("ffprobe")
    return str(Path(found).resolve()) if found else None


def _ffprobe(path: Path) -> Probe:
    binary = _ffprobe_binary()
    if binary is None:
        return Probe()
    argv = [
        binary,
        "-v", "error",
        "-protocol_whitelist", "file",
        "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json",
        str(path.resolve()),
    ]  # fmt: skip
    try:
        done = subprocess.run(  # noqa: S603 - absolute binary, argv list, server-generated path
            argv, capture_output=True, timeout=_FFPROBE_TIMEOUT_S, stdin=subprocess.DEVNULL, check=False
        )
        info = json.loads(done.stdout[:1_000_000] or b"{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return Probe()
    width = height = duration_ms = None
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and isinstance(stream.get("width"), int):
            width, height = stream["width"], stream.get("height")
            break
    try:
        duration_ms = int(float(info["format"]["duration"]) * 1000)
    except (KeyError, TypeError, ValueError):
        duration_ms = None
    return Probe(width=width, height=height, duration_ms=duration_ms)


def probe(path: Path, kind: str, ext: str) -> Probe:
    """Best-effort metadata. Images must decode; audio/video metadata is optional."""
    if kind == "image":
        return probe_image(path)
    if ext == "wav":
        return probe_wav(path)
    return _ffprobe(path)


def make_preview(path: Path, *, max_side: int = PREVIEW_MAX_SIDE, max_bytes: int = PREVIEW_MAX_BYTES) -> bytes:
    """A small JPEG of the first frame, for the model (or a widget) to look at."""
    with _open_image(path) as image:
        image.seek(0)
        frame = image.convert("RGBA")
    background = Image.new("RGBA", frame.size, (255, 255, 255, 255))
    flattened = Image.alpha_composite(background, frame).convert("RGB")
    flattened.thumbnail((max_side, max_side))
    for quality in (82, 70, 55, 40, 28):
        buffer = io.BytesIO()
        flattened.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= max_bytes:
            return buffer.getvalue()
        flattened.thumbnail((max(64, flattened.width * 3 // 4), max(64, flattened.height * 3 // 4)))
    return buffer.getvalue()
