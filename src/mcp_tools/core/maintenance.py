"""Housekeeping, run by `mcp-tools gc`.

Removes what nothing can use any more: upload slots that expired, replay keys past their
window, temporary files of interrupted downloads and blob files without a media row (left
when a process is killed between writing a file and committing its row). Deleting media by
age is opt-in.
"""

import time
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.db.schema import media

ORPHAN_MIN_AGE_SECONDS = 24 * 3600  # never touch a file a running job may be about to register


async def collect_garbage(platform: Any, *, media_older_than_days: int = 0, dry_run: bool = False) -> dict[str, int]:
    now_ms = platform.clock.now_ms()
    report = {"expired_uploads": 0, "old_media": 0, "replay_keys": 0, "orphan_files": 0, "temporary_files": 0}
    doomed_keys: list[str] = []

    async def unit(conn: AsyncConnection) -> None:
        expired = (
            await conn.execute(
                select(media.c.id, media.c.blob_key).where(media.c.status == "pending", media.c.expires_at <= now_ms)
            )
        ).all()
        report["expired_uploads"] = len(expired)
        doomed_keys.extend(key for _, key in expired if key)
        old: list[Any] = []
        if media_older_than_days > 0:
            cutoff = now_ms - media_older_than_days * 86_400_000
            old = list(
                (
                    await conn.execute(
                        select(media.c.id, media.c.blob_key).where(
                            media.c.status == "ready", media.c.created_at <= cutoff
                        )
                    )
                ).all()
            )
            report["old_media"] = len(old)
            doomed_keys.extend(key for _, key in old if key)
        if dry_run:
            return
        if expired:
            await conn.execute(delete(media).where(media.c.id.in_([row[0] for row in expired])))
        if old:
            await conn.execute(
                update(media).where(media.c.id.in_([row[0] for row in old])).values(status="deleted", blob_key=None)
            )
        report["replay_keys"] = await platform.idempotency.sweep(conn)

    await platform.store.write(unit)

    async with platform.store.read_tx() as conn:
        known = {
            row[0] for row in (await conn.execute(select(media.c.blob_key).where(media.c.blob_key.is_not(None)))).all()
        }

    root: Path = platform.blobs.root

    def scan() -> tuple[list[Path], list[Path]]:
        orphans, temporary = [], []
        now = time.time()
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            age = now - path.stat().st_mtime
            relative = path.relative_to(root).as_posix()
            if path.suffix == ".part":
                if age > 3600:
                    temporary.append(path)
            elif relative.startswith("media/") and relative not in known and age > ORPHAN_MIN_AGE_SECONDS:
                orphans.append(path)
        return orphans, temporary

    orphans, temporary = await anyio.to_thread.run_sync(scan)
    report["orphan_files"], report["temporary_files"] = len(orphans), len(temporary)
    if not dry_run:
        for key in doomed_keys:
            await platform.blobs.delete(key)
        for path in [*orphans, *temporary]:
            await anyio.to_thread.run_sync(lambda p=path: p.unlink(missing_ok=True))
    return report
