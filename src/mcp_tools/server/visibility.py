"""Keep widget-only tools away from clients that have no widgets.

`_meta.ui.visibility` is enforced by the host, not by the SDK. A client that never
negotiated MCP Apps would list an app-only helper as an ordinary tool and its model could
call it. This middleware removes such tools from `tools/list` and refuses calls to them
unless the client advertised Apps support.
"""

import json
import re
from collections.abc import Iterable
from typing import Any

from mcp.server.apps import client_supports_apps
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult
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


_SDK_ERROR = re.compile(r"^Error executing tool (?P<tool>[a-z0-9_]+): (?P<rest>.*)$", re.DOTALL)


def _validation_message(text: str) -> str:
    """Turn pydantic's multi-line report into "field: problem; field: problem" without echoing input values."""
    problems: list[str] = []
    lines = text.splitlines()[1:]
    for position, line in enumerate(lines):
        if line and not line.startswith(" ") and position + 1 < len(lines) and lines[position + 1].startswith("  "):
            detail = lines[position + 1].strip().split(" [type=", 1)[0]
            problems.append(f"{line.strip()}: {detail}")
    return "; ".join(problems[:4]) or "the arguments do not match the tool's input schema"


class ValidationEnvelope:
    """Give argument-validation failures the same JSON error shape as every other failure.

    The SDK validates arguments before a handler runs and reports a mismatch as raw text
    ("Error executing tool X: 1 validation error for ..."), which a model cannot act on as
    reliably as `{"error": {code, message, hint}}`.
    """

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method != "tools/call":
            return result
        # By this point the result is in its wire form (a dict); a model instance is handled too.
        wire = result if isinstance(result, dict) else None
        if wire is None and isinstance(result, CallToolResult):
            wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not wire or not wire.get("isError"):
            return result
        content = wire.get("content") or []
        first = content[0] if content and isinstance(content[0], dict) else {}
        match = _SDK_ERROR.match(first.get("text") or "") if first.get("type") == "text" else None
        if match is None or "validation error" not in match.group("rest").split("\n", 1)[0]:
            return result
        body = {
            "error": {
                "code": "invalid_params",
                "message": _validation_message(match.group("rest")),
                "retryable": False,
                "hint": f"Fix the arguments to match the input schema of {match.group('tool')}; nothing was done.",
            }
        }
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        return {**wire, "content": [{"type": "text", "text": text}], "isError": True}
