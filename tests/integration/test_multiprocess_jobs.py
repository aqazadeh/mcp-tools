"""Several server processes (each with its own job runner) on one data directory."""

import multiprocessing
import os
import signal
import time
from collections import Counter
from pathlib import Path

import anyio
import pytest
from sqlalchemy import select

from mcp_tools.config import Settings
from mcp_tools.core.jobs.service import GenerationRequest
from mcp_tools.core.platform import Platform
from mcp_tools.db.schema import jobs, ledger_entries, media
from tests.helpers.mp_workers import job_worker
from tests.helpers.providers import CATALOG_DIR, ScriptedProvider

pytestmark = [pytest.mark.anyio, pytest.mark.slow]

JOBS = 24
PRICE = 100


async def test_no_job_is_submitted_twice_and_a_killed_worker_is_cleaned_up(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    submit_log = tmp_path / "submits.log"
    settings = Settings(
        data_dir=data_dir,
        catalog_dirs=str(CATALOG_DIR),
        log_level="ERROR",
        lease_ttl_ms=600,
        initial_credits=JOBS * PRICE,
        auto_approve_credits=PRICE,
        daily_cap_credits=10 * JOBS * PRICE,
    )
    # The parent only creates jobs; it runs no job runner of its own.
    parent = Platform(settings, providers=[ScriptedProvider()], run_jobs=False)
    async with parent.running():
        job_ids = []
        for _ in range(JOBS):
            outcome = await parent.jobs.create(
                [GenerationRequest(0, {"model": "fake_image", "prompt": "p"})],
                principal=parent.principal,
                tool="generate_image",
                output_type="image",
            )
            job_ids.append(outcome.jobs[0].job_id)

        context = multiprocessing.get_context("spawn")
        # One worker hangs inside the provider call and is killed: its jobs are stuck in `submitting`.
        hanging = context.Process(target=job_worker, args=(str(data_dir), str(submit_log), 60.0, True))
        hanging.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (submit_log.exists() and submit_log.read_text().strip()):
            await anyio.sleep(0.05)
        assert hanging.pid is not None
        os.kill(hanging.pid, signal.SIGKILL)
        hanging.join(10)

        workers = [
            context.Process(target=job_worker, args=(str(data_dir), str(submit_log), 30.0, False)) for _ in range(3)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            await anyio.to_thread.run_sync(worker.join, 60)
            assert worker.exitcode == 0

        async with parent.store.read_tx() as conn:
            rows = (await conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.error_code))).all()
            media_jobs = [r[0] for r in (await conn.execute(select(media.c.job_id))).all()]
            kinds = Counter(
                (r[0], r[1])
                for r in (
                    await conn.execute(
                        select(ledger_entries.c.ref_id, ledger_entries.c.kind).where(
                            ledger_entries.c.ref_id.is_not(None)
                        )
                    )
                ).all()
            )
            balance = (await parent.ledger.balance(conn, "ws_local")).balance

    statuses = {job_id: (status, code) for job_id, status, code in rows}
    assert set(statuses) == set(job_ids)
    completed = [j for j, (s, _) in statuses.items() if s == "completed"]
    interrupted = [j for j, (s, c) in statuses.items() if (s, c) == ("failed", "interrupted")]
    assert len(completed) + len(interrupted) == JOBS, Counter(statuses.values())  # nothing left active
    assert interrupted, "the killed worker should have left at least one job in `submitting`"

    submits = Counter(line.split()[0] for line in submit_log.read_text().splitlines())
    assert max(submits.values()) == 1, {j: n for j, n in submits.items() if n > 1}  # never sent twice
    assert set(interrupted) <= set(submits)  # they were sent by the worker that died, and not again

    assert Counter(media_jobs) == Counter(completed)  # one output per completed job, none for the others
    assert all(kinds[(j, "spend")] == 1 for j in job_ids)
    assert all(kinds[(j, "refund")] == 1 for j in interrupted)
    assert all(kinds[(j, "refund")] == 0 for j in completed)
    assert balance == JOBS * PRICE - PRICE * len(completed)
