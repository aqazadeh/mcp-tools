"""Keep widget-only tools away from clients that have no widgets.

`_meta.ui.visibility` is enforced by the host, not by the SDK. A client that never
negotiated MCP Apps would list an app-only helper as an ordinary tool and its model could
call it. This middleware removes such tools from `tools/list` and refuses calls to them
unless the client advertised Apps support.
"""

from collections.abc import Iterable
from typing import Any

from mcp.server.apps import client_supports_apps
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import METHOD_NOT_FOUND


class AppsVisibility:
    def __init__(self, app_only: Iterable[str]) -> None:
        self._app_only = frozenset(app_only)

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if not self._app_only or ctx.method not in ("tools/list", "tools/call"):
            return await call_next(ctx)
        try:
            supported = client_supports_apps(ctx)
        except Exception:
            supported = False
        if supported:
            return await call_next(ctx)
        if ctx.method == "tools/call":
            params = ctx.params if isinstance(ctx.params, dict) else {}
            if params.get("name") in self._app_only:
                raise MCPError(METHOD_NOT_FOUND, f"Unknown tool: {params.get('name')}")
            return await call_next(ctx)
        result = await call_next(ctx)
        if isinstance(result, dict):
            tools = result.get("tools")
            if isinstance(tools, list):
                kept = [t for t in tools if not (isinstance(t, dict) and t.get("name") in self._app_only)]
                return {**result, "tools": kept}
            return result
        tools = getattr(result, "tools", None)
        if result is not None and isinstance(tools, list):
            kept = [t for t in tools if getattr(t, "name", None) not in self._app_only]
            return result.model_copy(update={"tools": kept})
        return result
