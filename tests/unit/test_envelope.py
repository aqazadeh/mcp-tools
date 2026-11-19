import json
from typing import Annotated

import pytest
from mcp import Client
from mcp.server.mcpserver import Context
from mcp_types import CallToolResult
from pydantic import BaseModel

from mcp_tools.config import Settings
from mcp_tools.core.errors import ApiError, provider_text
from mcp_tools.server import envelope
from mcp_tools.server.build import build_server
from mcp_tools.server.envelope import fail, ok
from mcp_tools.server.registry import ToolDef
from tests.conftest import error_of, payload

pytestmark = pytest.mark.anyio


class Res(BaseModel):
    kind: str = "res"
    value: int = 0
    note: str | None = None
    blob: str | None = None


async def echo(ctx: Context, value: int = 0, max_credits: int = -1) -> Annotated[CallToolResult, Res]:
    return ok(Res(value=value))


async def refuses(ctx: Context) -> Annotated[CallToolResult, Res]:
    raise ApiError(
        "budget_exceeded", "too expensive", hint="lower count", recovery_tool="echo", allowed=[1, 2]
    )


async def crashes(ctx: Context) -> Annotated[CallToolResult, Res]:
    raise RuntimeError("database password is hunter2")


async def huge(ctx: Context) -> Annotated[CallToolResult, Res]:
    return ok(Res(blob="x" * (envelope.MAX_RESULT_BYTES + 1)))


EXTRA = [
    ToolDef(name="echo", handler=echo, description="Echo.", family="test"),
    ToolDef(name="refuses", handler=refuses, description="Refuses.", family="test"),
    ToolDef(name="crashes", handler=crashes, description="Crashes.", family="test"),
    ToolDef(name="huge", handler=huge, description="Too big.", family="test"),
]


def test_ok_strips_nulls_from_text_and_structured_content() -> None:
    result = ok(Res(value=3))
    assert not result.is_error
    assert json.loads(result.content[0].text) == {"kind": "res", "value": 3}  # type: ignore[union-attr]
    assert result.structured_content == {"kind": "res", "value": 3}
    assert " " not in result.content[0].text  # type: ignore[union-attr]  # compact separators


def test_fail_builds_a_parseable_error_without_structured_content() -> None:
    result = fail(ApiError("not_found", "no such job", hint="check the id", retryable=False))
    assert result.is_error and result.structured_content is None
    assert json.loads(result.content[0].text) == {  # type: ignore[union-attr]
        "error": {"code": "not_found", "message": "no such job", "retryable": False, "hint": "check the id"}
    }


def test_provider_text_is_truncated_and_cleaned() -> None:
    assert provider_text(None) is None
    cleaned = provider_text("bad\x00thing" + "y" * 1000)
    assert cleaned is not None and "\x00" not in cleaned and len(cleaned) == 500


async def test_guard_maps_errors_and_rejects_unknown_arguments(settings: Settings) -> None:
    built = build_server(settings, extra_tools=EXTRA)
    async with Client(built.mcp, raise_exceptions=True) as c:
        assert payload(await c.call_tool("echo", {"value": 4})) == {"kind": "res", "value": 4}

        refused = error_of(await c.call_tool("refuses", {}))
        assert refused == {
            "code": "budget_exceeded",
            "message": "too expensive",
            "retryable": False,
            "hint": "lower count",
            "recovery_tool": "echo",
            "allowed": [1, 2],
        }

        # a misspelled cap must fail loudly instead of silently not applying
        unknown = error_of(await c.call_tool("echo", {"value": 1, "max_credit": 5}))
        assert unknown["code"] == "unknown_argument"
        assert unknown["allowed"] == ["max_credits", "value"]

        crashed = error_of(await c.call_tool("crashes", {}))
        assert crashed["code"] == "internal_error"
        assert "hunter2" not in json.dumps(crashed)
        assert len(crashed["details"]["error_id"]) == 12

        too_big = error_of(await c.call_tool("huge", {}))
        assert too_big["code"] == "result_too_large"


async def test_every_success_result_validates_against_its_output_schema(settings: Settings) -> None:
    import jsonschema

    built = build_server(settings, extra_tools=EXTRA)
    async with Client(built.mcp, raise_exceptions=True) as c:
        schemas = {t.name: t.output_schema for t in (await c.list_tools()).tools}
        result = await c.call_tool("echo", {"value": 2})
    assert schemas["echo"] is not None
    jsonschema.validate(result.structured_content, schemas["echo"])
