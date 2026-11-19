"""Bring outside files into the media store: from a URL or from an allowed local folder."""

from collections.abc import Sequence
from pathlib import Path

import anyio

from mcp_tools.core.errors import BUSY, UNSUPPORTED, ApiError
from mcp_tools.core.media.fetch import SafeFetcher
from mcp_tools.core.media.paths import open_confined, read_fd
from mcp_tools.core.media.service import MediaRow, MediaService
from mcp_tools.core.principal import Principal

MAX_CONCURRENT_IMPORTS = 3


class MediaImporter:
    def __init__(self, media: MediaService, fetcher: SafeFetcher, *, roots: Sequence[Path]) -> None:
        self._media = media
        self._fetcher = fetcher
        self.roots = tuple(roots)
        self._slots = MAX_CONCURRENT_IMPORTS

    @property
    def egress(self) -> str:
        return self._fetcher.egress

    def _acquire(self) -> None:
        if self._slots <= 0:
            raise ApiError(
                BUSY, "too many imports are running", hint="Wait for the running imports to finish.", retryable=True
            )
        self._slots -= 1

    async def from_url(self, url: str, *, principal: Principal) -> MediaRow:
        self._acquire()
        try:
            async with self._fetcher.stream(url, max_bytes=self._media.limits.largest) as chunks:
                # The origin is kept without query or fragment: those often carry access tokens.
                origin = url.split("?", 1)[0].split("#", 1)[0][:500]
                name = origin.rstrip("/").rsplit("/", 1)[-1] or None
                return await self._media.ingest(
                    chunks, principal=principal, source="import_url", filename=name, origin=origin
                )
        finally:
            self._slots += 1

    async def from_path(self, path: str, *, principal: Principal) -> MediaRow:
        if not self.roots:
            raise ApiError(UNSUPPORTED, "local imports are disabled on this server", hint="Use media_import_url.")
        self._acquire()
        try:
            limit = self._media.limits.largest
            fd, resolved, _size = await anyio.to_thread.run_sync(open_confined, path, self.roots, limit)
            return await self._media.ingest(
                read_fd(fd, max_bytes=limit), principal=principal, source="import_path", filename=resolved.name
            )
        finally:
            self._slots += 1
