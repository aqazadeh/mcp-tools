from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.billing.idempotency import IdempotencyStore, check_key, request_hash
from mcp_tools.core.billing.ledger import Ledger, Ref
from mcp_tools.core.clock import FakeClock
from mcp_tools.core.errors import ApiError
from mcp_tools.core.pagination import PageRequest, decode_cursor
from mcp_tools.db.store import Store

pytestmark = pytest.mark.anyio
WS = "ws_local"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(tmp_path / "db.sqlite")
    await s.open()
    yield s
    await s.close()


async def _open_account(store: Store, ledger: Ledger, credits: int) -> None:
    async with store.write_tx() as conn:
        await ledger.ensure_account(conn, WS, initial_credits=credits)


async def _balance(store: Store, ledger: Ledger) -> tuple[int, int]:
    async with store.read_tx() as conn:
        b = await ledger.balance(conn, WS)
    return b.balance, b.spent_today


async def test_opening_grant_happens_once(store: Store, clock: FakeClock) -> None:
    ledger = Ledger(clock)
    await _open_account(store, ledger, 100)
    await _open_account(store, ledger, 100)
    assert await _balance(store, ledger) == (100, 0)


async def test_spend_and_refund_exactly_once(store: Store, clock: FakeClock) -> None:
    ledger = Ledger(clock)
    await _open_account(store, ledger, 100)
    ref = Ref("job", "job_1")
    async with store.write_tx() as conn:
        assert await ledger.spend(conn, WS, 30, ref) == 70
    assert await _balance(store, ledger) == (70, 30)
    async with store.write_tx() as conn:
        assert await ledger.refund(conn, WS, ref) == 30
        assert await ledger.refund(conn, WS, ref) == 0  # second refund is a no-op
    async with store.write_tx() as conn:
        assert await ledger.refund(conn, WS, Ref("job", "job_never_spent")) == 0
    assert await _balance(store, ledger) == (100, 0)


async def test_same_reference_cannot_be_spent_twice(store: Store, clock: FakeClock) -> None:
    ledger = Ledger(clock)
    await _open_account(store, ledger, 100)
    async with store.write_tx() as conn:
        await ledger.spend(conn, WS, 10, Ref("job", "job_1"))
    with pytest.raises(IntegrityError):
        async with store.write_tx() as conn:
            await ledger.spend(conn, WS, 10, Ref("job", "job_1"))
    assert await _balance(store, ledger) == (90, 10)  # the failed transaction rolled back completely


async def test_insufficient_credits_writes_nothing(store: Store, clock: FakeClock) -> None:
    ledger = Ledger(clock)
    await _open_account(store, ledger, 20)
    with pytest.raises(ApiError) as excinfo:
        async with store.write_tx() as conn:
            await ledger.spend(conn, WS, 21, Ref("job", "job_1"))
    assert excinfo.value.code == "insufficient_credits" and excinfo.value.recovery_tool == "balance"
    assert await _balance(store, ledger) == (20, 0)


async def test_daily_cap_is_enforced_and_resets_next_day(store: Store, clock: FakeClock) -> None:
    ledger = Ledger(clock, daily_cap=50)
    await _open_account(store, ledger, 1000)
    async with store.write_tx() as conn:
        await ledger.spend(conn, WS, 40, Ref("job", "job_1"))
    with pytest.raises(ApiError) as excinfo:
        async with store.write_tx() as conn:
            await ledger.spend(conn, WS, 11, Ref("job", "job_2"))
    assert excinfo.value.code == "budget_exceeded"
    assert await _balance(store, ledger) == (960, 40)
    # a refund gives the amount back to the cap of the day it was spent on
    async with store.write_tx() as conn:
        await ledger.refund(conn, WS, Ref("job", "job_1"))
        await ledger.spend(conn, WS, 50, Ref("job", "job_3"))
    clock.advance(24 * 3600)
    async with store.write_tx() as conn:
        await ledger.spend(conn, WS, 50, Ref("job", "job_4"))
    assert await _balance(store, ledger) == (900, 50)


async def test_concurrent_spends_never_overdraw(store: Store, clock: FakeClock, tmp_path: Path) -> None:
    """Two stores on one file race for the last credits; exactly the affordable number wins."""
    ledger = Ledger(clock)
    await _open_account(store, ledger, 10)
    other = Store(tmp_path / "db.sqlite")
    await other.open()
    outcomes: list[str] = []

    async def spender(s: Store, name: str) -> None:
        for i in range(10):
            try:
                async with s.write_tx() as conn:
                    await ledger.spend(conn, WS, 1, Ref("job", f"job_{name}_{i}"))
                outcomes.append("ok")
            except ApiError as exc:
                outcomes.append(exc.code)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(spender, store, "a")
            tg.start_soon(spender, other, "b")
    finally:
        await other.close()
    assert outcomes.count("ok") == 10
    assert outcomes.count("insufficient_credits") == 10
    assert await _balance(store, ledger) == (0, 10)


async def test_entries_are_newest_first_and_paginated(store: Store, clock: FakeClock) -> None:
    ledger = Ledger(clock)
    await _open_account(store, ledger, 100)
    async with store.write_tx() as conn:
        for i in range(3):
            await ledger.spend(conn, WS, 1, Ref("job", f"job_{i}"))
    async with store.read_tx() as conn:
        first = await ledger.entries(conn, WS, PageRequest(limit=2))
        second = await ledger.entries(conn, WS, PageRequest(limit=2, cursor=decode_cursor(first.next_cursor)))
        spends = await ledger.entries(conn, WS, PageRequest(limit=10), kinds=("spend",))
    assert [e.ref_id for e in first.items] == ["job_2", "job_1"] and first.has_more
    assert [e.kind for e in second.items] == ["spend", "grant"] and not second.has_more
    assert len(spends.items) == 3 and spends.items[0].balance_after == 97


async def test_idempotency_replays_same_request_and_rejects_a_different_one(
    store: Store, clock: FakeClock
) -> None:
    idem = IdempotencyStore(clock, window_ms=1000)
    digest = request_hash({"model": "m", "prompt": "p"})
    async with store.write_tx() as conn:
        assert await idem.begin(conn, WS, "generate_image", "k1", digest) is None
        await idem.finish(conn, WS, "generate_image", "k1", {"jobs": ["job_1"]})
    async with store.write_tx() as conn:
        assert await idem.begin(conn, WS, "generate_image", "k1", digest) == {"jobs": ["job_1"]}
        # the same key under another tool is a different key
        assert await idem.begin(conn, WS, "generate_video", "k1", digest) is None
    with pytest.raises(ApiError) as excinfo:
        async with store.write_tx() as conn:
            await idem.begin(conn, WS, "generate_image", "k1", request_hash({"model": "other"}))
    assert excinfo.value.code == "idempotency_key_reused"
    clock.advance(2)  # past the replay window: the key can be used again
    async with store.write_tx() as conn:
        assert await idem.begin(conn, WS, "generate_image", "k1", request_hash({"model": "other"})) is None


async def test_idempotency_claim_rolls_back_with_the_failed_work(store: Store, clock: FakeClock) -> None:
    idem = IdempotencyStore(clock)

    async def failing(conn: AsyncConnection) -> None:
        await idem.begin(conn, WS, "t", "k", "h")
        raise ApiError("insufficient_credits", "no")

    with pytest.raises(ApiError):
        await store.write(failing)
    async with store.write_tx() as conn:
        assert await idem.begin(conn, WS, "t", "k", "h") is None  # nothing was left behind


def test_request_hash_is_order_insensitive_and_keys_are_checked() -> None:
    assert request_hash({"a": 1, "b": [1, 2]}) == request_hash({"b": [1, 2], "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})
    assert check_key("abc-123") == "abc-123"
    for bad in ("", "has space", "x" * 129, "line\nbreak"):
        with pytest.raises(ApiError):
            check_key(bad)
