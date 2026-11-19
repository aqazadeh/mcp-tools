"""Signed, short-lived URLs for the file gateway.

A URL carries everything that is signed: purpose, media id, expiry, byte limit and (for
uploads) a single-use nonce. Each field is matched against a strict pattern before it is
used, the fields are joined with newlines under a version tag, and the MAC key is derived
for this one purpose, so a signature can never be replayed as a different kind of token.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl

from mcp_tools.core.clock import Clock

MAX_TTL_SECONDS = 15 * 60
_VERSION = "v1"
_MEDIA_ID = re.compile(r"^med_[0-9a-z]{26}$")
_INT = re.compile(r"^(0|[1-9][0-9]{0,14})$")  # canonical form only: one URL per grant
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SIG = re.compile(r"^[0-9a-f]{64}$")
_KEYS = {"download": {"e", "s"}, "upload": {"e", "m", "n", "s"}}


@dataclass(frozen=True)
class Grant:
    """What a verified URL allows."""

    kind: str
    media_id: str
    expires_at: int
    max_bytes: int = 0
    nonce: str = ""


class Signer:
    def __init__(self, secret: bytes) -> None:
        self._key = hmac.new(secret, b"mcp-tools/file-gateway/url/" + _VERSION.encode(), hashlib.sha256).digest()

    def _mac(self, kind: str, media_id: str, expires_at: int, max_bytes: int, nonce: str) -> str:
        message = "\n".join([_VERSION, kind, media_id, str(expires_at), str(max_bytes), nonce]).encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def download_query(self, media_id: str, expires_at: int) -> str:
        return f"e={expires_at}&s={self._mac('download', media_id, expires_at, 0, '')}"

    def upload_query(self, media_id: str, expires_at: int, max_bytes: int, nonce: str) -> str:
        return f"e={expires_at}&m={max_bytes}&n={nonce}&s={self._mac('upload', media_id, expires_at, max_bytes, nonce)}"

    def verify(self, kind: str, media_id: str, query: bytes, *, now_s: int) -> Grant | None:
        """Return the grant a URL carries, or None for anything malformed, forged or expired."""
        try:
            pairs = parse_qsl(query.decode("ascii"), keep_blank_values=True, strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            return None
        fields = dict(pairs)
        if len(fields) != len(pairs) or set(fields) != _KEYS[kind] or not _MEDIA_ID.match(media_id):
            return None
        if not (_INT.match(fields["e"]) and _SIG.match(fields["s"])):
            return None
        max_bytes, nonce = 0, ""
        if kind == "upload":
            if not (_INT.match(fields["m"]) and _NONCE.match(fields["n"])):
                return None
            max_bytes, nonce = int(fields["m"]), fields["n"]
        expires_at = int(fields["e"])
        expected = self._mac(kind, media_id, expires_at, max_bytes, nonce)
        if not hmac.compare_digest(expected, fields["s"]):
            return None
        if expires_at <= now_s or expires_at > now_s + MAX_TTL_SECONDS + 60:
            return None
        return Grant(kind=kind, media_id=media_id, expires_at=expires_at, max_bytes=max_bytes, nonce=nonce)


class GatewayIssuer:
    """Mints gateway URLs. They are built from the configured public base URL, never from a request's Host."""

    def __init__(self, signer: Signer, public_base_url: str, clock: Clock) -> None:
        self._signer = signer
        self._base = public_base_url.rstrip("/")
        self._clock = clock

    def _expiry(self, ttl_seconds: int) -> int:
        return self._clock.now_ms() // 1000 + max(1, min(ttl_seconds, MAX_TTL_SECONDS))

    def download_url(self, media_id: str, *, ttl_seconds: int) -> str | None:
        return f"{self._base}/files/{media_id}?{self._signer.download_query(media_id, self._expiry(ttl_seconds))}"

    def upload_url(self, media_id: str, *, ttl_seconds: int, max_bytes: int, nonce: str) -> str | None:
        query = self._signer.upload_query(media_id, self._expiry(ttl_seconds), max_bytes, nonce)
        return f"{self._base}/upload/{media_id}?{query}"
