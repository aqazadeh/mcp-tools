"""The background runner: the only place where jobs are advanced.

It lives in a task group owned by the platform, not by any request. Tool handlers `poke` it
(non-blocking) and wait for a change signal. Nothing depends on a poke arriving: the loop
also sweeps the database, so a job created by a request that was cancelled right after its
commit, or by another process, is still picked up.
"""

import logging

import anyio
import anyio.abc

from mcp_tools.core.jobs.engine import HEARTBEAT_SECONDS, Engine

logger = logging.getLogger(__name__)

IDLE_TICK_SECONDS = 5.0
MIN_TICK_SECONDS = 0.05


class ChangeSignal:
    """Broadcast "some job changed" to everyone waiting in this process."""

    def __init__(self) -> None:
        self._event = anyio.Event()

    def fire(self) -> None:
        event, self._event = self._event, anyio.Event()
        event.set()

    def token(self) -> anyio.Event:
        """Take this *before* reading state; a change after that point wakes `wait` at once."""
        return self._event

    @staticmethod
    async def wait(token: anyio.Event, timeout: float) -> None:
        with anyio.move_on_after(max(0.0, timeout)):
            await token.wait()


class Runner:
    def __init__(self, engine: Engine, *, concurrency: int = 4, heartbeat_seconds: float = HEARTBEAT_SECONDS) -> None:
        self._engine = engine
        self._heartbeat_seconds = heartbeat_seconds
        self._limiter = anyio.CapacityLimiter(concurrency)
        self._wake = anyio.Event()
        self._poked: set[str] = set()
        self._inflight: set[str] = set()
        self.running = False

    def poke(self, job_id: str | None = None) -> None:
        """Ask for prompt attention. Never blocks; safe to call from a request handler."""
        if job_id is not None:
            self._poked.add(job_id)
        self._wake.set()

    async def run(self) -> None:
        """Run until cancelled."""
        self.running = True
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._heartbeat_loop)
                while True:
                    delay = IDLE_TICK_SECONDS
                    try:
                        delay = await self._tick(tg)
                    except Exception:
                        logger.exception("runner tick failed; continuing")
                    with anyio.move_on_after(delay):
                        await self._wake.wait()
                    self._wake = anyio.Event()
        finally:
            self.running = False
            with anyio.CancelScope(shield=True), anyio.move_on_after(1.0):
                try:
                    await self._engine.release_leases()
                except Exception:
                    logger.debug("could not release leases on shutdown", exc_info=True)

    async def _tick(self, tg: anyio.abc.TaskGroup) -> float:
        await self._engine.reap()
        poked, self._poked = self._poked, set()
        due = await self._engine.due_jobs()
        for job_id in [*poked, *due]:
            if job_id not in self._inflight:
                self._inflight.add(job_id)
                tg.start_soon(self._advance, job_id)
        next_due = await self._engine.next_due_in_ms()
        if next_due is None:
            return IDLE_TICK_SECONDS
        return min(IDLE_TICK_SECONDS, max(MIN_TICK_SECONDS, next_due / 1000))

    async def _advance(self, job_id: str) -> None:
        try:
            async with self._limiter:
                await self._engine.advance(job_id)
        except Exception:
            logger.exception("advancing job %s failed", job_id)
        finally:
            self._inflight.discard(job_id)
            self._wake.set()  # re-evaluate what is due next

    async def _heartbeat_loop(self) -> None:
        while True:
            await anyio.sleep(self._heartbeat_seconds)
            try:
                with anyio.move_on_after(max(0.2, self._heartbeat_seconds * 0.9)):
                    await self._engine.heartbeat()
            except Exception:
                logger.exception("lease heartbeat failed; continuing")
