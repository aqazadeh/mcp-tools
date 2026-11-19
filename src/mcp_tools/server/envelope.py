"""The single place where tool results and tool errors are shaped.

Facts about mcp 2.3.0 this module is built around (pinned by tests/test_sdk_contract.py):
- A `ToolError` raised by a handler reaches the client as "Error executing tool NAME: <text>",
  so a JSON error body would not parse. Handlers therefore never raise for anticipated
  failures: `guard` converts `ApiError` into an `is_error` result.
- The SDK silently drops unknown top-level arguments, so a misspelled `max_credits` would
  simply not apply. `guard` rejects them.
- A handler may return `CallToolResult` directly; with `Annotated[CallToolResult, Model]`
  the SDK publishes Model's schema and validates `structured_content` against it.
"""

import functools
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, ContentBlock, TextContent
from pydantic import BaseModel

from mcp_tools.core.errors import INTERNAL_ERROR, RESULT_TOO_LARGE, UNKNOWN_ARGUMENT, ApiError
from mcp_tools.core.platform import Platform
from mcp_tools.server.registry import CONTEXT_PARAM, ToolDef

logger = logging.getLogger(__name__)

# The Python SDK client rejects a single SSE event above 1 MiB; stay well below it.
MAX_RESULT_BYTES = 512 * 1024


def _compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def ok(
    data: BaseModel, *, content: Sequence[ContentBlock] = (), meta: Mapping[str, Any] | None = None
) -> CallToolResult:
    """Build a success result: compact JSON text without nulls plus structured content.

    `meta` goes to `_meta`, which hosts pass to a widget but not to the model. It counts
    toward the size budget; if it would not fit, it is dropped rather than failing the call.
    """
    payload = data.model_dump(mode="json", exclude_none=True)
    text = _compact(payload)
    extra = sum(len(getattr(block, "data", "") or "") + len(getattr(block, "text", "") or "") for block in content)
    if len(text.encode()) + extra > MAX_RESULT_BYTES:
        return fail(
            ApiError(
                RESULT_TOO_LARGE,
                "the result exceeds the size budget for one tool response",
                hint="Request fewer items (lower limit) or fewer previews per call.",
            )
        )
    if meta and len(text.encode()) + extra + len(_compact(meta).encode()) > MAX_RESULT_BYTES:
        meta = None
    return CallToolResult(
        content=[TextContent(type="text", text=text), *content],
        structured_content=payload,
        meta=dict(meta) if meta else None,
    )


def fail(err: ApiError) -> CallToolResult:
    """Build an error result the calling model (or a widget) can parse and act on."""
    return CallToolResult(content=[TextContent(type="text", text=_compact({"error": err.to_dict()}))], is_error=True)


def platform_of(ctx: Context) -> Platform:
    platform = ctx.request_context.lifespan_context
    if not isinstance(platform, Platform):  # pragma: no cover - wiring error
        raise RuntimeError("server lifespan did not provide a Platform")
    return platform


def _raw_argument_names(ctx: Any) -> set[str] | None:
    try:
        params = ctx.request_context.params
    except Exception:
        return None
    if not isinstance(params, Mapping):
        return None
    arguments = params.get("arguments")
    return set(arguments) if isinstance(arguments, Mapping) else None


def guard(d: ToolDef) -> Callable[..., Awaitable[CallToolResult]]:
    """Wrap a handler: reject unknown arguments, map ApiError, hide unexpected errors."""
    handler = d.handler
    allowed = d.arg_names

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
        try:
            raw = _raw_argument_names(kwargs.get(CONTEXT_PARAM))
            unknown = sorted(raw - allowed) if raw is not None else []
            if unknown:
                raise ApiError(
                    UNKNOWN_ARGUMENT,
                    f"unknown argument(s) for {d.name}: {', '.join(unknown)}",
                    hint="Remove or rename them; nothing was done.",
                    allowed=sorted(allowed),
                )
            return await handler(*args, **kwargs)
        except ApiError as err:
            return fail(err)
        except MCPError:
            raise
        except Exception:
            error_id = uuid.uuid4().hex[:12]
            logger.exception("tool %s failed unexpectedly (error_id=%s)", d.name, error_id)
            return fail(
                ApiError(
                    INTERNAL_ERROR,
                    f"{d.name} failed unexpectedly",
                    hint="This is a server bug, not a problem with the request. Do not retry in a loop.",
                    details={"error_id": error_id},
                )
            )

    return wrapper
