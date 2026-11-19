"""Pins the behaviour of the MCP SDK that the server design depends on.

If one of these fails after a dependency upgrade, the design note next to the assertion
says what has to be revisited. Nothing here tests our own code except where stated.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.server.apps import APP_MIME_TYPE, Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel

pytestmark = pytest.mark.anyio


class Out(BaseModel):
    value: int
    note: str | None = None


class Params(BaseModel):
    n: int = 1


async def test_extensions_are_consumed_when_the_server_is_constructed() -> None:
    """build.py registers UI tools and HTML on Apps *before* constructing MCPServer."""
    apps = Apps()
    apps.add_html_resource("ui://t/a.html", "<html></html>")

    @apps.tool(resource_uri="ui://t/a.html", name="early")
    async def early() -> str:
        return "early"

    mcp = MCPServer("t", extensions=[apps])

    @apps.tool(resource_uri="ui://t/a.html", name="late")
    async def late() -> str:
        return "late"

    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        resources = (await c.list_resources()).resources
    assert "early" in names
    assert "late" not in names
    assert [r.mime_type for r in resources] == [APP_MIME_TYPE]


async def test_ui_tool_without_registered_resource_fails_at_construction() -> None:
    apps = Apps()

    @apps.tool(resource_uri="ui://t/missing.html", name="orphan")
    async def orphan() -> str:
        return ""

    with pytest.raises(ValueError, match="no such resource"):
        MCPServer("t", extensions=[apps])


async def test_duplicate_tool_names_are_silently_ignored() -> None:
    """registry.validate_defs raises on duplicates because the SDK does not."""
    mcp = MCPServer("t", warn_on_duplicate_tools=False)

    async def first() -> str:
        return "first"

    async def second() -> str:
        return "second"

    mcp.add_tool(first, name="dup")
    mcp.add_tool(second, name="dup")
    async with Client(mcp) as c:
        result = await c.call_tool("dup", {})
    assert result.content[0].text == "first"  # type: ignore[union-attr]


async def test_annotated_calltoolresult_publishes_schema_and_passes_result_through() -> None:
    mcp = MCPServer("t")

    async def tool() -> Annotated[CallToolResult, Out]:
        return CallToolResult(content=[TextContent(type="text", text='{"value":1}')], structured_content={"value": 1})

    mcp.add_tool(tool, name="tool", structured_output=True)
    async with Client(mcp, raise_exceptions=True) as c:
        (listed,) = (await c.list_tools()).tools
        result = await c.call_tool("tool", {})
    assert listed.output_schema is not None
    assert set(listed.output_schema["properties"]) == {"value", "note"}
    assert listed.output_schema.get("required") == ["value"]  # nullable-with-default is optional
    assert result.structured_content == {"value": 1}
    assert result.content[0].text == '{"value":1}'  # type: ignore[union-attr]


async def test_top_level_json_string_arguments_are_decoded() -> None:
    mcp = MCPServer("t")

    async def tool(params: Params, label: str = "") -> str:
        return f"{params.n}:{label}"

    mcp.add_tool(tool, name="tool")
    async with Client(mcp, raise_exceptions=True) as c:
        as_string = await c.call_tool("tool", {"params": '{"n": 7}', "label": '{"keep": "me"}'})
    # `params` is decoded; a parameter annotated exactly `str` is left alone.
    assert as_string.content[0].text == '7:{"keep": "me"}'  # type: ignore[union-attr]


async def test_toolerror_text_is_prefixed_so_json_bodies_do_not_parse() -> None:
    """Why envelope.fail() returns an is_error result instead of raising ToolError."""
    mcp = MCPServer("t")

    async def raises() -> str:
        raise ToolError('{"code":"x"}')

    async def crashes() -> str:
        raise ValueError("secret detail")

    mcp.add_tool(raises, name="raises")
    mcp.add_tool(crashes, name="crashes")
    async with Client(mcp) as c:
        anticipated = await c.call_tool("raises", {})
        unexpected = await c.call_tool("crashes", {})
    assert anticipated.is_error
    assert anticipated.content[0].text == 'Error executing tool raises: {"code":"x"}'  # type: ignore[union-attr]
    assert unexpected.is_error
    assert "secret detail" not in unexpected.content[0].text  # type: ignore[union-attr]


async def test_unknown_top_level_arguments_are_dropped_and_raw_arguments_are_visible() -> None:
    """Why envelope.guard() compares raw argument names with the declared ones."""
    mcp = MCPServer("t")
    seen: dict[str, Any] = {}

    async def tool(ctx: Context, a: int = 0, max_credits: int = -1) -> str:
        seen["raw"] = dict(ctx.request_context.params["arguments"])  # type: ignore[index]
        return f"{a}:{max_credits}"

    mcp.add_tool(tool, name="tool")
    async with Client(mcp, raise_exceptions=True) as c:
        result = await c.call_tool("tool", {"a": 1, "max_credit": 5})
    assert not result.is_error
    assert result.content[0].text == "1:-1"  # type: ignore[union-attr]  # the misspelled cap vanished
    assert seen["raw"] == {"a": 1, "max_credit": 5}


async def test_handlers_are_cancelled_when_the_client_gives_up() -> None:
    """Why provider calls and lease transitions never run inside a tool handler."""
    mcp = MCPServer("t")
    state = {"cancelled": False, "finished": False}
    cancelled = anyio.Event()

    async def slow() -> str:
        try:
            await anyio.sleep(5)
            state["finished"] = True
        except BaseException:
            state["cancelled"] = True
            cancelled.set()
            raise
        return "done"

    mcp.add_tool(slow, name="slow")
    async with Client(mcp) as c:
        with pytest.raises(Exception):  # noqa: B017, PT011 - the SDK's timeout error type is not part of the contract
            await c.call_tool("slow", {}, read_timeout_seconds=0.05)
        with anyio.move_on_after(2):
            await cancelled.wait()
    assert state == {"cancelled": True, "finished": False}


async def test_lifespan_is_entered_once_per_in_process_client() -> None:
    """Why Platform.running() is re-entrant and build_server() is a factory."""
    entries: list[str] = []

    @asynccontextmanager
    async def lifespan(_server: MCPServer[dict[str, int]]) -> AsyncIterator[dict[str, int]]:
        entries.append("start")
        yield {"n": len(entries)}
        entries.append("stop")

    mcp = MCPServer("t", lifespan=lifespan)

    async def tool(ctx: Context) -> int:
        return ctx.request_context.lifespan_context["n"]

    mcp.add_tool(tool, name="tool")
    for _ in range(2):
        async with Client(mcp, raise_exceptions=True) as c:
            await c.call_tool("tool", {})
    assert entries == ["start", "stop", "start", "stop"]


async def test_http_app_has_its_own_host_allowlist_and_needs_a_running_session_manager() -> None:
    """Why the Gate and TransportSecuritySettings are fed from one HostPolicy, and how HTTP tests run."""
    mcp = MCPServer("t")

    async def ping() -> str:
        return "pong"

    mcp.add_tool(ping, name="ping")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:8000"], allowed_origins=[]
    )
    app = mcp.streamable_http_app(transport_security=security)
    transport = httpx2.ASGITransport(app=app)
    timeout = httpx2.Timeout(30.0)
    async with mcp.session_manager.run():  # ASGITransport does not run the ASGI lifespan
        async with httpx2.AsyncClient(transport=transport, base_url="http://evil.test", timeout=timeout) as bad:
            rejected = await bad.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        async with (
            httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000", timeout=timeout) as good,
            Client(streamable_http_client("http://127.0.0.1:8000/mcp", http_client=good)) as c,
        ):
            result = await c.call_tool("ping", {})
    assert rejected.status_code == 421
    assert result.content[0].text == "pong"  # type: ignore[union-attr]
