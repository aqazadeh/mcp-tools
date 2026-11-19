"""Blob storage and URL issuing interfaces. Local disk now; S3 can implement the same protocols."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class BlobTooLarge(Exception):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"blob exceeds {max_bytes} bytes")
        self.max_bytes = max_bytes


@dataclass(frozen=True)
class BlobStat:
    key: str
    size: int
    sha256: str


class BlobStore(Protocol):
    async def put(self, key: str, chunks: AsyncIterator[bytes], *, max_bytes: int) -> BlobStat:
        """Store a stream under `key` atomically; raise BlobTooLarge past `max_bytes`."""
        ...

    def open(self, key: str, *, start: int = 0, end: int | None = None) -> AsyncIterator[bytes]: ...

    async def stat(self, key: str) -> BlobStat | None: ...

    async def delete(self, key: str) -> None: ...

    def local_path(self, key: str) -> Path | None:
        """A filesystem path for local stores; None for remote ones."""
        ...


class UrlIssuer(Protocol):
    """Mints time-limited URLs for a media item. `NullIssuer` on stdio, HMAC gateway on HTTP."""

    def download_url(self, media_id: str, *, ttl_seconds: int) -> str | None: ...

    def upload_url(self, media_id: str, *, ttl_seconds: int, max_bytes: int, nonce: str) -> str | None: ...


class NullIssuer:
    """No URLs: results carry local paths instead (stdio mode)."""

    def download_url(self, media_id: str, *, ttl_seconds: int) -> str | None:
        return None

    def upload_url(self, media_id: str, *, ttl_seconds: int, max_bytes: int, nonce: str) -> str | None:
        return None
