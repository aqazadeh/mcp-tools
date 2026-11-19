import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from mcp_tools.server.build import Built
from mcp_tools.server.registry import MAX_DESCRIPTION_CHARS
from tests.conftest import error_of, payload

pytestmark = pytest.mark.anyio


async def test_tool_is_listed_with_schema_annotations_and_visibility(client: Client, built: Built) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    tool = tools["models_explore"]
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    assert tool.meta == {"ui": {"visibility": ["model"]}}
    assert tool.input_schema["properties"]["action"]["enum"] == ["list", "search", "get", "recommend"]
    assert tool.output_schema is not None
    assert len(tool.description or "") <= MAX_DESCRIPTION_CHARS
    assert "models_explore" in (client.instructions or "")
    assert {d.name for d in built.tools if d.visibility != ("app",)} == set(tools)  # widget helpers are hidden


async def test_list_search_get_recommend(client: Client) -> None:
    listed = payload(await client.call_tool("models_explore", {"action": "list", "type": "image"}))
    assert listed["kind"] == "models" and listed["has_more"] is False
    ids = [m["id"] for m in listed["items"]]
    assert ids[0] == "mock_image" and {"gpt_image_2", "flux_schnell"} <= set(ids)  # usable models come first
    assert "mock_upscale" not in ids and "recraft_crisp_upscale" not in ids  # transforms are not generation models
    assert [m["available"] for m in listed["items"]] == [True] + [False] * (len(ids) - 1)  # no provider keys here

    found = payload(await client.call_tool("models_explore", {"action": "search", "query": "upscale"}))
    assert found["items"][0]["id"] == "mock_upscale"

    detail = payload(await client.call_tool("models_explore", {"action": "get", "model_id": "mock_video"}))["model"]
    assert {p["name"] for p in detail["parameters"]} == {"duration", "resolution", "simulate"}
    assert detail["medias"][0]["role"] == "start_image"
    assert detail["pricing"] == {
        "unit": "per_second",
        "credits": 20,
        "multipliers": {"resolution": {"480p": 1, "720p": 2}},
    }

    recommended = payload(
        await client.call_tool("models_explore", {"action": "recommend", "query": "a short video from a photo"})
    )
    assert recommended["items"][0]["id"] == "mock_video"


async def test_pagination_cursor_round_trip(client: Client) -> None:
    first = payload(await client.call_tool("models_explore", {"limit": 2}))
    assert len(first["items"]) == 2 and first["has_more"] is True
    second = payload(await client.call_tool("models_explore", {"limit": 2, "cursor": first["next_cursor"]}))
    assert not {m["id"] for m in first["items"]} & {m["id"] for m in second["items"]}
    seen, page = len(first["items"]) + len(second["items"]), second
    while page.get("has_more"):
        page = payload(await client.call_tool("models_explore", {"limit": 2, "cursor": page["next_cursor"]}))
        seen += len(page["items"])
    assert "next_cursor" not in page and seen == len(
        payload(await client.call_tool("models_explore", {"limit": 50}))["items"]
    )


async def test_errors_use_the_json_contract(client: Client) -> None:
    missing = error_of(await client.call_tool("models_explore", {"action": "get"}))
    assert missing["code"] == "invalid_params"

    unknown = error_of(await client.call_tool("models_explore", {"action": "get", "model_id": "mock_vidoe"}))
    assert unknown["code"] == "unknown_model" and unknown["recovery_tool"] == "models_explore"
    assert "mock_video" in unknown["allowed"]

    bad_action = error_of(await client.call_tool("models_explore", {"action": "delete"}))
    assert bad_action["code"] == "invalid_params" and bad_action["allowed"] == [
        "list",
        "search",
        "get",
        "recommend",
    ]

    bad_cursor = error_of(await client.call_tool("models_explore", {"cursor": "%%%"}))
    assert bad_cursor["code"] == "invalid_params"

    typo = error_of(await client.call_tool("models_explore", {"modelid": "mock_image"}))
    assert typo["code"] == "unknown_argument"


@pytest.mark.slow
async def test_stdio_subprocess_serves_the_same_tool(tmp_path: Path) -> None:
    env = {**os.environ, "MCP_TOOLS_DATA_DIR": str(tmp_path / "data"), "MCP_TOOLS_LOG_LEVEL": "WARNING"}
    server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_tools", "serve"], env=env)
    async with Client(server) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        result = payload(await c.call_tool("models_explore", {"action": "get", "model_id": "mock_image"}))
    assert "models_explore" in names
    assert result["model"]["id"] == "mock_image"
