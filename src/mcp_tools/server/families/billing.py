"""Billing family: credits and limits."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field

from mcp_tools.core.catalog.pricing import CREDIT_USD
from mcp_tools.core.pagination import page_request
from mcp_tools.core.preferences import KEYS as PREFERENCE_KEYS
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import Wire, choice, optional_choice
from mcp_tools.server.registry import ToolDef


class BalanceResult(Wire):
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


ENTRY_KINDS = ("spend", "refund", "grant", "adjust")


class TransactionsResult(Wire):
    kind: Literal["transactions"] = "transactions"
    schema_version: int = 1
    items: list[dict[str, Any]]
    has_more: bool = False
    next_cursor: str | None = None


async def transactions(
    ctx: Context,
    kind: Annotated[str, choice("Only entries of this kind; omit for all.", ENTRY_KINDS)] = "",
    limit: Annotated[int, Field(description="Page size, 1-50 (default 20).")] = 0,
    cursor: Annotated[str, Field(description="next_cursor from the previous page; keep the same filter.")] = "",
) -> Annotated[CallToolResult, TransactionsResult]:
    platform = platform_of(ctx)
    wanted = optional_choice("kind", kind, ENTRY_KINDS)
    async with platform.store.read_tx() as conn:
        page = await platform.ledger.entries(
            conn, platform.principal.workspace_id, page_request(limit, cursor), kinds=(wanted,) if wanted else ()
        )
    items = []
    for entry in page.items:
        item: dict[str, Any] = {
            "kind": entry.kind,
            "amount": entry.amount,
            "balance_after": entry.balance_after,
            "created_at_ms": entry.created_at,
        }
        if entry.ref_type == "job" and entry.ref_id:
            item["job_id"] = entry.ref_id
        if entry.note:
            item["note"] = entry.note
        items.append(item)
    return ok(TransactionsResult(items=items, has_more=page.has_more, next_cursor=page.next_cursor))


class PreferencesResult(Wire):
    kind: Literal["preferences"] = "preferences"
    schema_version: int = 1
    preferences: dict[str, Any]
    available_keys: list[str]
    message: str | None = None


async def preferences_get(ctx: Context) -> Annotated[CallToolResult, PreferencesResult]:
    platform = platform_of(ctx)
    values = await platform.preferences.get(principal=platform.principal)
    return ok(
        PreferencesResult(
            preferences=values,
            available_keys=list(PREFERENCE_KEYS),
            message="Apply these as defaults when the user has not said otherwise." if values else None,
        )
    )


async def preferences_update(
    ctx: Context,
    patch: Annotated[
        dict[str, Any],
        Field(
            description="Keys to set; null clears a key. Keys: default_image_model, default_video_model, "
            "default_audio_model, default_aspect_ratio, default_folder_id, style_notes."
        ),
    ],
) -> Annotated[CallToolResult, PreferencesResult]:
    platform = platform_of(ctx)
    values = await platform.preferences.update(patch, principal=platform.principal)
    return ok(PreferencesResult(preferences=values, available_keys=list(PREFERENCE_KEYS)))


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
        ),
        ToolDef(
            name="transactions",
            family="billing",
            handler=transactions,
            title="Credit transactions",
            description=(
                "List credit ledger entries, newest first: spend (a job was created), refund (a job failed, was "
                "moderated or canceled), grant and adjust (operator actions). Read-only. Job entries carry the "
                "job_id. Use it to explain where credits went or to confirm a refund."
            ),
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        ),
        ToolDef(
            name="preferences_get",
            family="billing",
            handler=preferences_get,
            title="Get preferences",
            description=(
                "Read the user's saved defaults: preferred image / video / audio model, aspect ratio, "
                "destination folder and style notes. Read-only. Call it once before creative work and use "
                "the values wherever the user has not said otherwise."
            ),
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        ),
        ToolDef(
            name="preferences_update",
            family="billing",
            handler=preferences_update,
            title="Update preferences",
            description=(
                "Save or clear the user's defaults. Use it only when the user states a lasting preference "
                '("always use 9:16", "put everything in this project"), not for a one-off choice. Only the '
                "listed keys exist; spending limits are server settings and cannot be changed here."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
            ),
            effects=frozenset({"write"}),
        ),
    ]
