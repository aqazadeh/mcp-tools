import time
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.errors import ApiError
from mcp_tools.db import schema
from mcp_tools.db.store import Store, StoreError

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(tmp_path / "db.sqlite")
    await s.open()
    yield s
    await s.close()


async def _meta(store: Store, key: str) -> str | None:
    async with store.read_tx() as conn:
        row = (await conn.execute(select(schema.meta.c.value).where(schema.meta.c.key == key))).first()
    return row[0] if row else None


async def test_open_creates_schema_and_stamps_version(store: Store) -> None:
    assert await _meta(store, "schema_version") == str(schema.SCHEMA_VERSION)
    async with store.read_tx() as conn:
        tables = {
            r[0]
            for r in (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).all()
        }
    assert set(schema.metadata.tables) <= tables


async def test_pragmas_are_applied(store: Store) -> None:
    async with store.write_tx() as conn:
        assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
        assert (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar() == "wal"
        assert (await conn.exec_driver_sql("PRAGMA synchronous")).scalar() == 2  # FULL
    async with store.read_tx() as conn:
        assert (await conn.exec_driver_sql("PRAGMA query_only")).scalar() == 1


async def test_reopening_keeps_data_and_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    first = Store(path)
    await first.open()
    async with first.write_tx() as conn:
        await conn.execute(schema.meta.insert().values(key="note", value="kept"))
    await first.close()

    second = Store(path)
    await second.open()
    assert await _meta(second, "note") == "kept"
    async with second.write_tx() as conn:
        await conn.execute(text("UPDATE meta SET value = '999' WHERE key = 'schema_version'"))
    await second.close()

    with pytest.raises(StoreError, match="newer than this server"):
        await Store(path).open()


async def test_write_tx_commits_and_rolls_back(store: Store) -> None:
    async with store.write_tx() as conn:
        await conn.execute(schema.meta.insert().values(key="a", value="1"))
    with pytest.raises(RuntimeError, match="boom"):
        async with store.write_tx() as conn:
            await conn.execute(schema.meta.insert().values(key="b", value="2"))
            raise RuntimeError("boom")
    assert await _meta(store, "a") == "1"
    assert await _meta(store, "b") is None


async def test_foreign_keys_are_enforced(store: Store) -> None:
    with pytest.raises(IntegrityError):
        async with store.write_tx() as conn:
            await conn.execute(
                schema.folders.insert().values(
                    id="fld_x", project_id="prj_missing", name="n", depth=0, created_at=1
                )
            )


async def test_read_connections_cannot_write(store: Store) -> None:
    with pytest.raises(OperationalError, match="readonly"):
        async with store.read_tx() as conn:
            await conn.execute(schema.meta.insert().values(key="x", value="y"))


async def test_second_writer_waits_for_the_first(store: Store, tmp_path: Path) -> None:
    """Two Store objects on one file stand in for two server processes."""
    other = Store(tmp_path / "db.sqlite")
    await other.open()
    waited: list[float] = []

    async def holder() -> None:
        async with store.write_tx() as conn:
            await conn.execute(schema.meta.insert().values(key="h", value="1"))
            await anyio.sleep(0.4)

    async def waiter() -> None:
        await anyio.sleep(0.05)
        started = time.monotonic()
        async with other.write_tx() as conn:
            await conn.execute(schema.meta.insert().values(key="w", value="1"))
        waited.append(time.monotonic() - started)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(holder)
            tg.start_soon(waiter)
    finally:
        await other.close()
    assert waited and waited[0] > 0.2
    assert await _meta(store, "h") == "1" and await _meta(store, "w") == "1"


async def test_write_is_shielded_and_runs_after_commit_callback(store: Store) -> None:
    """A cancelled caller must not leave a half-done unit of work or a lost wake-up."""
    committed: list[str] = []

    async def unit(conn: AsyncConnection) -> str:
        await conn.execute(schema.meta.insert().values(key="s", value="1"))
        await anyio.sleep(0.2)
        return "result"

    async def caller() -> None:
        await store.write(unit, after_commit=committed.append)

    async with anyio.create_task_group() as tg:
        tg.start_soon(caller)
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()
    assert committed == ["result"]
    assert await _meta(store, "s") == "1"


async def test_failed_unit_does_not_call_after_commit(store: Store) -> None:
    called: list[object] = []

    async def unit(conn: AsyncConnection) -> None:
        raise ApiError("invalid_params", "nope")

    with pytest.raises(ApiError):
        await store.write(unit, after_commit=called.append)
    assert called == []


async def test_closed_store_is_reported(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    with pytest.raises(StoreError, match="not open"):
        async with store.write_tx():
            pass
