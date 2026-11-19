"""The platform core: everything the MCP facade (or any other front end) talks to.

`Platform` is constructed eagerly and started inside the server lifespan. The lifespan is
entered once per transport run and once per in-process test client, so `running()` is
re-entrant: the first entry starts the platform, the last exit stops it. Stopping must be
quick (stdio hosts kill the process about two seconds after closing stdin) and idempotent.

The job runner lives in its own long-lived task rather than in the task of whichever caller
entered first: a task group must be exited by the task that entered it, and with several
overlapping entries the last one out is not necessarily the first one in.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import anyio
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.blobs.base import NullIssuer, UrlIssuer
from mcp_tools.blobs.local import LocalBlobStore
from mcp_tools.config import Settings
from mcp_tools.core.appreg.builtin import image_tools, storyboard
from mcp_tools.core.appreg.registry import AppRegistry
from mcp_tools.core.billing.idempotency import IdempotencyStore
from mcp_tools.core.billing.ledger import Ledger
from mcp_tools.core.catalog.loader import Catalog, load_catalog
from mcp_tools.core.clock import Clock, SystemClock
from mcp_tools.core.datadir import DataDir, prepare_data_dir
from mcp_tools.core.jobs.engine import Engine, make_owner_id
from mcp_tools.core.jobs.runner import ChangeSignal, Runner
from mcp_tools.core.jobs.service import JobService
from mcp_tools.core.media.fetch import Egress, SafeFetcher, detect_egress
from mcp_tools.core.media.importer import MediaImporter
from mcp_tools.core.media.paths import validate_roots
from mcp_tools.core.media.service import MediaService
from mcp_tools.core.principal import LOCAL, Principal
from mcp_tools.core.projects.service import ProjectService
from mcp_tools.core.secrets import load_secrets
from mcp_tools.core.workflows.loader import Workflow, load_workflows
from mcp_tools.db.store import Store
from mcp_tools.providers.base import Provider
from mcp_tools.providers.mock import MockProvider
from mcp_tools.providers.openai import OpenAIProvider
from mcp_tools.providers.registry import ProviderRegistry
from mcp_tools.providers.replicate import ReplicateProvider

logger = logging.getLogger(__name__)


def _egress(value: str) -> Egress:
    if value == "auto":
        return detect_egress()
    if value not in ("direct", "proxy", "off"):
        raise ValueError(f"egress must be auto, direct, proxy or off, got {value!r}")
    return value  # type: ignore[return-value]


class Platform:
    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
        providers: Sequence[Provider] | None = None,
        urls: UrlIssuer | None = None,
        expose_local_paths: bool = True,
        run_jobs: bool = True,
        fetcher: SafeFetcher | None = None,
    ) -> None:
        self.settings = settings
        self.clock: Clock = clock or SystemClock()
        self.principal: Principal = LOCAL
        self.urls: UrlIssuer = urls or NullIssuer()
        self.expose_local_paths = expose_local_paths
        self.run_jobs = run_jobs
        self.fetcher = fetcher or SafeFetcher(egress=_egress(settings.egress))
        self.providers = ProviderRegistry(providers if providers is not None else self._default_providers())
        self.catalog: Catalog = load_catalog(settings.catalog_paths, availability=self.providers.availability)
        self.workflows: dict[str, Workflow] = load_workflows(settings.workflow_paths)
        self.ledger = Ledger(self.clock, daily_cap=settings.daily_cap_credits)
        self.idempotency = IdempotencyStore(self.clock)
        self._refs = 0
        self._lock: anyio.Lock | None = None
        self.started = False
        # set in _start()
        self.data: DataDir
        self.store: Store
        self.blobs: LocalBlobStore
        self.media: MediaService
        self.importer: MediaImporter
        self.projects: ProjectService
        self.apps: AppRegistry
        self.signal: ChangeSignal
        self.engine: Engine
        self.runner: Runner
        self.jobs: JobService
        self._stop_event: anyio.Event
        self._service: asyncio.Task[None] | None = None

    def _default_providers(self) -> list[Provider]:
        delay = self.settings.mock_delay_ms
        delays = {"image": delay, "video": delay, "audio": delay} if delay is not None else None
        keys = load_secrets(self.settings.data_dir)
        return [
            MockProvider(self.clock, delays_ms=delays),
            ReplicateProvider(keys.get("REPLICATE_API_TOKEN"), self.fetcher),
            OpenAIProvider(
                keys.get("OPENAI_API_KEY"),
                organization=keys.get("OPENAI_ORG_ID"),
                project=keys.get("OPENAI_PROJECT_ID"),
            ),
        ]

    @asynccontextmanager
    async def running(self) -> AsyncIterator["Platform"]:
        while True:
            if self._lock is None:
                self._lock = anyio.Lock()
            lock = self._lock
            await lock.acquire()
            if self._lock is lock:
                break
            lock.release()  # the platform stopped and dropped this lock while we waited: start over
        try:
            self._refs += 1
            if self._refs == 1:
                try:
                    await self._start()
                except BaseException:
                    self._refs -= 1
                    with anyio.CancelScope(shield=True):
                        await self._stop()
                    self._lock = None
                    raise
        finally:
            lock.release()
        try:
            yield self
        finally:
            with anyio.CancelScope(shield=True):
                async with lock:
                    self._refs -= 1
                    if self._refs == 0:
                        await self._stop()
                        self._lock = None

    async def _start(self) -> None:
        self.data = await anyio.to_thread.run_sync(prepare_data_dir, self.settings.data_dir)
        self.store = Store(self.data.database)
        await self.store.open()
        self.blobs = LocalBlobStore(self.data.blobs)
        self.media = MediaService(self.store, self.blobs, self.clock)
        try:  # partial downloads left behind by a killed process
            await self.blobs.sweep_temp(older_than_seconds=3600, now=time.time())
        except OSError:
            logger.debug("could not sweep temporary blobs", exc_info=True)
        roots = validate_roots([self.data.inbox, *self.settings.import_root_paths]) if self.expose_local_paths else ()
        self.importer = MediaImporter(self.media, self.fetcher, roots=roots)

        async def bootstrap(conn: AsyncConnection) -> None:
            await self.ledger.ensure_account(
                conn, self.principal.workspace_id, initial_credits=self.settings.initial_credits
            )

        await self.store.write(bootstrap)

        self.projects = ProjectService(self.store, self.clock, self.idempotency)
        self.apps = AppRegistry([image_tools.APP, storyboard.APP], store=self.store, clock=self.clock)
        self.signal = ChangeSignal()
        self.engine = Engine(
            store=self.store,
            providers=self.providers,
            media=self.media,
            ledger=self.ledger,
            catalog=self.catalog,
            clock=self.clock,
            owner=make_owner_id(self.clock),
            on_change=lambda _job_id: self.signal.fire(),
            lease_ttl_ms=self.settings.lease_ttl_ms,
        )
        self.runner = Runner(
            self.engine,
            concurrency=self.settings.job_concurrency,
            heartbeat_seconds=self.settings.lease_ttl_ms / 3000,
        )
        self.jobs = JobService(
            store=self.store,
            catalog=self.catalog,
            providers=self.providers,
            media=self.media,
            projects=self.projects,
            ledger=self.ledger,
            idempotency=self.idempotency,
            runner=self.runner,
            signal=self.signal,
            clock=self.clock,
            urls=self.urls,
            expose_local_paths=self.expose_local_paths,
            max_credits_per_call=self.settings.max_credits_per_call,
            auto_approve_credits=self.settings.auto_approve_credits,
        )
        self._stop_event = anyio.Event()
        if self.run_jobs:
            ready = anyio.Event()
            self._service = asyncio.get_running_loop().create_task(self._serve(ready), name="mcp-tools-runner")
            await ready.wait()
        self.started = True

    async def _serve(self, ready: anyio.Event) -> None:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._supervise_runner)
                ready.set()
                await self._stop_event.wait()
                tg.cancel_scope.cancel()
        finally:
            ready.set()

    async def _supervise_runner(self) -> None:
        """Keep the runner alive: a bug in one tick must not stop job processing for good."""
        while True:
            try:
                await self.runner.run()
            except Exception:
                logger.exception("job runner crashed; restarting in 1s")
                await anyio.sleep(1)

    async def _stop(self) -> None:
        service, self._service = self._service, None
        if service is not None:
            self._stop_event.set()
            # asyncio.wait does not cancel the task on timeout, and it returns even while the
            # runner is inside a shielded provider call. Stopping must stay quick: a job cut
            # off here keeps its lease and is closed (and refunded) by the next process.
            done, _ = await asyncio.wait({service}, timeout=self.settings.shutdown_grace_seconds)
            if not done:
                service.cancel()
                done, _ = await asyncio.wait({service}, timeout=0.2)
            if done and not service.cancelled() and service.exception() is not None:
                logger.error("job runner stopped with an error", exc_info=service.exception())
        self.started = False
        if getattr(self, "store", None) is not None:
            await self.store.close()
        await self.providers.aclose()
