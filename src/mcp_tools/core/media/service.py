"""Media: every file the server stores (uploads, imports, job outputs).

A source is stored once. Its type, extension and MIME type come from sniffing the bytes on
every ingest path, never from a file name or a declared content type.
"""

import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.blobs.base import BlobTooLarge
from mcp_tools.blobs.local import LocalBlobStore
from mcp_tools.core.clock import Clock
from mcp_tools.core.errors import INVALID_PARAMS, NOT_FOUND, QUOTA_EXCEEDED, ApiError
from mcp_tools.core.ids import is_id, new_id, shard_of
from mcp_tools.core.media import probe as probing
from mcp_tools.core.media.sniff import SNIFF_BYTES, Sniffed, sniff
from mcp_tools.core.pagination import Page, PageRequest, encode_cursor
from mcp_tools.core.principal import Principal
from mcp_tools.db.schema import media
from mcp_tools.db.sql import insert_ignore
from mcp_tools.db.store import Store

MB = 1024 * 1024
MAX_FILENAME_CHARS = 128
_BIDI_AND_FORMAT = {"Cf", "Cc", "Cs", "Co", "Cn"}


@dataclass(frozen=True)
class MediaLimits:
    image_bytes: int = 25 * MB
    video_bytes: int = 500 * MB
    audio_bytes: int = 100 * MB

    def for_kind(self, kind: str) -> int:
        return {"image": self.image_bytes, "video": self.video_bytes, "audio": self.audio_bytes}[kind]

    @property
    def largest(self) -> int:
        return max(self.image_bytes, self.video_bytes, self.audio_bytes)


@dataclass(frozen=True)
class StoredBlob:
    """A file written to the blob store that has no database row yet."""

    media_id: str
    key: str
    kind: str
    mime: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class MediaRow:
    id: str
    workspace_id: str
    kind: str
    mime: str
    status: str
    source: str
    job_id: str | None
    output_index: int | None
    blob_key: str | None
    size_bytes: int | None
    sha256: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    filename: str | None
    created_at: int

    @classmethod
    def from_mapping(cls, row: Any) -> "MediaRow":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


def sanitize_filename(name: str | None) -> str | None:
    """Keep a display name safe to show: no path, control, bidi or format characters."""
    if not name:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in base if unicodedata.category(ch) not in _BIDI_AND_FORMAT).strip()
    return cleaned[:MAX_FILENAME_CHARS] or None


def _unsupported(detail: str) -> ApiError:
    return ApiError(
        INVALID_PARAMS,
        f"unsupported media: {detail}",
        hint="Accepted: PNG, JPEG, GIF, WebP images; MP4, MOV, WebM video; WAV, MP3, OGG, FLAC, M4A audio.",
    )


class MediaService:
    def __init__(
        self, store: Store, blobs: LocalBlobStore, clock: Clock, *, limits: MediaLimits | None = None
    ) -> None:
        self._store = store
        self._blobs = blobs
        self._clock = clock
        self.limits = limits or MediaLimits()

    # -- writing ---------------------------------------------------------------------------
    async def store_stream(
        self,
        chunks: AsyncIterator[bytes],
        *,
        max_bytes: int | None = None,
        accept: frozenset[str] = frozenset({"image", "video", "audio"}),
    ) -> StoredBlob:
        """Sniff, size-limit and store a byte stream. No database row is written."""
        head = bytearray()
        iterator = chunks.__aiter__()
        exhausted = False
        while len(head) < SNIFF_BYTES and not exhausted:
            try:
                head.extend(await iterator.__anext__())
            except StopAsyncIteration:
                exhausted = True
        if not head:
            raise _unsupported("the file is empty")
        sniffed: Sniffed | None = sniff(bytes(head))
        if sniffed is None:
            raise _unsupported("the content is not a recognised image, video or audio file")
        if sniffed.kind not in accept:
            raise _unsupported(f"expected {' or '.join(sorted(accept))}, got {sniffed.kind}")
        cap = min(max_bytes or self.limits.largest, self.limits.for_kind(sniffed.kind))

        async def replay() -> AsyncIterator[bytes]:
            yield bytes(head)
            if not exhausted:
                async for chunk in iterator:
                    yield chunk

        media_id = new_id("med", self._clock.now_ms())
        key = f"media/{shard_of(media_id)}/{media_id}.{sniffed.ext}"
        try:
            stat = await self._blobs.put(key, replay(), max_bytes=cap)
        except BlobTooLarge as exc:
            raise ApiError(
                QUOTA_EXCEEDED,
                f"the {sniffed.kind} is larger than the {cap // MB} MB limit",
                hint="Use a smaller file.",
            ) from exc
        path = self._blobs.local_path(key)
        assert path is not None
        try:
            info = await anyio.to_thread.run_sync(probing.probe, path, sniffed.kind, sniffed.ext)
        except probing.ProbeError as exc:
            await self.discard_key(key)
            raise _unsupported(str(exc)) from exc
        return StoredBlob(
            media_id=media_id,
            key=key,
            kind=sniffed.kind,
            mime=sniffed.mime,
            size=stat.size,
            sha256=stat.sha256,
            width=info.width,
            height=info.height,
            duration_ms=info.duration_ms,
        )

    async def store_bytes(self, data: bytes, **kwargs: Any) -> StoredBlob:
        async def once() -> AsyncIterator[bytes]:
            yield data

        return await self.store_stream(once(), **kwargs)

    async def insert(
        self,
        conn: AsyncConnection,
        blob: StoredBlob,
        *,
        principal: Principal,
        source: str,
        filename: str | None = None,
        job_id: str | None = None,
        output_index: int | None = None,
        origin: str | None = None,
    ) -> bool:
        """Register a stored blob. Returns False if this job output was already registered."""
        inserted = (
            await conn.execute(
                insert_ignore(conn, media, conflict=["job_id", "output_index"])
                .values(
                    id=blob.media_id,
                    workspace_id=principal.workspace_id,
                    kind=blob.kind,
                    mime=blob.mime,
                    status="ready",
                    source=source,
                    job_id=job_id,
                    output_index=output_index,
                    blob_key=blob.key,
                    size_bytes=blob.size,
                    sha256=blob.sha256,
                    width=blob.width,
                    height=blob.height,
                    duration_ms=blob.duration_ms,
                    filename=sanitize_filename(filename),
                    origin=origin,
                    created_at=self._clock.now_ms(),
                )
                .returning(media.c.id)
            )
        ).first()
        return inserted is not None

    async def ingest(
        self,
        chunks: AsyncIterator[bytes],
        *,
        principal: Principal,
        source: str,
        filename: str | None = None,
        origin: str | None = None,
        max_bytes: int | None = None,
        accept: frozenset[str] = frozenset({"image", "video", "audio"}),
    ) -> MediaRow:
        """Store a stream and register it as ready media in one go."""
        blob = await self.store_stream(chunks, max_bytes=max_bytes, accept=accept)

        async def unit(conn: AsyncConnection) -> None:
            await self.insert(
                conn, blob, principal=principal, source=source, filename=filename, origin=origin
            )

        try:
            await self._store.write(unit)
        except BaseException:
            await self.discard_key(blob.key)
            raise
        return await self.get(blob.media_id, principal=principal)

    async def discard_key(self, key: str) -> None:
        """Delete a blob that will not get a row (lost finalize, rejected file)."""
        with anyio.CancelScope(shield=True):
            await self._blobs.delete(key)

    # -- reading ---------------------------------------------------------------------------
    async def get(self, media_id: str, *, principal: Principal) -> MediaRow:
        rows = await self.get_many([media_id], principal=principal)
        if not rows:
            raise ApiError(
                NOT_FOUND,
                f"no media with id {media_id!r}",
                hint="Use an id returned by a media or generate tool.",
                recovery_tool="media_list",
            )
        return rows[0]

    async def get_many(self, media_ids: list[str], *, principal: Principal) -> list[MediaRow]:
        wanted = [m for m in media_ids if is_id(m, "med")]
        if not wanted:
            return []
        async with self._store.read_tx() as conn:
            rows = (
                (
                    await conn.execute(
                        select(media).where(
                            media.c.id.in_(wanted),
                            media.c.workspace_id == principal.workspace_id,
                            media.c.status != "deleted",
                        )
                    )
                )
                .mappings()
                .all()
            )
        by_id = {r["id"]: MediaRow.from_mapping(r) for r in rows}
        return [by_id[m] for m in wanted if m in by_id]

    async def for_jobs(self, job_ids: list[str], *, principal: Principal) -> dict[str, list[MediaRow]]:
        if not job_ids:
            return {}
        async with self._store.read_tx() as conn:
            rows = (
                (
                    await conn.execute(
                        select(media)
                        .where(
                            media.c.job_id.in_(job_ids),
                            media.c.workspace_id == principal.workspace_id,
                            media.c.status == "ready",
                        )
                        .order_by(media.c.job_id, media.c.output_index)
                    )
                )
                .mappings()
                .all()
            )
        out: dict[str, list[MediaRow]] = {}
        for row in rows:
            out.setdefault(row["job_id"], []).append(MediaRow.from_mapping(row))
        return out

    async def list(
        self, page: PageRequest, *, principal: Principal, kind: str | None = None, source: str | None = None
    ) -> Page[MediaRow]:
        """Newest first, keyset-paginated on the (time-sortable) id."""
        query = select(media).where(media.c.workspace_id == principal.workspace_id, media.c.status == "ready")
        if kind:
            query = query.where(media.c.kind == kind)
        if source:
            query = query.where(media.c.source == source)
        before = (page.cursor or {}).get("before")
        if isinstance(before, str):
            query = query.where(media.c.id < before)
        async with self._store.read_tx() as conn:
            rows = (
                (await conn.execute(query.order_by(media.c.id.desc()).limit(page.limit + 1))).mappings().all()
            )
        items = [MediaRow.from_mapping(r) for r in rows[: page.limit]]
        has_more = len(rows) > page.limit
        return Page(
            items=items,
            has_more=has_more,
            next_cursor=encode_cursor({"before": items[-1].id}) if has_more and items else None,
        )

    def local_path(self, row: MediaRow) -> Path | None:
        return self._blobs.local_path(row.blob_key) if row.blob_key else None

    async def preview(self, row: MediaRow) -> bytes | None:
        """A small JPEG of an image; None for kinds we cannot preview."""
        path = self.local_path(row)
        if row.kind != "image" or path is None:
            return None
        try:
            return await anyio.to_thread.run_sync(probing.make_preview, path)
        except probing.ProbeError:
            return None
