"""Job state machine.

    queued --claim--> submitting --handle saved--> running --output stored--> completed
       |                  |  '--synchronous result-------------------------> completed
       |                  '--error | owner died--> failed | moderated
       '--cancel | queue TTL--> canceled | failed     running --> failed | canceled | moderated

Rules that make this safe with several server processes on one database:
- Correctness rests on compare-and-set: every transition is `UPDATE ... WHERE status = <expected>
  AND lease_owner = <me>` and checks that exactly one row changed. Leases only avoid duplicate
  submits and wasted polls; they are not what keeps the state consistent.
- A job is sent to a provider at most once. A job found in `submitting` whose owner is gone is
  failed as `interrupted` and refunded; it is never resubmitted, because the provider may have
  accepted it.
- Only code running in the runner's task group calls into this module's `advance`. Tool handlers
  can be cancelled at any await, so they never claim leases or call providers.
- Every terminal state other than `completed` refunds in the same transaction.
"""

import logging
import os
import secrets
import socket
import sys
from dataclasses import dataclass
from typing import Any

import anyio
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.billing.ledger import Ledger, Ref
from mcp_tools.core.catalog.loader import Catalog
from mcp_tools.core.clock import Clock
from mcp_tools.core.errors import ApiError, provider_text
from mcp_tools.core.media.service import MediaService
from mcp_tools.core.principal import Principal
from mcp_tools.db.schema import ACTIVE_JOB_STATUSES, jobs
from mcp_tools.db.store import Store
from mcp_tools.providers.base import (
    MediaInput,
    PollResult,
    Provider,
    ProviderError,
    ProviderRequest,
    Submitted,
)
from mcp_tools.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

LEASE_TTL_MS = 30_000
HEARTBEAT_SECONDS = 10.0
SAME_HOST_GRACE_MS = 30_000  # after a laptop resume every lease looks expired; let a live owner renew first
QUEUED_TTL_MS = 10 * 60 * 1000
POLL_BACKOFF_MS = (1_000, 2_000, 3_000, 5_000)
POLL_MIN_MS = 100
POLL_MAX_MS = 10_000
MAX_DOWNLOAD_RETRIES = 5
POLL_TIMEOUT_S = 60.0
SUBMIT_TIMEOUT_S = 120.0  # asynchronous submit: the provider only has to accept the request
DOWNLOAD_TIMEOUT_S = 600.0
_DOWNLOAD_RETRIES_KEY = "_download_retries"


def make_owner_id(clock: Clock) -> str:
    """host|pid|start: unique per platform instance, and enough to tell whether the owner is alive."""
    return f"{socket.gethostname()}|{os.getpid()}|{clock.now_ms()}-{secrets.token_hex(4)}"


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) is not an existence probe on Windows
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_alive_here(owner: str) -> bool:
    """True if the lease owner is a live process on this host (so its lease may just be late)."""
    try:
        host, pid, _started = owner.split("|")
        return host == socket.gethostname() and _pid_alive(int(pid))
    except (ValueError, OSError, AttributeError):
        return False


@dataclass(frozen=True)
class _Claim:
    row: dict[str, Any]
    prior: str


class Engine:
    def __init__(
        self,
        *,
        store: Store,
        providers: ProviderRegistry,
        media: MediaService,
        ledger: Ledger,
        catalog: Catalog,
        clock: Clock,
        owner: str,
        on_change: Any = None,
        lease_ttl_ms: int = LEASE_TTL_MS,
    ) -> None:
        self._store = store
        self._providers = providers
        self._media = media
        self._ledger = ledger
        self._catalog = catalog
        self._clock = clock
        self.owner = owner
        self._on_change = on_change or (lambda _job_id: None)
        self._lease_ttl_ms = lease_ttl_ms
        self._grace_ms = min(SAME_HOST_GRACE_MS, lease_ttl_ms)
        self._held: set[str] = set()  # jobs this process is working on right now
        self._expired_seen: dict[tuple[str, str, int], int] = {}

    def _may_take_over(self, job_id: str, owner: str, expires: int, now: int) -> bool:
        """May an expired lease of another owner be taken?

        A live owner on this host gets a grace period to renew, counted from the moment this
        process first saw the lease expired. Counting from the stored expiry would not work:
        after a laptop sleep the whole grace has already "passed" at the first look.
        """
        if not _owner_alive_here(owner):
            return True
        if len(self._expired_seen) > 2000:
            self._expired_seen.clear()
        first = self._expired_seen.setdefault((job_id, owner, expires), now)
        return now - first >= self._grace_ms

    # -- scheduling queries ----------------------------------------------------------------
    def _free_lease(self, now: int) -> Any:
        return or_(jobs.c.lease_owner.is_(None), jobs.c.lease_expires_at <= now)

    async def due_jobs(self, limit: int = 32) -> list[str]:
        """Active jobs this process may work on right now."""
        usable = self._providers.usable()
        if not usable:
            return []
        now = self._clock.now_ms()
        query = (
            select(jobs.c.id)
            .where(
                jobs.c.status.in_(("queued", "running")),
                jobs.c.provider.in_(usable),
                self._free_lease(now),
                or_(
                    jobs.c.status == "queued",
                    jobs.c.next_poll_at <= now,
                    jobs.c.cancel_requested_at.is_not(None),
                    jobs.c.deadline_at <= now,
                ),
            )
            .order_by(jobs.c.next_poll_at)
            .limit(limit)
        )
        async with self._store.read_tx() as conn:
            return [row[0] for row in (await conn.execute(query)).all()]

    async def next_due_in_ms(self) -> int | None:
        """Milliseconds until the earliest active job is due; None when nothing is active."""
        usable = self._providers.usable()
        if not usable:
            return None
        now = self._clock.now_ms()
        active = (jobs.c.status.in_(ACTIVE_JOB_STATUSES), jobs.c.provider.in_(usable))
        # A job under a live lease is somebody's work in progress: nothing to do before the lease ends.
        free = select(func.min(jobs.c.next_poll_at)).where(*active, self._free_lease(now))
        leased = select(func.min(jobs.c.lease_expires_at)).where(
            *active, jobs.c.lease_owner.is_not(None), jobs.c.lease_expires_at > now
        )
        async with self._store.read_tx() as conn:
            times = [(await conn.execute(query)).scalar() for query in (free, leased)]
        known = [t for t in times if t is not None]
        return max(0, min(known) - now) if known else None

    async def heartbeat(self) -> None:
        """One statement renews the leases of the jobs this process is working on right now.

        Only those: a lease left behind by a step that failed in an unexpected way must be
        allowed to expire, or the job would be stuck for as long as this process lives.
        """
        held = sorted(self._held)
        if not held:
            return
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> None:
            await conn.execute(
                update(jobs)
                .where(jobs.c.lease_owner == self.owner, jobs.c.id.in_(held), jobs.c.status.in_(ACTIVE_JOB_STATUSES))
                .values(lease_expires_at=now + self._lease_ttl_ms)
            )

        await self._store.write(unit)

    async def release_leases(self) -> None:
        """On shutdown, hand `running` jobs back so another process can poll them at once."""

        async def unit(conn: AsyncConnection) -> None:
            await conn.execute(
                update(jobs)
                .where(jobs.c.lease_owner == self.owner, jobs.c.status == "running")
                .values(lease_owner=None, lease_expires_at=None)
            )

        await self._store.write(unit)

    # -- reaping (needs no provider) ---------------------------------------------------------
    async def reap(self) -> list[str]:
        """End jobs nobody can finish. Needs no provider, so every process can do it.

        - `submitting` with a dead owner: failed as interrupted (never resubmitted).
        - `queued` for too long: failed as expired.
        - `running`, unleased, and well past its deadline or an unserved cancel request: no
          process that can reach its provider is around, so it is closed here and refunded.
        """
        now = self._clock.now_ms()
        margin = 2 * self._lease_ttl_ms
        candidates = or_(
            and_(jobs.c.status == "submitting", jobs.c.lease_expires_at <= now),
            and_(jobs.c.status == "queued", jobs.c.created_at <= now - QUEUED_TTL_MS, self._free_lease(now)),
            and_(
                jobs.c.status == "running",
                self._free_lease(now),
                or_(jobs.c.deadline_at <= now - margin, jobs.c.cancel_requested_at <= now - margin),
            ),
        )
        columns = (
            jobs.c.id,
            jobs.c.workspace_id,
            jobs.c.status,
            jobs.c.lease_owner,
            jobs.c.lease_expires_at,
            jobs.c.cancel_requested_at,
        )
        async with self._store.read_tx() as conn:  # the common case is "nothing to do": avoid a write lock
            if (await conn.execute(select(jobs.c.id).where(candidates).limit(1))).first() is None:
                return []

        async def unit(conn: AsyncConnection) -> list[tuple[str, str]]:
            rows = (await conn.execute(select(*columns).where(candidates).limit(100))).mappings().all()
            reaped: list[tuple[str, str]] = []
            for row in rows:
                owner = row["lease_owner"]
                status = "failed"
                if row["status"] == "submitting":
                    if row["id"] in self._held:
                        continue  # our own call is still in flight; the heartbeat is merely late
                    if (
                        owner
                        and owner != self.owner
                        and not self._may_take_over(row["id"], owner, row["lease_expires_at"] or 0, now)
                    ):
                        continue
                    code, message = "interrupted", "the server stopped while the request was being submitted"
                elif row["status"] == "queued":
                    code, message = "expired", "the job waited in the queue for too long and was not started"
                else:
                    if (
                        owner
                        and owner != self.owner
                        and not self._may_take_over(row["id"], owner, row["lease_expires_at"] or 0, now)
                    ):
                        continue
                    if row["cancel_requested_at"] is not None:
                        status, code, message = "canceled", "canceled", "canceled on request"
                    else:
                        code, message = "timeout", "the provider did not finish before the deadline"
                changed = await conn.execute(
                    update(jobs)
                    .where(jobs.c.id == row["id"], jobs.c.status == row["status"])
                    .values(
                        status=status,
                        error_code=code,
                        error_message=message,
                        finished_at=now,
                        updated_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                )
                if changed.rowcount == 1:
                    await self._ledger.refund(conn, row["workspace_id"], Ref("job", row["id"]), note=code)
                    reaped.append((row["id"], code))
            return reaped

        reaped = await self._store.write(unit)
        for job_id, code in reaped:
            logger.warning("job %s failed: %s", job_id, code)
            self._on_change(job_id)
        return [job_id for job_id, _ in reaped]

    # -- claim -------------------------------------------------------------------------------
    async def _claim(self, job_id: str) -> _Claim | None:
        usable = set(self._providers.usable())
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> _Claim | None:
            row = (
                (await conn.execute(select(jobs).where(jobs.c.id == job_id).with_for_update(skip_locked=True)))
                .mappings()
                .first()
            )
            if row is None or row["status"] not in ("queued", "running") or row["provider"] not in usable:
                return None
            owner, expires = row["lease_owner"], row["lease_expires_at"]
            if owner is not None:
                if expires is not None and expires > now:
                    return None
                if owner != self.owner and not self._may_take_over(job_id, owner, expires or 0, now):
                    return None
            prior = row["status"]
            due = (
                prior == "queued"
                or row["next_poll_at"] <= now
                or row["cancel_requested_at"] is not None
                or row["deadline_at"] <= now
            )
            if not due:
                return None
            lease_matches = jobs.c.lease_owner.is_(None) if owner is None else jobs.c.lease_owner == owner
            changed = await conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status == prior, lease_matches)
                .values(
                    status="submitting" if prior == "queued" else prior,
                    lease_owner=self.owner,
                    lease_expires_at=now + self._lease_ttl_ms,
                    attempts=jobs.c.attempts + 1,
                    updated_at=now,
                )
            )
            return _Claim(row=dict(row), prior=prior) if changed.rowcount == 1 else None

        claim = await self._store.write(unit)
        if claim is not None:
            self._held.add(job_id)
        return claim

    # -- transitions -------------------------------------------------------------------------
    async def _transition(
        self,
        row: dict[str, Any],
        *,
        expect: tuple[str, ...],
        values: dict[str, Any],
        refund_note: str | None = None,
        extra: Any = None,
    ) -> bool:
        """Compare-and-set a job this process holds. Returns False if the lease was lost."""
        job_id = row["id"]
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> bool:
            changed = await conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status.in_(expect), jobs.c.lease_owner == self.owner)
                .values(updated_at=now, **values)
            )
            if changed.rowcount != 1:
                return False
            if extra is not None:
                await extra(conn)
            if refund_note is not None:
                await self._ledger.refund(conn, row["workspace_id"], Ref("job", job_id), note=refund_note)
            return True

        changed = await self._store.write(unit)
        if changed:
            self._on_change(job_id)
        else:
            logger.info("job %s: lease lost, discarding this step", job_id)
        return changed

    async def _finish(self, row: dict[str, Any], status: str, code: str | None, message: str | None) -> bool:
        """Move to a terminal state other than `completed`, refunding in the same transaction."""
        return await self._transition(
            row,
            expect=("submitting", "running"),
            values={
                "status": status,
                "error_code": code,
                "error_message": provider_text(message),
                "finished_at": self._clock.now_ms(),
                "lease_owner": None,
                "lease_expires_at": None,
            },
            refund_note=code or status,
        )

    @staticmethod
    def _poll_delay_ms(attempts: int, retry_after: float | None) -> int:
        """Honour the provider's hint when it gives one; otherwise back off 1, 2, 3, 5 s."""
        if retry_after is not None:
            return int(min(max(retry_after * 1000, POLL_MIN_MS), POLL_MAX_MS))
        return POLL_BACKOFF_MS[min(max(attempts - 1, 0), len(POLL_BACKOFF_MS) - 1)]

    async def _reschedule(
        self,
        row: dict[str, Any],
        *,
        progress: float | None,
        retry_after: float | None,
        state: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "next_poll_at": self._clock.now_ms() + self._poll_delay_ms(row["attempts"], retry_after),
            "lease_owner": None,
            "lease_expires_at": None,
        }
        if progress is not None:
            values["progress"] = progress
        if state is not None:
            values["remote_state_json"] = state
        await self._transition(row, expect=("running",), values=values)

    # -- the step ----------------------------------------------------------------------------
    async def advance(self, job_id: str) -> bool:
        """Do one step for a job: submit it, poll it, or finish it. Runner task group only.

        Returns False when the job could not be claimed (nothing was done).
        """
        claim = await self._claim(job_id)
        if claim is None:
            return False
        row = claim.row
        try:
            await self._step(claim)
        except BaseException as exc:
            # Whatever went wrong, the lease must not be left behind with the job half-way.
            with anyio.CancelScope(shield=True):
                try:
                    if claim.prior == "queued":
                        await self._finish(
                            row, "failed", "interrupted", "the server stopped or failed while starting the job"
                        )
                    else:
                        await self._reschedule(row, progress=None, retry_after=None)
                except Exception:
                    logger.exception("job %s: could not release the lease after a failed step", job_id)
            if not isinstance(exc, Exception):
                raise
            logger.exception("job %s: step failed unexpectedly", job_id)
        finally:
            self._held.discard(job_id)
        return True

    async def _step(self, claim: _Claim) -> None:
        row, now = claim.row, self._clock.now_ms()
        provider = self._providers.peek(row["provider"])
        assert provider is not None

        if claim.prior == "queued":
            if row["cancel_requested_at"] is not None:
                await self._finish(row, "canceled", "canceled", "canceled before it started")
            elif row["created_at"] <= now - QUEUED_TTL_MS:
                await self._finish(row, "failed", "expired", "the job waited in the queue for too long")
            else:
                await self._submit(row, provider)
            return

        # running
        if row["cancel_requested_at"] is not None:
            await self._cancel_remote(row, provider)
            await self._finish(row, "canceled", "canceled", "canceled on request")
            return
        overdue = row["deadline_at"] <= now
        try:
            with anyio.fail_after(POLL_TIMEOUT_S):
                result = await provider.poll(row["remote_id"] or "", row["remote_state_json"] or {})
        except (ProviderError, TimeoutError) as exc:
            if overdue:
                await self._time_out(row, provider)
            elif isinstance(exc, TimeoutError) or exc.retryable:
                await self._reschedule(row, progress=None, retry_after=None)
            else:
                await self._finish(row, "failed", exc.code, exc.message)
            return
        # Past the deadline the job gets this one last look: a result the provider already
        # produced (say, while the laptop slept) is kept rather than thrown away.
        if overdue and result.state == "running":
            await self._time_out(row, provider)
            return
        await self._apply(row, result, provider)

    async def _time_out(self, row: dict[str, Any], provider: Provider) -> None:
        await self._cancel_remote(row, provider)
        await self._finish(row, "failed", "timeout", "the provider did not finish before the deadline")

    async def _cancel_remote(self, row: dict[str, Any], provider: Provider) -> None:
        if not row["remote_id"]:
            return
        try:
            with anyio.move_on_after(10):
                await provider.cancel(row["remote_id"], row["remote_state_json"] or {})
        except Exception:
            logger.info("job %s: remote cancel failed (best effort)", row["id"], exc_info=True)

    async def _request(self, row: dict[str, Any]) -> ProviderRequest:
        params = row["params_json"]
        model = self._catalog.get(row["model_id"])
        principal = Principal(workspace_id=row["workspace_id"])
        refs = params.get("medias") or []
        found = {m.id: m for m in await self._media.get_many([r["media_id"] for r in refs], principal=principal)}
        inputs = []
        for ref in refs:
            media = found.get(ref["media_id"])
            path = self._media.local_path(media) if media else None
            if media is None or path is None or not path.is_file():
                raise ProviderError("input_unavailable", f"reference media {ref['media_id']} is no longer available")
            inputs.append(
                MediaInput(
                    role=ref["role"],
                    media_id=media.id,
                    kind=media.kind,
                    mime=media.mime,
                    path=path,
                    size=media.size_bytes or 0,
                )
            )
        return ProviderRequest(
            job_id=row["id"],
            model=model,
            prompt=params.get("prompt"),
            aspect_ratio=params.get("aspect_ratio"),
            duration=params.get("duration"),
            params=dict(params.get("params") or {}),
            medias=tuple(inputs),
        )

    async def _submit(self, row: dict[str, Any], provider: Provider) -> None:
        try:
            request = await self._request(row)
        except (ProviderError, ApiError) as exc:
            await self._finish(row, "failed", getattr(exc, "code", "invalid_input"), getattr(exc, "message", str(exc)))
            return
        asynchronous = request.model.execution == "async"
        submitted: Submitted | None = None
        try:
            # An asynchronous submit is quick: shield it together with saving the remote handle so
            # a shutdown cannot fall between "the provider accepted" and "we remember the handle".
            # A synchronous call can take a minute, so it stays cancellable (and is failed below).
            with anyio.CancelScope(shield=asynchronous):
                left = max(1.0, (row["deadline_at"] - self._clock.now_ms()) / 1000)
                try:
                    with anyio.fail_after(min(SUBMIT_TIMEOUT_S, left) if asynchronous else left):
                        submitted = await provider.submit(request)
                except TimeoutError:
                    raise ProviderError("timeout", "the provider did not answer in time", ambiguous=True) from None
                if submitted.result is None:
                    now = self._clock.now_ms()
                    saved = await self._transition(
                        row,
                        expect=("submitting",),
                        values={
                            "status": "running",
                            "remote_id": submitted.remote_id,
                            "remote_state_json": submitted.state,
                            "started_at": now,
                            "next_poll_at": now + self._poll_delay_ms(1, submitted.retry_after),
                            "lease_owner": None,
                            "lease_expires_at": None,
                        },
                    )
                    if not saved and submitted.remote_id:
                        # The lease was lost while the provider accepted the job: nobody will poll it.
                        await self._cancel_remote(
                            {**row, "remote_id": submitted.remote_id, "remote_state_json": submitted.state}, provider
                        )
                    return
        except ProviderError as exc:
            if exc.ambiguous:
                await self._finish(
                    row,
                    "failed",
                    "submit_outcome_unknown",
                    "the provider may or may not have accepted the request; it was not sent again",
                )
            else:
                await self._finish(
                    row, "moderated" if exc.code == "content_policy" else "failed", exc.code, exc.message
                )
            return
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self._finish(
                    row, "failed", "interrupted", "the server stopped while the provider call was in flight"
                )
            raise
        assert submitted is not None
        assert submitted.result is not None
        await self._apply(row, submitted.result, provider, synchronous=True)

    async def _apply(
        self, row: dict[str, Any], result: PollResult, provider: Provider, *, synchronous: bool = False
    ) -> None:
        if result.state == "running":
            if synchronous:
                await self._finish(row, "failed", "provider_error", "the provider returned no result")
            else:
                await self._reschedule(row, progress=result.progress, retry_after=result.retry_after)
        elif result.state == "succeeded":
            await self._finalize(row, result, provider, synchronous=synchronous)
        elif result.state == "moderated":
            await self._finish(row, "moderated", result.error_code or "content_policy", result.error_message)
        elif result.state == "canceled":
            await self._finish(row, "canceled", result.error_code or "canceled", result.error_message)
        else:
            await self._finish(row, "failed", result.error_code or "provider_error", result.error_message)

    async def _finalize(
        self, row: dict[str, Any], result: PollResult, provider: Provider, *, synchronous: bool
    ) -> None:
        """Copy the output into our store, then mark the job completed in one fenced transaction."""
        if not result.outputs:
            await self._finish(row, "failed", "no_output", "the provider reported success without an output")
            return
        if len(result.outputs) > 1:
            logger.info("job %s: provider returned %d outputs, keeping the first", row["id"], len(result.outputs))
        state = dict(row["remote_state_json"] or {})
        retries = int(state.get(_DOWNLOAD_RETRIES_KEY) or 0)
        try:
            with anyio.fail_after(DOWNLOAD_TIMEOUT_S):
                async with provider.open_output(next(iter(result.outputs))) as chunks:
                    blob = await self._media.store_stream(chunks, accept=frozenset({row["output_type"]}))
        except (ProviderError, TimeoutError) as exc:
            retryable = isinstance(exc, TimeoutError) or exc.retryable
            if retryable and not synchronous and retries < MAX_DOWNLOAD_RETRIES:
                state[_DOWNLOAD_RETRIES_KEY] = retries + 1
                await self._reschedule(row, progress=None, retry_after=2.0, state=state)
            else:
                message = "the output could not be downloaded in time" if isinstance(exc, TimeoutError) else exc.message
                await self._finish(row, "failed", "output_unavailable", message)
            return
        except ApiError as exc:
            await self._finish(row, "failed", "invalid_output", exc.message)
            return

        principal = Principal(workspace_id=row["workspace_id"])

        async def register(conn: AsyncConnection) -> None:
            await self._media.insert(
                conn, blob, principal=principal, source="job_output", job_id=row["id"], output_index=0
            )

        values: dict[str, Any] = {
            "status": "completed",
            "progress": 1.0,
            "finished_at": self._clock.now_ms(),
            "lease_owner": None,
            "lease_expires_at": None,
        }
        if result.usage:
            values["remote_state_json"] = {**state, "usage": result.usage}
            logger.info("job %s (%s): provider usage %s", row["id"], row["model_id"], result.usage)
        completed = False
        try:
            completed = await self._transition(
                row,
                expect=("submitting", "running"),
                values=values,
                extra=register,
            )
        finally:
            if not completed:
                await self._media.discard_key(blob.key)
