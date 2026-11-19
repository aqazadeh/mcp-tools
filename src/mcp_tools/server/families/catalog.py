"""Catalog family: discover models and their constraints."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field

from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.core.pagination import page_request
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import Wire, choice, optional_choice, require_choice
from mcp_tools.server.registry import ToolDef

ACTIONS = ("list", "search", "get", "recommend")
OUTPUT_TYPES = ("image", "video", "audio")
INPUT_KINDS = ("text", "media")


class ModelsExploreResult(Wire):
    kind: Literal["models"] = "models"
    schema_version: int = 1
    action: str
    items: list[dict[str, Any]] | None = None
    model: dict[str, Any] | None = None
    has_more: bool | None = None
    next_cursor: str | None = None
    hint: str | None = None


async def models_explore(
    ctx: Context,
    action: Annotated[
        str,
        choice(
            "list: browse; search: every word of `query` must match; get: one model in full; "
            "recommend: best fits for a goal.",
            ACTIONS,
        ),
    ] = "list",
    type: Annotated[str, choice("Filter by output type; omit for all.", OUTPUT_TYPES)] = "",
    input: Annotated[
        str, choice("text: usable from a prompt alone; media: accepts reference media.", INPUT_KINDS)
    ] = "",
    query: Annotated[
        str,
        Field(description="Search words (search) or the goal in plain words (recommend).", max_length=500),
    ] = "",
    model_id: Annotated[str, Field(description="Model id; required for action 'get'.", max_length=64)] = "",
    limit: Annotated[int, Field(description="Page size, 1-50 (default 20; 5 for recommend).")] = 0,
    cursor: Annotated[str, Field(description="next_cursor from the previous page; keep the same filters.")] = "",
) -> Annotated[CallToolResult, ModelsExploreResult]:
    catalog = platform_of(ctx).catalog
    require_choice("action", action, ACTIONS)
    output_type: Any = optional_choice("type", type, OUTPUT_TYPES)
    input_kind = optional_choice("input", input, INPUT_KINDS)

    if action == "get":
        if not model_id:
            raise ApiError(
                INVALID_PARAMS,
                "model_id is required for action 'get'",
                hint="Find an id with action 'list', 'search' or 'recommend' first.",
            )
        model = catalog.get(model_id)
        return ok(ModelsExploreResult(action=action, model=catalog.public(model, detail=True)))

    if action == "recommend":
        if not query.strip():
            raise ApiError(
                INVALID_PARAMS,
                "query is required for action 'recommend'",
                hint="Describe the goal, e.g. 'vertical product video from one photo'.",
            )
        size = min(limit, 20) if limit > 0 else 5
        models = catalog.recommend(query, type=output_type, input=input_kind, limit=size)
        return ok(
            ModelsExploreResult(
                action=action,
                items=[catalog.public(m) for m in models],
                hint="Best match first. Call action 'get' with the chosen id before building params.",
            )
        )

    page = page_request(limit, cursor)
    if action == "search":
        result = catalog.search(query, page, type=output_type, input=input_kind)
    else:
        result = catalog.browse(page, type=output_type, input=input_kind)
    return ok(
        ModelsExploreResult(
            action=action,
            items=[catalog.public(m) for m in result.items],
            has_more=result.has_more,
            next_cursor=result.next_cursor,
        )
    )


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="models_explore",
            family="catalog",
            handler=models_explore,
            title="Explore models",
            description=(
                "Find generation models and read their constraints. Read-only; costs nothing. "
                "Use 'recommend' with the goal in `query`, or 'list' / 'search' to browse; then 'get' with "
                "`model_id` for that model's parameters, media roles, aspect ratios and price. Items marked "
                "available:false need provider credentials on the server. Next: pass the chosen id as "
                "params.model to a generate tool."
            ),
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
        )
    ]
