"""The job state machine, driven step by step with a fake clock and a scripted provider.

No runner is started: each test calls `engine.advance()` exactly when it wants a step, and a
second `Platform` on the same data directory stands in for a second server process.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from mcp_tools.config import Settings
from mcp_tools.core.clock import FakeClock
from mcp_tools.core.errors import ApiError
from mcp_tools.core.jobs.engine import QUEUED_TTL_MS
from mcp_tools.core.jobs.service import GenerationRequest
from mcp_tools.core.platform import Platform
from mcp_tools.db.schema import jobs, ledger_entries, media
from mcp_tools.providers.base import OutputRef, PollResult, ProviderError
from tests.helpers.providers import CATALOG_DIR, FAILED, MODERATED, RUNNING, TRANSIENT, ScriptedProvider

pytestmark = pytest.mark.anyio
TTL_MS = 30_000


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        catalog_dirs=str(CATALOG_DIR),
        log_level="WARNING",
        initial_credits=1_000,
        auto_approve_credits=1_000,
        daily_cap_credits=100_000,
    )


class Rig:
    """One simulated server process: its own provider, store and engine on a shared data dir."""

    def __init__(self, tmp_path: Path, clock: FakeClock) -> None:
        self.provider = ScriptedProvider()
        self.platform = Platform(_settings(tmp_path), clock=clock, providers=[self.provider], run_jobs=False)
        self.clock = clock

    async def create(self, model: str = "fake_image", **params: Any) -> str:
        outcome = await self.platform.jobs.create(
            [GenerationRequest(0, {"model": model, "prompt": "p", **params})],
            principal=self.platform.principal,
            tool="generate_image",
            output_type=None,
        )
        return outcome.jobs[0].job_id

    async def step(self, job_id: str) -> None:
        await self.platform.engine.advance(job_id)

    async def row(self, job_id: str) -> dict[str, Any]:
        async with self.platform.store.read_tx() as conn:
            return dict((await conn.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one())

    async def status(self, job_id: str) -> str:
        return (await self.row(job_id))["status"]

    async def balance(self) -> int:
        async with self.platform.store.read_tx() as conn:
            return (await self.platform.ledger.balance(conn, "ws_local")).balance

    async def kinds(self, job_id: str) -> list[str]:
        async with self.platform.store.read_tx() as conn:
            rows = await conn.execute(
                select(ledger_entries.c.kind).where(ledger_entries.c.ref_id == job_id).order_by(ledger_entries.c.id)
            )
            return [r[0] for r in rows.all()]

    async def media_count(self, job_id: str) -> int:
        async with self.platform.store.read_tx() as conn:
            return len((await conn.execute(select(media.c.id).where(media.c.job_id == job_id))).all())

    async def set(self, job_id: str, **values: Any) -> None:
        async with self.platform.store.write_tx() as conn:
            await conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def rig(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Rig]:
    r = Rig(tmp_path, clock)
    async with r.platform.running():
        yield r


@pytest.fixture
async def other(tmp_path: Path, clock: FakeClock, rig: Rig) -> AsyncIterator[Rig]:
    """A second "process" sharing the first one's database and blob directory."""
    r = Rig(tmp_path, clock)
    async with r.platform.running():
        yield r


# -- the happy path -----------------------------------------------------------------------------
async def test_submit_poll_complete(rig: Rig) -> None:
    job = await rig.create(quality="high")
    assert await rig.status(job) == "queued" and await rig.balance() == 700  # 100 x3, debited at creation

    await rig.step(job)
    row = await rig.row(job)
    assert (row["status"], row["remote_id"], row["lease_owner"]) == ("running", f"remote-{job}", None)
    assert len(rig.provider.submits) == 1 and rig.provider.submits[0].params == {"quality": "high"}

    await rig.step(job)  # not due yet: no provider call
    assert rig.provider.polls == []

    rig.clock.advance(0.2)
    rig.provider.poll_script = [RUNNING]
    await rig.step(job)
    assert (await rig.row(job))["progress"] == 0.5 and await rig.status(job) == "running"

    rig.clock.advance(0.2)
    await rig.step(job)
    row = await rig.row(job)
    assert (row["status"], row["progress"], row["lease_owner"], row["error_code"]) == (
        "completed",
        1.0,
        None,
        None,
    )
    assert await rig.media_count(job) == 1 and await rig.kinds(job) == ["spend"]

    (view,) = await rig.platform.jobs.get([job], principal=rig.platform.principal)
    assert view.status == "completed" and view.results[0].kind == "image"
    assert view.results[0].local_path is not None and Path(view.results[0].local_path).is_file()

    await rig.step(job)  # a terminal job is never touched again
    assert len(rig.provider.submits) == 1 and len(rig.provider.polls) == 2


async def test_synchronous_provider_completes_in_the_submit_step(rig: Rig) -> None:
    rig.provider.sync_result = PollResult("succeeded", outputs=(OutputRef(state={}),))
    job = await rig.create("fake_image_sync")
    await rig.step(job)
    assert await rig.status(job) == "completed" and rig.provider.polls == []
    assert await rig.media_count(job) == 1


# -- failures refund exactly once -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("result", "status", "code"),
    [(FAILED, "failed", "provider_error"), (MODERATED, "moderated", "content_policy")],
)
async def test_provider_failure_refunds(rig: Rig, result: PollResult, status: str, code: str) -> None:
    job = await rig.create()
    await rig.step(job)
    rig.clock.advance(1)
    rig.provider.poll_script = [result]
    await rig.step(job)
    row = await rig.row(job)
    assert (row["status"], row["error_code"]) == (status, code)
    assert await rig.balance() == 1_000 and await rig.kinds(job) == ["spend", "refund"]
    assert await rig.media_count(job) == 0


async def test_definite_submit_error_fails_and_ambiguous_one_is_never_resent(rig: Rig) -> None:
    rig.provider.submit_error = ProviderError("invalid_request", "bad input")
    definite = await rig.create()
    await rig.step(definite)
    assert (await rig.row(definite))["error_code"] == "invalid_request"

    rig.provider.submit_error = ProviderError("timeout", "no answer", ambiguous=True)
    ambiguous = await rig.create()
    await rig.step(ambiguous)
    row = await rig.row(ambiguous)
    assert (row["status"], row["error_code"]) == ("failed", "submit_outcome_unknown")
    rig.provider.submit_error = None
    rig.clock.advance(120)
    await rig.step(ambiguous)
    assert [r.job_id for r in rig.provider.submits].count(ambiguous) == 1  # sent once, never again
    assert await rig.balance() == 1_000


async def test_content_policy_error_at_submit_is_moderated(rig: Rig) -> None:
    rig.provider.submit_error = ProviderError("content_policy", "prompt rejected")
    job = await rig.create()
    await rig.step(job)
    assert await rig.status(job) == "moderated" and await rig.balance() == 1_000


async def test_transient_poll_error_is_retried(rig: Rig) -> None:
    job = await rig.create()
    await rig.step(job)
    rig.clock.advance(1)
    rig.provider.poll_script = [TRANSIENT]
    await rig.step(job)
    assert await rig.status(job) == "running"
    rig.clock.advance(5)
    await rig.step(job)
    assert await rig.status(job) == "completed"


async def test_bad_or_missing_output_fails_the_job(rig: Rig) -> None:
    rig.provider.output = b"<html>not an image</html>"
    bad = await rig.create()
    await rig.step(bad)
    rig.clock.advance(1)
    await rig.step(bad)
    assert (await rig.row(bad))["error_code"] == "invalid_output"

    rig.provider.default_poll = PollResult("succeeded")
    empty = await rig.create()
    await rig.step(empty)
    rig.clock.advance(1)
    await rig.step(empty)
    assert (await rig.row(empty))["error_code"] == "no_output"
    assert await rig.balance() == 1_000


async def test_output_download_error_is_retried_then_fails(rig: Rig) -> None:
    rig.provider.output = ProviderError("network", "reset", retryable=True)
    job = await rig.create()
    await rig.step(job)
    for _ in range(8):  # retried a few times (well inside the 60 s deadline), then given up
        rig.clock.advance(3)
        await rig.step(job)
    row = await rig.row(job)
    assert (row["status"], row["error_code"]) == ("failed", "output_unavailable")
    assert await rig.balance() == 1_000


# -- deadlines, queue expiry, cancellation -----------------------------------------------------------
async def test_deadline_cancels_remotely_and_fails(rig: Rig) -> None:
    job = await rig.create()
    await rig.step(job)
    rig.clock.advance(61)
    await rig.step(job)
    assert (await rig.row(job))["error_code"] == "timeout"
    assert rig.provider.cancels == [f"remote-{job}"] and await rig.balance() == 1_000


async def test_a_job_queued_for_too_long_is_not_started(rig: Rig) -> None:
    job = await rig.create()
    rig.clock.advance(QUEUED_TTL_MS / 1000 + 1)
    assert await rig.platform.engine.reap() == [job]
    assert (await rig.row(job))["error_code"] == "expired"
    assert rig.provider.submits == [] and await rig.balance() == 1_000


async def test_cancel_before_start_is_immediate(rig: Rig) -> None:
    job = await rig.create()
    view, refunded, _ = await rig.platform.jobs.cancel(job, principal=rig.platform.principal)
    assert (view.status, refunded) == ("canceled", 100)
    await rig.step(job)
    assert rig.provider.submits == [] and await rig.balance() == 1_000
    again, refunded_again, _ = await rig.platform.jobs.cancel(job, principal=rig.platform.principal)
    assert (again.status, refunded_again) == ("canceled", 0)


async def test_cancel_while_running_goes_through_the_provider(rig: Rig) -> None:
    job = await rig.create()
    await rig.step(job)
    view, refunded, _ = await rig.platform.jobs.cancel(job, principal=rig.platform.principal)
    assert (view.status, refunded) == ("running", 0)  # requested; the runner finishes it
    await rig.step(job)
    assert await rig.status(job) == "canceled" and rig.provider.cancels == [f"remote-{job}"]
    assert await rig.kinds(job) == ["spend", "refund"]
    with pytest.raises(ApiError) as excinfo:
        await rig.platform.jobs.cancel("job_" + "0" * 26, principal=rig.platform.principal)
    assert excinfo.value.code == "not_found"


# -- several processes on one database ---------------------------------------------------------------
async def test_another_process_continues_polling_a_running_job(rig: Rig, other: Rig) -> None:
    job = await rig.create()
    await rig.step(job)  # process A submits, then "dies"
    rig.clock.advance(1)
    await other.step(job)  # process B needs nothing but the stored handle
    assert await other.status(job) == "completed"
    assert len(rig.provider.submits) == 1 and other.provider.submits == [] and other.provider.polls == [f"remote-{job}"]


async def test_a_live_lease_is_respected(rig: Rig, other: Rig) -> None:
    job = await rig.create()
    await rig.step(job)
    rig.clock.advance(1)
    # A holds the lease on the running job (a long poll or download in flight)
    await rig.set(job, lease_owner=rig.platform.engine.owner, lease_expires_at=rig.clock.now_ms() + TTL_MS)
    await other.step(job)
    assert other.provider.polls == [] and await rig.status(job) == "running"


async def test_job_orphaned_while_submitting_is_failed_and_never_resubmitted(rig: Rig, other: Rig) -> None:
    job = await rig.create()
    # process A claimed the job and crashed inside the provider call: the handle was never saved
    await rig.set(
        job,
        status="submitting",
        lease_owner="otherhost|999999|1",
        lease_expires_at=rig.clock.now_ms() + TTL_MS,
    )
    assert await other.platform.engine.reap() == []  # the lease has not expired yet
    await other.step(job)
    assert other.provider.submits == []

    rig.clock.advance(TTL_MS / 1000 + 1)
    assert await other.platform.engine.reap() == [job]
    row = await other.row(job)
    assert (row["status"], row["error_code"]) == ("failed", "interrupted")
    await other.step(job)
    await rig.step(job)
    assert other.provider.submits == [] and rig.provider.submits == []
    assert await other.balance() == 1_000 and await other.kinds(job) == ["spend", "refund"]


async def test_live_owner_on_this_host_gets_a_grace_period_after_a_suspend(rig: Rig, other: Rig) -> None:
    """After a laptop resume every lease looks expired; a live owner must be able to renew first."""
    job = await rig.create()
    alive_here = rig.platform.engine.owner  # same host, a pid that is running (this test process)
    await rig.set(job, status="submitting", lease_owner=alive_here, lease_expires_at=rig.clock.now_ms() + TTL_MS)
    rig.clock.advance(TTL_MS / 1000 + 5)  # expired, but within the grace period
    assert await other.platform.engine.reap() == []
    await rig.platform.engine.heartbeat()  # the owner wakes up and renews
    rig.clock.advance(TTL_MS / 1000 - 1)
    assert await other.platform.engine.reap() == []
    rig.clock.advance(2 * TTL_MS / 1000 + 5)  # the owner really is gone
    assert await other.platform.engine.reap() == [job]


async def test_losing_the_lease_during_finalize_discards_the_output(rig: Rig, other: Rig) -> None:
    job = await rig.create()
    await rig.step(job)
    rig.clock.advance(1)

    async def steal() -> None:
        # while A downloads the output, its lease lapses (long past any grace) and B completes the job
        await rig.set(job, lease_expires_at=rig.clock.now_ms() - 2 * TTL_MS)
        await other.step(job)

    rig.provider.before_output = steal
    await rig.step(job)
    assert await rig.status(job) == "completed" and await rig.media_count(job) == 1  # registered once, by B
    blob_files = [p for p in (rig.platform.data.blobs / "media").rglob("*") if p.is_file()]
    assert len(blob_files) == 1  # A's copy was discarded


async def test_a_process_without_the_provider_does_not_claim(rig: Rig, other: Rig) -> None:
    other.provider.available = False
    job = await rig.create()
    assert await other.platform.engine.due_jobs() == []
    await other.step(job)
    assert await other.status(job) == "queued"
    assert await rig.platform.engine.due_jobs() == [job]


async def test_heartbeat_renews_and_shutdown_releases_running_leases(rig: Rig) -> None:
    job = await rig.create()
    await rig.step(job)
    owner = rig.platform.engine.owner
    await rig.set(job, lease_owner=owner, lease_expires_at=rig.clock.now_ms() + 5)
    rig.clock.advance(1)
    await rig.platform.engine.heartbeat()
    assert (await rig.row(job))["lease_expires_at"] == rig.clock.now_ms() + TTL_MS
    await rig.platform.engine.release_leases()
    assert (await rig.row(job))["lease_owner"] is None
