"""Small, valid media files generated on the fly for tests."""

import io
import struct
import wave

from PIL import Image


def png_bytes(width: int = 32, height: int = 24, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 32, height: int = 24) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


def wav_bytes(seconds: float = 0.5, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return buffer.getvalue()


def mp4_header_bytes() -> bytes:
    """Enough of an MP4 for sniffing (not a playable file)."""
    return struct.pack(">I", 24) + b"ftypisom" + struct.pack(">I", 512) + b"isomiso2mp41" + b"\x00" * 64


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body><script>alert(1)</script></body></html>"
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"
ZIP = b"PK\x03\x04" + b"\x00" * 40
