"""Credit ledger (estimates; 1 credit = 0.001 USD).

Every method joins the caller's write transaction. Each rule is one atomic statement, so the
logic is also correct on a database without SQLite's global write lock:
- balance never goes negative: conditional UPDATE, enforced again by a CHECK constraint;
- the daily cap is a conditional UPDATE on a per-day counter row;
- a reference (a job, an app run) is spent once and refunded at most once: UNIQUE constraint.

The hard limits (daily cap, per-call ceiling) come from server configuration. Nothing a
tool caller passes can raise them.
"""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.clock import Clock, day_of
from mcp_tools.core.errors import BUDGET_EXCEEDED, INSUFFICIENT_CREDITS, ApiError
from mcp_tools.core.pagination import Page, PageRequest, encode_cursor
from mcp_tools.db.schema import accounts, daily_spend, ledger_entries
from mcp_tools.db.sql import insert_ignore


@dataclass(frozen=True)
class Ref:
    type: str
    id: str


@dataclass(frozen=True)
class Balance:
    balance: int
    spent_today: int
    daily_cap: int | None


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    kind: str
    amount: int
    balance_after: int
    ref_type: str | None
    ref_id: str | None
    note: str | None
    created_at: int


class Ledger:
    def __init__(self, clock: Clock, *, daily_cap: int | None = None) -> None:
        self._clock = clock
        self.daily_cap = daily_cap

    async def ensure_account(self, conn: AsyncConnection, workspace_id: str, *, initial_credits: int) -> None:
        """Create the account with its opening grant on first use; a no-op afterwards."""
        now = self._clock.now_ms()
        created = (
            await conn.execute(
                insert_ignore(conn, accounts, conflict=["workspace_id"])
                .values(workspace_id=workspace_id, balance=0, created_at=now)
                .returning(accounts.c.workspace_id)
            )
        ).first()
        if created is not None and initial_credits > 0:
            await self.grant(conn, workspace_id, initial_credits, note="opening balance")

    async def balance(self, conn: AsyncConnection, workspace_id: str) -> Balance:
        row = (
            await conn.execute(select(accounts.c.balance).where(accounts.c.workspace_id == workspace_id))
        ).first()
        day = day_of(self._clock.now_ms())
        spent = (
            await conn.execute(
                select(daily_spend.c.spent).where(
                    daily_spend.c.workspace_id == workspace_id, daily_spend.c.day == day
                )
            )
        ).first()
        return Balance(
            balance=row[0] if row else 0, spent_today=spent[0] if spent else 0, daily_cap=self.daily_cap
        )

    async def spend(
        self, conn: AsyncConnection, workspace_id: str, amount: int, ref: Ref, *, note: str | None = None
    ) -> int:
        """Debit `amount` for `ref`. Raises ApiError when the cap or the balance does not allow it."""
        if amount < 0:
            raise ValueError("amount must not be negative")
        now = self._clock.now_ms()
        day = day_of(now)
        await conn.execute(
            insert_ignore(conn, daily_spend, conflict=["workspace_id", "day"]).values(
                workspace_id=workspace_id, day=day, spent=0
            )
        )
        conditions = [daily_spend.c.workspace_id == workspace_id, daily_spend.c.day == day]
        if self.daily_cap is not None:
            conditions.append(daily_spend.c.spent + amount <= self.daily_cap)
        capped = await conn.execute(
            update(daily_spend).where(*conditions).values(spent=daily_spend.c.spent + amount)
        )
        if capped.rowcount != 1:
            raise ApiError(
                BUDGET_EXCEEDED,
                "the daily spending cap of this server would be exceeded",
                hint="The cap resets at 00:00 UTC. Only the operator can raise it; do not retry today.",
                recovery_tool="balance",
                details={"daily_cap": self.daily_cap, "requested": amount},
            )
        remaining = (
            await conn.execute(
                update(accounts)
                .where(accounts.c.workspace_id == workspace_id, accounts.c.balance >= amount)
                .values(balance=accounts.c.balance - amount)
                .returning(accounts.c.balance)
            )
        ).first()
        if remaining is None:
            raise ApiError(
                INSUFFICIENT_CREDITS,
                "not enough credits for this request",
                hint="Lower the cost (fewer or smaller outputs) or ask the operator to add credits.",
                recovery_tool="balance",
                details={"requested": amount},
            )
        await conn.execute(
            ledger_entries.insert().values(
                workspace_id=workspace_id,
                kind="spend",
                amount=amount,
                balance_after=remaining[0],
                ref_type=ref.type,
                ref_id=ref.id,
                day=day,
                note=note,
                created_at=now,
            )
        )
        return remaining[0]

    async def refund(
        self, conn: AsyncConnection, workspace_id: str, ref: Ref, *, note: str | None = None
    ) -> int:
        """Return what was spent for `ref`. Exactly once: a second call refunds nothing and returns 0."""
        spent = (
            await conn.execute(
                select(ledger_entries.c.amount, ledger_entries.c.day).where(
                    ledger_entries.c.ref_type == ref.type,
                    ledger_entries.c.ref_id == ref.id,
                    ledger_entries.c.kind == "spend",
                )
            )
        ).first()
        if spent is None:
            return 0
        amount, spend_day = spent
        now = self._clock.now_ms()
        inserted = (
            await conn.execute(
                insert_ignore(conn, ledger_entries, conflict=["ref_type", "ref_id", "kind"])
                .values(
                    workspace_id=workspace_id,
                    kind="refund",
                    amount=amount,
                    balance_after=0,
                    ref_type=ref.type,
                    ref_id=ref.id,
                    day=day_of(now),
                    note=note,
                    created_at=now,
                )
                .returning(ledger_entries.c.id)
            )
        ).first()
        if inserted is None:
            return 0
        balance = (
            await conn.execute(
                update(accounts)
                .where(accounts.c.workspace_id == workspace_id)
                .values(balance=accounts.c.balance + amount)
                .returning(accounts.c.balance)
            )
        ).first()
        await conn.execute(
            update(ledger_entries)
            .where(ledger_entries.c.id == inserted[0])
            .values(balance_after=balance[0] if balance else 0)
        )
        # give the amount back to the cap of the day it was spent on (never below zero)
        await conn.execute(
            update(daily_spend)
            .where(daily_spend.c.workspace_id == workspace_id, daily_spend.c.day == spend_day)
            .values(spent=daily_spend.c.spent - amount)
            .where(daily_spend.c.spent >= amount)
        )
        return amount

    async def grant(self, conn: AsyncConnection, workspace_id: str, amount: int, *, note: str) -> int:
        """Add credits. Operator-only: reachable from the CLI, never from a tool."""
        if amount <= 0:
            raise ValueError("grant amount must be positive")
        now = self._clock.now_ms()
        balance = (
            await conn.execute(
                update(accounts)
                .where(accounts.c.workspace_id == workspace_id)
                .values(balance=accounts.c.balance + amount)
                .returning(accounts.c.balance)
            )
        ).first()
        if balance is None:
            raise ValueError(f"no account for workspace {workspace_id!r}")
        await conn.execute(
            ledger_entries.insert().values(
                workspace_id=workspace_id,
                kind="grant",
                amount=amount,
                balance_after=balance[0],
                day=day_of(now),
                note=note,
                created_at=now,
            )
        )
        return balance[0]

    async def entries(
        self,
        conn: AsyncConnection,
        workspace_id: str,
        page: PageRequest,
        *,
        kinds: tuple[str, ...] = (),
    ) -> Page[LedgerEntry]:
        """Newest first, keyset-paginated on the entry id."""
        query = select(ledger_entries).where(ledger_entries.c.workspace_id == workspace_id)
        if kinds:
            query = query.where(ledger_entries.c.kind.in_(kinds))
        before = (page.cursor or {}).get("before")
        if isinstance(before, int):
            query = query.where(ledger_entries.c.id < before)
        rows = (
            (await conn.execute(query.order_by(ledger_entries.c.id.desc()).limit(page.limit + 1)))
            .mappings()
            .all()
        )
        items = [
            LedgerEntry(
                id=r["id"],
                kind=r["kind"],
                amount=r["amount"],
                balance_after=r["balance_after"],
                ref_type=r["ref_type"],
                ref_id=r["ref_id"],
                note=r["note"],
                created_at=r["created_at"],
            )
            for r in rows[: page.limit]
        ]
        has_more = len(rows) > page.limit
        return Page(
            items=items,
            has_more=has_more,
            next_cursor=encode_cursor({"before": items[-1].id}) if has_more and items else None,
        )
