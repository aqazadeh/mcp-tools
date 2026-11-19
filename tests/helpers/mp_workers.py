"""Worker entry points for multi-process tests (must be importable under the spawn context)."""

import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.billing.ledger import Ledger, Ref
from mcp_tools.core.clock import SystemClock
from mcp_tools.core.errors import ApiError
from mcp_tools.db.store import Store


def spend_worker(db_path: str, worker: int, attempts: int, amount: int) -> tuple[int, int, int]:
    """Try to spend `attempts` times; return (succeeded, refused, busy)."""

    async def run() -> tuple[int, int, int]:
        store = Store(Path(db_path))
        await store.open()
        ledger = Ledger(SystemClock())
        ok = refused = busy = 0
        try:
            for i in range(attempts):
                ref = Ref("job", f"job_w{worker}_{i}")

                async def unit(conn: AsyncConnection, ref: Ref = ref) -> None:
                    await ledger.spend(conn, "ws_local", amount, ref)

                try:
                    await store.write(unit)
                    ok += 1
                except ApiError as exc:
                    if exc.code == "busy":
                        busy += 1
                    else:
                        refused += 1
        finally:
            await store.close()
        return ok, refused, busy

    return asyncio.run(run())


def job_worker(data_dir: str, submit_log: str, seconds: float, hang: bool) -> int:
    """Run a platform (with its job runner) on a shared data directory; return jobs still active."""
    import anyio
    from sqlalchemy import func, select

    from mcp_tools.config import Settings
    from mcp_tools.core.platform import Platform
    from mcp_tools.db.schema import ACTIVE_JOB_STATUSES, jobs
    from tests.helpers.providers import CATALOG_DIR, ScriptedProvider

    async def run() -> int:
        settings = Settings(
            data_dir=Path(data_dir),
            catalog_dirs=str(CATALOG_DIR),
            log_level="ERROR",
            lease_ttl_ms=600,
            shutdown_grace_seconds=0.5,
        )
        provider = ScriptedProvider(submit_log=Path(submit_log), submit_delay=120.0 if hang else 0.02)
        platform = Platform(settings, providers=[provider])
        active = -1
        async with platform.running():
            deadline = anyio.current_time() + seconds
            while anyio.current_time() < deadline:
                async with platform.store.read_tx() as conn:
                    active = (
                        await conn.execute(
                            select(func.count()).select_from(jobs).where(jobs.c.status.in_(ACTIVE_JOB_STATUSES))
                        )
                    ).scalar_one()
                if active == 0 and not hang:
                    break
                await anyio.sleep(0.05)
        return active

    return asyncio.run(run())
