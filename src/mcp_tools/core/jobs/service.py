"""Job API used by request handlers: create, quote, read, wait, cancel.

Nothing here calls a provider. `create` is one write transaction that validates, prices,
checks the budget, inserts `queued` rows and debits credits; the runner does the rest.
"""

from dataclasses import dataclass, field
from typing import Any

import anyio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.blobs.base import UrlIssuer
from mcp_tools.core.billing.idempotency import IdempotencyStore, check_key, request_hash
from mcp_tools.core.billing.ledger import Ledger, Ref
from mcp_tools.core.catalog.loader import Catalog
from mcp_tools.core.catalog.normalize import NormalizedRequest, normalize_request
from mcp_tools.core.catalog.pricing import Quote, quote
from mcp_tools.core.catalog.spec import ModelSpec
from mcp_tools.core.clock import Clock
from mcp_tools.core.errors import BUDGET_EXCEEDED, IDEMPOTENCY_KEY_REUSED, INVALID_PARAMS, NOT_FOUND, ApiError
from mcp_tools.core.ids import is_id, new_id
from mcp_tools.core.jobs.runner import ChangeSignal, Runner
from mcp_tools.core.jobs.views import JobView, MediaView, job_view, media_view
from mcp_tools.core.media.service import MediaRow, MediaService
from mcp_tools.core.pagination import Page, PageRequest, encode_cursor
from mcp_tools.core.principal import Principal
from mcp_tools.core.projects.service import ProjectService
from mcp_tools.db.schema import ACTIVE_JOB_STATUSES, jobs
from mcp_tools.db.store import Store
from mcp_tools.providers.registry import ProviderRegistry

MAX_WAIT_JOBS = 12
MAX_BATCH = 12
# The SDK switches a silent response to SSE after 15 s; stay below it so the response shape is stable.
MAX_WAIT_SECONDS = 12.0
URL_TTL_SECONDS = 15 * 60

_GENERATE_TOOL = {"image": "generate_image", "video": "generate_video", "audio": "generate_audio"}


@dataclass(frozen=True)
class GenerationRequest:
    index: int
    params: dict[str, Any]


@dataclass
class _Prepared:
    index: int
    model: ModelSpec
    request: NormalizedRequest
    quote: Quote
    medias: list[dict[str, str]]

    def canonical(self) -> dict[str, Any]:
        body = self.request.canonical()
        body["medias"] = self.medias
        body["count"] = self.request.count
        return body


@dataclass(frozen=True)
class CreatedJob:
    index: int
    job_id: str


@dataclass
class ItemQuote:
    index: int
    model: str
    credits: int
    breakdown: dict[str, Any]
    adjustments: list[dict[str, Any]]


@dataclass
class Outcome:
    """Result of `create` / `quote`. `status` is 'submitted', 'approval_required' or 'quoted'."""

    status: str
    items: list[ItemQuote] = field(default_factory=list)
    errors: dict[int, ApiError] = field(default_factory=dict)
    jobs: list[CreatedJob] = field(default_factory=list)
    credits_total: int = 0
    balance: int | None = None
    group_id: str | None = None
    replayed: bool = False
    reason: str | None = None
    limit: int | None = None


@dataclass(frozen=True)
class WaitResult:
    jobs: list[JobView]
    all_terminal: bool
    poll_after_seconds: int | None


class JobService:
    def __init__(
        self,
        *,
        store: Store,
        catalog: Catalog,
        providers: ProviderRegistry,
        media: MediaService,
        projects: ProjectService,
        ledger: Ledger,
        idempotency: IdempotencyStore,
        runner: Runner,
        signal: ChangeSignal,
        clock: Clock,
        urls: UrlIssuer,
        expose_local_paths: bool,
        max_credits_per_call: int,
        auto_approve_credits: int,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._providers = providers
        self._media = media
        self._projects = projects
        self._ledger = ledger
        self._idempotency = idempotency
        self._runner = runner
        self._signal = signal
        self._clock = clock
        self._urls = urls
        self._expose_local_paths = expose_local_paths
        self._max_per_call = max_credits_per_call
        self._auto_approve = auto_approve_credits

    # -- preparing -------------------------------------------------------------------------
    async def _resolve_medias(
        self, model: ModelSpec, request: NormalizedRequest, principal: Principal
    ) -> list[dict[str, str]]:
        """Turn each media reference (a media id or a completed job id) into a concrete media id."""
        if not request.medias:
            return []
        media_ids = [m.value for m in request.medias if is_id(m.value, "med")]
        job_ids = [m.value for m in request.medias if is_id(m.value, "job")]
        by_id: dict[str, MediaRow] = {m.id: m for m in await self._media.get_many(media_ids, principal=principal)}
        outputs = await self._media.for_jobs(job_ids, principal=principal) if job_ids else {}
        resolved: list[dict[str, str]] = []
        for ref in request.medias:
            if is_id(ref.value, "job"):
                rows = outputs.get(ref.value)
                if not rows:
                    raise ApiError(
                        INVALID_PARAMS,
                        f"job {ref.value} has no finished output to use as a reference",
                        hint="Wait until the job is completed, then retry.",
                        recovery_tool="jobs_wait",
                    )
                row = rows[0]
            else:
                found = by_id.get(ref.value)
                if found is None:
                    raise ApiError(
                        NOT_FOUND,
                        f"no media with id {ref.value!r}",
                        hint="Upload or import the file first and use the returned media_id.",
                        recovery_tool="media_list",
                    )
                if found.status != "ready":
                    raise ApiError(
                        INVALID_PARAMS,
                        f"media {ref.value} is not ready (status {found.status})",
                        hint="Finish the upload and confirm it, then retry.",
                        recovery_tool="media_list",
                    )
                row = found
            role = model.media_role(ref.role)
            assert role is not None
            if row.kind not in role.kinds:
                raise ApiError(
                    INVALID_PARAMS,
                    f"role {ref.role!r} of model {model.id} takes {' or '.join(role.kinds)}, got a {row.kind}",
                    hint="Use a file of the right kind for that role.",
                    recovery_tool="models_explore",
                    allowed=list(role.kinds),
                )
            resolved.append({"role": ref.role, "value": ref.value, "media_id": row.id})
        return resolved

    async def _prepare(
        self, item: GenerationRequest, *, principal: Principal, output_type: str | None, task: str
    ) -> _Prepared:
        raw = item.params
        model_id = raw.get("model")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ApiError(
                INVALID_PARAMS,
                "params.model is required",
                hint="Pick a model id with models_explore.",
                recovery_tool="models_explore",
            )
        model = self._catalog.get(model_id.strip())
        if model.task != task:
            raise ApiError(
                INVALID_PARAMS,
                f"model {model.id} is a {model.task} model and cannot be used here",
                hint=f"Use a {task} model: omit `model` on single-purpose tools, or find one with models_explore "
                f"action 'search' (items there carry task: '{task}' unless they are generation models).",
                recovery_tool="models_explore",
            )
        if output_type is not None and model.output_type != output_type:
            raise ApiError(
                INVALID_PARAMS,
                f"model {model.id} produces {model.output_type}, not {output_type}",
                hint=f"Call {_GENERATE_TOOL[model.output_type]} for this model.",
                recovery_tool=_GENERATE_TOOL[model.output_type],
            )
        self._providers.get(model.provider)  # raises provider_unavailable
        request = normalize_request(model, raw)
        medias = await self._resolve_medias(model, request, principal)
        if request.folder_id:
            await self._projects.require_folder(request.folder_id, principal)
        return _Prepared(index=item.index, model=model, request=request, quote=quote(model, request), medias=medias)

    async def _prepare_all(
        self,
        requests: list[GenerationRequest],
        *,
        principal: Principal,
        output_type: str | None,
        task: str,
        strict: bool,
    ) -> tuple[list[_Prepared], dict[int, ApiError]]:
        if not requests or len(requests) > MAX_BATCH:
            raise ApiError(INVALID_PARAMS, f"send 1 to {MAX_BATCH} requests", hint="Split larger sets.")
        if len({r.index for r in requests}) != len(requests):
            raise ApiError(INVALID_PARAMS, "request indices must be unique", hint="Number the requests 0, 1, 2 …")
        prepared: list[_Prepared] = []
        errors: dict[int, ApiError] = {}
        for item in requests:
            try:
                prepared.append(await self._prepare(item, principal=principal, output_type=output_type, task=task))
            except ApiError as exc:
                if strict:
                    raise
                errors[item.index] = exc
        return prepared, errors

    @staticmethod
    def _items(prepared: list[_Prepared]) -> list[ItemQuote]:
        return [
            ItemQuote(
                index=p.index,
                model=p.model.id,
                credits=p.quote.total,
                breakdown=p.quote.breakdown(),
                adjustments=p.request.adjustments,
            )
            for p in prepared
        ]

    # -- quote / create --------------------------------------------------------------------
    async def quote(
        self, requests: list[GenerationRequest], *, principal: Principal, task: str = "generate"
    ) -> Outcome:
        """Price requests without creating anything. A quote does not reserve credits."""
        prepared, errors = await self._prepare_all(
            requests, principal=principal, output_type=None, task=task, strict=False
        )
        async with self._store.read_tx() as conn:
            balance = (await self._ledger.balance(conn, principal.workspace_id)).balance
        return Outcome(
            status="quoted",
            items=self._items(prepared),
            errors=errors,
            credits_total=sum(p.quote.total for p in prepared),
            balance=balance,
        )

    async def create(
        self,
        requests: list[GenerationRequest],
        *,
        principal: Principal,
        tool: str,
        output_type: str | None,
        task: str = "generate",
        max_credits: int | None = None,
        idempotency_key: str | None = None,
        client_context: str | None = None,
        strict: bool = True,
    ) -> Outcome:
        key = check_key(idempotency_key) if idempotency_key else None
        if max_credits is not None and max_credits < 0:
            raise ApiError(INVALID_PARAMS, "max_credits must not be negative", hint="Omit it or pass a cap in credits.")
        if key is not None:
            # A retry of a call that already went through must be answered with that call's result,
            # whatever the budget, the balance or the provider's availability look like now.
            async with self._store.read_tx() as conn:
                stored = await self._idempotency.lookup(conn, principal.workspace_id, tool, key)
            if stored is not None:
                return await self._replay(
                    stored, requests, principal=principal, tool=tool, output_type=output_type, task=task, strict=strict
                )
        prepared, errors = await self._prepare_all(
            requests, principal=principal, output_type=output_type, task=task, strict=strict
        )
        items = self._items(prepared)
        total = sum(p.quote.total for p in prepared)
        if not prepared:
            return Outcome(status="submitted", items=items, errors=errors)

        # Budget: all or nothing. The per-call ceiling and the daily cap are server configuration;
        # max_credits can only lower what a call may spend, never raise it.
        if total > self._max_per_call:
            raise ApiError(
                BUDGET_EXCEEDED,
                f"this call is estimated at {total} credits, above the server's per-call ceiling of "
                f"{self._max_per_call}",
                hint="Split the work into smaller calls or lower count / duration / resolution. "
                "Only the server operator can raise the ceiling.",
                recovery_tool="estimate_cost",
                details={"estimated": total, "ceiling": self._max_per_call},
            )
        if max_credits is not None and total > max_credits:
            return Outcome(
                status="approval_required",
                items=items,
                errors=errors,
                credits_total=total,
                reason="above_max_credits",
                limit=max_credits,
            )
        if max_credits is None and total > self._auto_approve:
            return Outcome(
                status="approval_required",
                items=items,
                errors=errors,
                credits_total=total,
                reason="above_auto_approve",
                limit=self._auto_approve,
            )

        digest = self._digest(tool, prepared)
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> tuple[bool, dict[str, Any]]:
            if key is not None:
                stored = await self._idempotency.begin(conn, principal.workspace_id, tool, key, digest)
                if stored is not None:
                    return True, stored
            group_id = new_id("grp", now)
            created: list[dict[str, Any]] = []
            position = 0
            for p in prepared:
                canonical = p.canonical()
                # Re-checked inside the transaction: the folder may have been deleted since the read.
                folder = (
                    await self._projects.folder_row(conn, p.request.folder_id, principal)
                    if p.request.folder_id
                    else None
                )
                for _ in range(p.request.count):
                    job_id = new_id("job", now)
                    await conn.execute(
                        jobs.insert().values(
                            id=job_id,
                            workspace_id=principal.workspace_id,
                            group_id=group_id,
                            group_index=position,
                            tool=tool,
                            output_type=p.model.output_type,
                            model_id=p.model.id,
                            provider=p.model.provider,
                            status="queued",
                            params_json=canonical,
                            adjustments_json=p.request.adjustments or None,
                            next_poll_at=now,
                            deadline_at=now + p.model.deadline_seconds * 1000,
                            attempts=0,
                            credits=p.quote.per_job,
                            folder_id=p.request.folder_id,
                            client_context=(client_context or None),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await self._ledger.spend(
                        conn, principal.workspace_id, p.quote.per_job, Ref("job", job_id), note=p.model.id
                    )
                    if folder is not None:
                        await self._projects.place(conn, folder, "job", job_id, now)
                    created.append({"index": p.index, "job_id": job_id})
                    position += 1
            balance = (await self._ledger.balance(conn, principal.workspace_id)).balance
            response = {"group_id": group_id, "jobs": created, "credits_total": total, "balance": balance}
            if key is not None:
                await self._idempotency.finish(conn, principal.workspace_id, tool, key, response)
            return False, response

        def wake(result: tuple[bool, dict[str, Any]]) -> None:
            for job in result[1]["jobs"]:
                self._runner.poke(job["job_id"])

        replayed, response = await self._store.write(unit, after_commit=wake)
        balance = response["balance"]
        if replayed:  # lost a race with an identical call: report the balance as it is now
            async with self._store.read_tx() as conn:
                balance = (await self._ledger.balance(conn, principal.workspace_id)).balance
        return Outcome(
            status="submitted",
            items=items,
            errors=errors,
            jobs=[CreatedJob(index=j["index"], job_id=j["job_id"]) for j in response["jobs"]],
            credits_total=response["credits_total"],
            balance=balance,
            group_id=response["group_id"],
            replayed=replayed,
        )

    @staticmethod
    def _digest(tool: str, prepared: list[_Prepared]) -> str:
        return request_hash({"tool": tool, "items": [{"index": p.index, **p.canonical()} for p in prepared]})

    async def _replay(
        self,
        stored: tuple[str, dict[str, Any]],
        requests: list[GenerationRequest],
        *,
        principal: Principal,
        tool: str,
        output_type: str | None,
        task: str,
        strict: bool,
    ) -> Outcome:
        stored_digest, response = stored
        items: list[ItemQuote] = []
        errors: dict[int, ApiError] = {}
        try:
            prepared, errors = await self._prepare_all(
                requests, principal=principal, output_type=output_type, task=task, strict=strict
            )
        except ApiError:
            pass  # the request cannot be normalised any more (say, the provider is gone): replay on the key
        else:
            if self._digest(tool, prepared) != stored_digest:
                raise ApiError(
                    IDEMPOTENCY_KEY_REUSED,
                    "this idempotency_key was already used with a different request",
                    hint="Use a new key for a new request; reuse a key only to retry the identical request.",
                )
            items = self._items(prepared)
        for job in response["jobs"]:
            self._runner.poke(job["job_id"])
        async with self._store.read_tx() as conn:
            balance = (await self._ledger.balance(conn, principal.workspace_id)).balance
        return Outcome(
            status="submitted",
            items=items,
            errors=errors,
            jobs=[CreatedJob(index=j["index"], job_id=j["job_id"]) for j in response["jobs"]],
            credits_total=response["credits_total"],
            balance=balance,
            group_id=response["group_id"],
            replayed=True,
        )

    # -- reading ---------------------------------------------------------------------------
    def media_view(self, row: MediaRow) -> MediaView:
        path = self._media.local_path(row) if self._expose_local_paths else None
        return media_view(
            row,
            local_path=str(path) if path is not None else None,
            url=self._urls.download_url(row.id, ttl_seconds=URL_TTL_SECONDS),
        )

    async def get(self, job_ids: list[str], *, principal: Principal) -> list[JobView]:
        """Views in the order asked. Unknown ids come back as status 'not_found'."""
        wanted = [j for j in job_ids if is_id(j, "job")]
        rows: dict[str, Any] = {}
        if wanted:
            async with self._store.read_tx() as conn:
                found = (
                    (
                        await conn.execute(
                            select(jobs).where(jobs.c.id.in_(wanted), jobs.c.workspace_id == principal.workspace_id)
                        )
                    )
                    .mappings()
                    .all()
                )
            rows = {r["id"]: r for r in found}
        completed = [job_id for job_id, r in rows.items() if r["status"] == "completed"]
        outputs = await self._media.for_jobs(completed, principal=principal)
        views = []
        for job_id in job_ids:
            row = rows.get(job_id)
            if row is None:
                views.append(JobView(job_id=str(job_id), status="not_found"))
            else:
                views.append(job_view(row, [self.media_view(m) for m in outputs.get(job_id, [])]))
        return views

    async def history(
        self,
        page: PageRequest,
        *,
        principal: Principal,
        output_type: str | None = None,
        only_completed: bool = False,
        folder_id: str | None = None,
    ) -> Page[JobView]:
        """Newest first, keyset-paginated on the (time-sortable) id."""
        query = select(jobs.c.id).where(jobs.c.workspace_id == principal.workspace_id)
        if output_type:
            query = query.where(jobs.c.output_type == output_type)
        if only_completed:
            query = query.where(jobs.c.status == "completed")
        if folder_id:
            query = query.where(jobs.c.folder_id == folder_id)
        before = (page.cursor or {}).get("before")
        if isinstance(before, str):
            query = query.where(jobs.c.id < before)
        async with self._store.read_tx() as conn:
            ids = list((await conn.execute(query.order_by(jobs.c.id.desc()).limit(page.limit + 1))).scalars())
        has_more = len(ids) > page.limit
        ids = ids[: page.limit]
        views = await self.get(ids, principal=principal) if ids else []
        return Page(
            items=views,
            has_more=has_more,
            next_cursor=encode_cursor({"before": ids[-1]}) if has_more and ids else None,
        )

    async def wait(self, job_ids: list[str], *, timeout: float, principal: Principal) -> WaitResult:
        """Long-poll: return when every job is terminal or the time is up. Reads only."""
        if not job_ids or len(job_ids) > MAX_WAIT_JOBS:
            raise ApiError(
                INVALID_PARAMS,
                f"pass 1 to {MAX_WAIT_JOBS} job ids",
                hint="Wait for larger sets in groups.",
            )
        budget = min(max(timeout, 0.0), MAX_WAIT_SECONDS)
        deadline = anyio.current_time() + budget
        while True:
            token = self._signal.token()  # before reading, so a change in between is not missed
            views = await self.get(job_ids, principal=principal)
            pending = [v for v in views if not v.terminal]
            remaining = deadline - anyio.current_time()
            if not pending or remaining <= 0:
                break
            for view in pending:
                self._runner.poke(view.job_id)
            await self._signal.wait(token, min(remaining, 1.0))
        all_terminal = not pending
        poll_after = None
        if not all_terminal:
            now = self._clock.now_ms()
            soonest = min((v.next_poll_at or now) for v in pending)
            poll_after = int(min(10, max(2, round((soonest - now) / 1000))))
        return WaitResult(jobs=views, all_terminal=all_terminal, poll_after_seconds=poll_after)

    async def cancel(self, job_id: str, *, principal: Principal) -> tuple[JobView, int, bool]:
        """Request cancellation. Returns the view, the credits refunded right away, and whether it was unfinished."""
        if not is_id(job_id, "job"):
            raise ApiError(NOT_FOUND, f"no job with id {job_id!r}", hint="Use a job_id returned by a generate tool.")
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> tuple[int, bool] | None:
            row = (
                (
                    await conn.execute(
                        select(jobs.c.status, jobs.c.lease_owner, jobs.c.lease_expires_at).where(
                            jobs.c.id == job_id, jobs.c.workspace_id == principal.workspace_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["status"] not in ACTIVE_JOB_STATUSES:
                return 0, False
            unleased = row["lease_owner"] is None or (row["lease_expires_at"] or 0) <= now
            if row["status"] == "queued" and unleased:
                # Nothing was sent to a provider yet: cancel here and now.
                changed = await conn.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id, jobs.c.status == "queued")
                    .values(
                        status="canceled",
                        error_code="canceled",
                        error_message="canceled before it started",
                        cancel_requested_at=now,
                        finished_at=now,
                        updated_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                )
                if changed.rowcount == 1:
                    refund = await self._ledger.refund(
                        conn, principal.workspace_id, Ref("job", job_id), note="canceled"
                    )
                    return refund, True
            await conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status.in_(ACTIVE_JOB_STATUSES))
                .values(cancel_requested_at=now, updated_at=now)
            )
            return 0, True

        outcome = await self._store.write(unit, after_commit=lambda _r: self._runner.poke(job_id))
        if outcome is None:
            raise ApiError(NOT_FOUND, f"no job with id {job_id!r}", hint="Use a job_id returned by a generate tool.")
        self._signal.fire()
        (view,) = await self.get([job_id], principal=principal)
        return view, outcome[0], outcome[1]
