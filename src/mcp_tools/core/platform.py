"""The platform core: everything the MCP facade (or any other front end) talks to.

`Platform` is constructed eagerly and started inside the server lifespan. The lifespan is
entered once per transport run and once per in-process test client, so `running()` is
re-entrant: the first entry starts the platform, the last exit stops it. Stopping must be
quick (stdio hosts kill the process about two seconds after closing stdin) and idempotent.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.blobs.local import LocalBlobStore
from mcp_tools.config import Settings
from mcp_tools.core.billing.idempotency import IdempotencyStore
from mcp_tools.core.billing.ledger import Ledger
from mcp_tools.core.catalog.loader import Availability, Catalog, load_catalog
from mcp_tools.core.clock import Clock, SystemClock
from mcp_tools.core.datadir import DataDir, prepare_data_dir
from mcp_tools.core.media.service import MediaService
from mcp_tools.core.principal import LOCAL, Principal
from mcp_tools.db.store import Store


class Platform:
    def __init__(self, settings: Settings, *, clock: Clock | None = None) -> None:
        self.settings = settings
        self.clock: Clock = clock or SystemClock()
        self.principal: Principal = LOCAL
        self.catalog: Catalog = load_catalog(settings.catalog_paths, availability=self.provider_availability)
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

    def provider_availability(self, provider: str) -> Availability:
        if provider == "mock":
            return Availability(True)
        return Availability(False, f"provider {provider!r} has no credentials in this server process")

    @asynccontextmanager
    async def running(self) -> AsyncIterator["Platform"]:
        if self._lock is None:
            self._lock = anyio.Lock()
        lock = self._lock
        async with lock:
            self._refs += 1
            if self._refs == 1:
                try:
                    await self._start()
                except BaseException:
                    self._refs -= 1
                    raise
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

        async def bootstrap(conn: AsyncConnection) -> None:
            await self.ledger.ensure_account(
                conn, self.principal.workspace_id, initial_credits=self.settings.initial_credits
            )

        await self.store.write(bootstrap)
        self.started = True

    async def _stop(self) -> None:
        if not self.started:
            return
        self.started = False
        await self.store.close()
