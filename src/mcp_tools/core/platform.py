"""The platform core: everything the MCP facade (or any other front end) talks to.

`Platform` is constructed eagerly and started inside the server lifespan. The lifespan is
entered once per transport run and once per in-process test client, so `running()` is
re-entrant: the first entry starts the platform, the last exit stops it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio

from mcp_tools.config import Settings
from mcp_tools.core.catalog.loader import Availability, Catalog, load_catalog


class Platform:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog: Catalog = load_catalog(settings.catalog_paths, availability=self.provider_availability)
        self._refs = 0
        self._lock: anyio.Lock | None = None
        self.started = False

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
                await self._start()
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
        self.started = True

    async def _stop(self) -> None:
        self.started = False
