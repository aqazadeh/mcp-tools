"""Billing family: credits and limits."""

from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import BaseModel

from mcp_tools.core.catalog.pricing import CREDIT_USD
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.registry import ToolDef


class BalanceResult(BaseModel):
    kind: Literal["balance"] = "balance"
    schema_version: int = 1
    balance: int
    spent_today: int
    daily_cap: int | None = None
    max_credits_per_call: int
    auto_approve_credits: int
    credit_usd: float = CREDIT_USD
    note: str = "Credits are local estimates for budgeting; the provider's own bill is authoritative."


async def balance(ctx: Context) -> Annotated[CallToolResult, BalanceResult]:
    platform = platform_of(ctx)
    async with platform.store.read_tx() as conn:
        state = await platform.ledger.balance(conn, platform.principal.workspace_id)
    return ok(
        BalanceResult(
            balance=state.balance,
            spent_today=state.spent_today,
            daily_cap=state.daily_cap,
            max_credits_per_call=platform.settings.max_credits_per_call,
            auto_approve_credits=platform.settings.auto_approve_credits,
        )
    )


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="balance",
            family="billing",
            handler=balance,
            title="Credit balance",
            description=(
                "Show the credit balance and this server's spending limits. Read-only; costs nothing. Limits "
                "(daily cap, per-call ceiling, auto-approve amount) are set by the server operator and cannot be "
                "changed through tools; credits are added by the operator from the command line. Use it when a "
                "generate call reports insufficient_credits or budget_exceeded."
            ),
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        )
    ]
