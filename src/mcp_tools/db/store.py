"""SQLite access: two engines on one file, explicit transaction control, migrations.

Why it looks like this (verified against SQLAlchemy 2.1 + aiosqlite, see tests):
- `isolation_level=None` plus a `begin` listener is the only recipe that allows
  `BEGIN IMMEDIATE`. The option the SQLAlchemy docs list first (`autocommit=False`) keeps a
  transaction open at all times, which makes BEGIN IMMEDIATE fail and silently disables
  `PRAGMA foreign_keys`.
- Every write transaction starts with BEGIN IMMEDIATE: a deferred transaction that reads and
  then writes can fail at once with SQLITE_BUSY while another process holds the write lock.
- The read-write pool holds exactly one connection (`max_overflow=0`), so writers inside one
  process queue instead of competing through the busy handler. Several server processes
  sharing the file is the normal case on stdio; they serialise through the file lock.
- aiosqlite work cannot be cancelled: a cancelled COMMIT may still commit. `Store.write`
  therefore shields the whole unit of work, including the after-commit callback.
"""

import sqlite3
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import anyio
from sqlalchemy import Connection, event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from mcp_tools.core.errors import BUSY, ApiError
from mcp_tools.db import schema

T = TypeVar("T")

MIN_SQLITE = (3, 35, 0)  # RETURNING
BUSY_TIMEOUT_MS = 5000
POOL_TIMEOUT_S = 5.0

# MIGRATIONS[n] upgrades a database from schema version n + 1 to n + 2.
MIGRATIONS: list[Callable[[Connection], None]] = []


class StoreError(RuntimeError):
    """The database cannot be opened safely."""


def _busy(exc: BaseException) -> ApiError:
    return ApiError(
        BUSY,
        "the database is busy",
        hint="Another request is writing; retry in a moment.",
        retryable=True,
    )


def _is_busy(exc: OperationalError) -> bool:
    message = str(exc.orig or exc).lower()
    return "database is locked" in message or "database is busy" in message


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._rw: AsyncEngine | None = None
        self._ro: AsyncEngine | None = None

    # -- lifecycle -------------------------------------------------------------------------
    def _engine(self, *, writer: bool) -> AsyncEngine:
        connect_args: dict[str, Any] = {"isolation_level": None, "timeout": BUSY_TIMEOUT_MS / 1000}
        if sys.version_info >= (3, 12):
            connect_args["autocommit"] = sqlite3.LEGACY_TRANSACTION_CONTROL
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path}",
            connect_args=connect_args,
            pool_size=1 if writer else 4,
            max_overflow=0,
            pool_timeout=POOL_TIMEOUT_S,
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _on_connect(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            if writer:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA foreign_keys=ON")
            if not writer:
                cursor.execute("PRAGMA query_only=ON")
            cursor.close()

        @event.listens_for(engine.sync_engine, "begin")
        def _on_begin(connection: Connection) -> None:
            connection.exec_driver_sql("BEGIN IMMEDIATE" if writer else "BEGIN")

        return engine

    async def open(self) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise StoreError(f"SQLite {sqlite3.sqlite_version} is too old; 3.35 or newer is required")
        if self._rw is not None:
            return
        self._rw = self._engine(writer=True)
        try:
            async with self._rw.begin() as conn:
                await conn.run_sync(_bootstrap)
            self._ro = self._engine(writer=False)
            await self._self_test()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        for engine in (self._ro, self._rw):
            if engine is not None:
                await engine.dispose()
        self._ro = self._rw = None

    async def _self_test(self) -> None:
        async with self.write_tx() as conn:
            foreign_keys = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
            journal = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
        if foreign_keys != 1:
            raise StoreError("PRAGMA foreign_keys is not enabled on the write connection")
        if str(journal).lower() != "wal":
            raise StoreError(
                f"journal_mode is {journal!r}, expected WAL (is the data directory on a network share?)"
            )
        probe = "__self_test__"
        try:
            async with self.write_tx() as conn:
                await conn.execute(schema.meta.insert().values(key=probe, value="x"))
                raise _Rollback
        except _Rollback:
            pass
        async with self.read_tx() as conn:
            leaked = (await conn.execute(select(schema.meta.c.key).where(schema.meta.c.key == probe))).first()
        if leaked is not None:
            raise StoreError("a rolled-back write was persisted: transaction control is broken")

    # -- transactions ----------------------------------------------------------------------
    @asynccontextmanager
    async def read_tx(self) -> AsyncIterator[AsyncConnection]:
        """A short read-only transaction. Never hold one across a sleep or a provider call."""
        if self._ro is None:
            raise StoreError("store is not open")
        try:
            async with self._ro.connect() as conn:
                yield conn
        except PoolTimeoutError as exc:
            raise _busy(exc) from exc

    @asynccontextmanager
    async def write_tx(self) -> AsyncIterator[AsyncConnection]:
        """A BEGIN IMMEDIATE transaction; commits on exit, rolls back on error."""
        if self._rw is None:
            raise StoreError("store is not open")
        try:
            async with self._rw.begin() as conn:
                yield conn
        except PoolTimeoutError as exc:
            raise _busy(exc) from exc
        except OperationalError as exc:
            if _is_busy(exc):
                raise _busy(exc) from exc
            raise

    async def write(
        self,
        unit: Callable[[AsyncConnection], Awaitable[T]],
        *,
        after_commit: Callable[[T], None] | None = None,
    ) -> T:
        """Run one unit of work in a write transaction, shielded from cancellation.

        A request handler can be cancelled at any await. Without the shield a cancellation
        that lands during COMMIT would leave the caller not knowing whether rows were
        written, and the after-commit callback (waking the job runner) would be skipped.
        """
        with anyio.CancelScope(shield=True):
            async with self.write_tx() as conn:
                result = await unit(conn)
            if after_commit is not None:
                after_commit(result)
            return result
        raise AssertionError("unreachable")  # pragma: no cover - keeps the type checker honest


class _Rollback(Exception):
    pass


def _bootstrap(conn: Connection) -> None:
    """Create or migrate the schema inside one BEGIN IMMEDIATE transaction."""
    schema.meta.create(conn, checkfirst=True)
    row = conn.execute(select(schema.meta.c.value).where(schema.meta.c.key == "schema_version")).first()
    if row is None:
        schema.metadata.create_all(conn)
        conn.execute(schema.meta.insert().values(key="schema_version", value=str(schema.SCHEMA_VERSION)))
        return
    version = int(row[0])
    if version > schema.SCHEMA_VERSION:
        raise StoreError(
            f"database schema version {version} is newer than this server ({schema.SCHEMA_VERSION}); "
            "upgrade mcp-tools or point MCP_TOOLS_DATA_DIR at another directory"
        )
    for step in range(version, schema.SCHEMA_VERSION):
        MIGRATIONS[step - 1](conn)
    if version != schema.SCHEMA_VERSION:
        conn.execute(
            text("UPDATE meta SET value = :v WHERE key = 'schema_version'"), {"v": str(schema.SCHEMA_VERSION)}
        )
