"""Blob store on the local filesystem.

Writes go to a temporary file in the same directory tree and are renamed into place, so a
reader never sees a partial blob and a crash leaves at most a stray temp file.
"""

import hashlib
import os
import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import anyio

from mcp_tools.blobs.base import BlobStat, BlobTooLarge

_KEY = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*/[a-z0-9_]+\.[a-z0-9]{1,8}$")
_CHUNK = 256 * 1024
_TMP_DIR = "tmp"


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        (self.root / _TMP_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY.match(key) or key.startswith(f"{_TMP_DIR}/"):
            raise ValueError(f"invalid blob key: {key!r}")
        return self.root / key

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    async def put(self, key: str, chunks: AsyncIterator[bytes], *, max_bytes: int) -> BlobStat:
        final = self._path(key)
        temp = self.root / _TMP_DIR / f"{secrets.token_hex(12)}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            async with await anyio.open_file(temp, "wb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise BlobTooLarge(max_bytes)
                    digest.update(chunk)
                    await handle.write(chunk)
                await handle.flush()
                await anyio.to_thread.run_sync(os.fsync, handle.wrapped.fileno())
            await anyio.to_thread.run_sync(
                lambda: final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            )
            await anyio.to_thread.run_sync(os.replace, temp, final)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda: temp.unlink(missing_ok=True))
            raise
        return BlobStat(key=key, size=size, sha256=digest.hexdigest())

    async def put_bytes(self, key: str, data: bytes, *, max_bytes: int | None = None) -> BlobStat:
        async def once() -> AsyncIterator[bytes]:
            yield data

        return await self.put(key, once(), max_bytes=max_bytes if max_bytes is not None else len(data))

    async def open(self, key: str, *, start: int = 0, end: int | None = None) -> AsyncIterator[bytes]:
        """Yield the blob (or the byte range [start, end)) in chunks."""
        path = self._path(key)
        remaining = None if end is None else max(0, end - start)
        async with await anyio.open_file(path, "rb") as handle:
            if start:
                await handle.seek(start)
            while remaining is None or remaining > 0:
                chunk = await handle.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
                if not chunk:
                    return
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk

    async def read_bytes(self, key: str, *, limit: int | None = None) -> bytes:
        parts = []
        async for chunk in self.open(key, end=limit):
            parts.append(chunk)
        return b"".join(parts)

    async def stat(self, key: str) -> BlobStat | None:
        path = self._path(key)

        def compute() -> BlobStat | None:
            if not path.is_file():
                return None
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
            return BlobStat(key=key, size=path.stat().st_size, sha256=digest.hexdigest())

        return await anyio.to_thread.run_sync(compute)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await anyio.to_thread.run_sync(lambda: path.unlink(missing_ok=True))

    async def sweep_temp(self, *, older_than_seconds: float, now: float) -> int:
        """Remove stray temp files left by a crash. Returns how many were removed."""

        def run() -> int:
            removed = 0
            for entry in (self.root / _TMP_DIR).glob("*.part"):
                try:
                    if now - entry.stat().st_mtime > older_than_seconds:
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
            return removed

        return await anyio.to_thread.run_sync(run)
