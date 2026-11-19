import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from mcp_tools.config import Settings
from mcp_tools.core.platform import Platform
from tests.helpers.mp_workers import spend_worker

pytestmark = pytest.mark.anyio


async def test_platform_start_is_reentrant_and_opens_the_account(settings: Settings) -> None:
    platform = Platform(settings)
    async with platform.running():
        assert platform.started
        # a second in-process client enters the same lifespan
        async with platform.running(), platform.store.read_tx() as conn:
            balance = await platform.ledger.balance(conn, platform.principal.workspace_id)
        assert platform.started  # still running for the outer entry
    assert not platform.started
    assert balance.balance == settings.initial_credits and balance.daily_cap == settings.daily_cap_credits

    # restarting on the same data directory does not grant the opening balance again
    again = Platform(settings)
    async with again.running(), again.store.read_tx() as conn:
        assert (await again.ledger.balance(conn, "ws_local")).balance == settings.initial_credits


@pytest.mark.slow
async def test_processes_sharing_one_database_never_overdraw(tmp_path: Path) -> None:
    """Several server processes on one SQLite file is the normal stdio case."""
    settings = Settings(data_dir=tmp_path / "data", initial_credits=60, daily_cap_credits=1_000_000)
    platform = Platform(settings)
    async with platform.running():
        db_path = str(platform.data.database)

    workers, attempts = 4, 30
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        futures = [pool.submit(spend_worker, db_path, w, attempts, 1) for w in range(workers)]
        results = [f.result(timeout=120) for f in futures]

    succeeded = sum(r[0] for r in results)
    refused = sum(r[1] for r in results)
    busy = sum(r[2] for r in results)
    assert succeeded == 60, results  # exactly the affordable number, across processes
    assert succeeded + refused + busy == workers * attempts
    assert busy == 0, results  # writers queue on the file lock instead of failing

    async with platform.running(), platform.store.read_tx() as conn:
        final = await platform.ledger.balance(conn, "ws_local")
    assert (final.balance, final.spent_today) == (0, 60)
