"""Decide what a file is from its first bytes.

The stored extension and MIME type always come from here, never from a file name or a
declared Content-Type. Only image, video and audio containers on the allowlist are
accepted; SVG, HTML, XML, PDF, archives and everything else are refused.
"""

from dataclasses import dataclass
from typing import Literal

MediaKind = Literal["image", "video", "audio"]

SNIFF_BYTES = 64 * 1024


@dataclass(frozen=True)
class Sniffed:
    kind: MediaKind
    mime: str
    ext: str


_PNG = Sniffed("image", "image/png", "png")
_JPEG = Sniffed("image", "image/jpeg", "jpg")
_GIF = Sniffed("image", "image/gif", "gif")
_WEBP = Sniffed("image", "image/webp", "webp")
_MP4 = Sniffed("video", "video/mp4", "mp4")
_MOV = Sniffed("video", "video/quicktime", "mov")
_WEBM = Sniffed("video", "video/webm", "webm")
_WAV = Sniffed("audio", "audio/wav", "wav")
_MP3 = Sniffed("audio", "audio/mpeg", "mp3")
_OGG = Sniffed("audio", "audio/ogg", "ogg")
_FLAC = Sniffed("audio", "audio/flac", "flac")
_M4A = Sniffed("audio", "audio/mp4", "m4a")

_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"F4A "}
_QUICKTIME_BRANDS = {b"qt  "}
_MP4_BRAND_PREFIXES = (b"isom", b"iso", b"mp4", b"avc", b"dash", b"M4V", b"3gp", b"mmp4", b"MSNV", b"f4v")


def _iso_bmff(head: bytes) -> Sniffed | None:
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    brand = head[8:12]
    if brand in _AUDIO_BRANDS:
        return _M4A
    if brand in _QUICKTIME_BRANDS:
        return _MOV
    if brand.startswith(_MP4_BRAND_PREFIXES):
        return _MP4
    return None


def _mp3(head: bytes) -> bool:
    if head[:3] == b"ID3":
        return True
    # MPEG audio frame sync: 11 set bits, a valid layer and a valid bitrate/sample-rate index
    if len(head) >= 4 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        layer = (head[1] >> 1) & 0x3
        bitrate = (head[2] >> 4) & 0xF
        sample_rate = (head[2] >> 2) & 0x3
        return layer != 0 and bitrate not in (0, 0xF) and sample_rate != 0x3
    return False


def sniff(head: bytes) -> Sniffed | None:
    """Identify allowed media from the first bytes; None means "not an accepted type"."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return _PNG
    if head.startswith(b"\xff\xd8\xff"):
        return _JPEG
    if head.startswith((b"GIF87a", b"GIF89a")):
        return _GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _WAV
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return _WEBM if b"webm" in head[:64] else None  # Matroska other than WebM is refused
    if head.startswith(b"OggS"):
        return _OGG
    if head.startswith(b"fLaC"):
        return _FLAC
    iso = _iso_bmff(head)
    if iso is not None:
        return iso
    if _mp3(head):
        return _MP3
    return None
