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
