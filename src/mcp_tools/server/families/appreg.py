"""App registry family: three meta-tools in front of any number of apps."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field

from mcp_tools.core.appreg.spec import AppContext
from mcp_tools.core.billing.idempotency import check_key
from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.families.generate import IDEMPOTENCY, MAX_CREDITS
from mcp_tools.server.params import NextStep, Wire
from mcp_tools.server.registry import ToolDef


class AppsSearchResult(Wire):
    kind: Literal["apps"] = "apps"
    schema_version: int = 1
    items: list[dict[str, Any]]
    message: str | None = None


class AppDescribeResult(Wire):
    kind: Literal["app"] = "app"
    schema_version: int = 1
    app: dict[str, Any]
    manifest_revision: str
    next: NextStep | None = None


class AppInvokeResult(Wire):
    kind: Literal["app_run"] = "app_run"
    schema_version: int = 1
    status: str = Field(description="completed, running (poll the app's status action) or approval_required.")
    app_id: str
    action: str
    run_id: str | None = None
    output: dict[str, Any]
    credits: int | None = None
    replayed: bool | None = None
    message: str | None = None


async def apps_search(
    ctx: Context,
    query: Annotated[
        str, Field(description="What you want to do, in a few words; empty lists every app.", max_length=200)
    ] = "",
    limit: Annotated[int, Field(description="How many apps to return, 1-20 (default 10).")] = 0,
) -> Annotated[CallToolResult, AppsSearchResult]:
    items = platform_of(ctx).apps.search(query, min(limit, 20) if limit > 0 else 10)
    return ok(
        AppsSearchResult(
            items=items,
            message="Call apps_describe with an app_id to see its actions."
            if items
            else "No app matches; this server may simply not have one for that.",
        )
    )


async def apps_describe(
    ctx: Context,
    app_id: Annotated[str, Field(description="An app id from apps_search.", max_length=64)],
    action: Annotated[
        str,
        Field(description="An action name: returns its full input schema. Omit to list the actions.", max_length=64),
    ] = "",
) -> Annotated[CallToolResult, AppDescribeResult]:
    manifest = platform_of(ctx).apps.describe(app_id, action or None)
    revision = manifest.pop("manifest_revision")
    next_step = (
        NextStep(tool="apps_invoke", hint="Pass manifest_revision unchanged and arguments that match input_schema.")
        if action
        else NextStep(tool="apps_describe", hint="Call again with `action` to get that action's input schema.")
    )
    return ok(AppDescribeResult(app=manifest, manifest_revision=revision, next=next_step))


async def apps_invoke(
    ctx: Context,
    app_id: Annotated[str, Field(description="An app id from apps_search.", max_length=64)],
    action: Annotated[str, Field(description="An action of that app.", max_length=64)],
    manifest_revision: Annotated[
        str, Field(description="The manifest_revision apps_describe returned.", max_length=64)
    ],
    arguments: Annotated[dict[str, Any], Field(description="Arguments matching the action's input_schema.")],
    max_credits: Annotated[int | None, MAX_CREDITS] = None,
    idempotency_key: Annotated[str, IDEMPOTENCY] = "",
    confirm_destructive: Annotated[
        bool,
        Field(
            description="Must be true for actions marked destructive. Set it only after the user agreed.",
        ),
    ] = False,
) -> Annotated[CallToolResult, AppInvokeResult]:
    platform = platform_of(ctx)
    if max_credits is not None and max_credits < 0:
        raise ApiError(INVALID_PARAMS, "max_credits must not be negative", hint="Omit it or pass a cap in credits.")
    context = AppContext(
        principal=platform.principal,
        clock=platform.clock,
        media=platform.media,
        jobs=platform.jobs,
        max_credits=max_credits,
        idempotency_key=check_key(idempotency_key) if idempotency_key else None,
        load_run=lambda run_id: platform.apps.run(run_id, principal=platform.principal),
    )
    run_id, result = await platform.apps.invoke(
        app_id,
        action,
        manifest_revision=manifest_revision,
        arguments=arguments,
        context=context,
        confirm_destructive=confirm_destructive,
    )
    message = None
    if result.status == "approval_required":
        message = (
            f"Nothing was created: the estimate is {result.output.get('estimated')} credits, above the limit "
            f"({result.output.get('limit')}). Tell the user; if they agree, call again with a sufficient max_credits."
        )
    return ok(
        AppInvokeResult(
            status=result.status,
            app_id=app_id,
            action=action,
            run_id=run_id or None,
            output=result.output,
            credits=result.credits or None,
            replayed=True if result.replayed else None,
            message=message,
        )
    )


_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="apps_search",
            family="appreg",
            handler=apps_search,
            title="Search apps",
            description=(
                "Find extra capabilities that are not separate tools: small apps such as local image "
                "operations (resize, crop, convert, contact sheet) or a storyboard run. Read-only. Use it when "
                "no listed tool does what is needed. Next: apps_describe."
            ),
            annotations=_READ,
        ),
        ToolDef(
            name="apps_describe",
            family="appreg",
            handler=apps_describe,
            title="Describe app",
            description=(
                "Read an app's manifest: its actions, whether each is read-only, destructive or spends credits, "
                "and with `action` the JSON Schema of that action's arguments. Read-only. Returns the "
                "manifest_revision that apps_invoke requires. Reading a manifest does not authorise running it."
            ),
            annotations=_READ,
        ),
        ToolDef(
            name="apps_invoke",
            family="appreg",
            handler=apps_invoke,
            title="Invoke app action",
            description=(
                "Run one action of an app with arguments that match its schema from apps_describe. Pass that "
                "call's manifest_revision; if the app changed since, the call is refused and nothing runs. "
                "Actions that spend credits obey max_credits and idempotency_key like the generate tools "
                "(approval_required means nothing was created); free actions ignore both. Destructive actions "
                "need confirm_destructive: true after the user agreed. A result with status running is followed "
                "with the app's own status action."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
            ),
            effects=frozenset({"spend", "write", "network"}),
        ),
    ]
