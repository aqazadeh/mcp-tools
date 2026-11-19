import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp_types import CallToolResult

from mcp_tools.config import Settings
from mcp_tools.server.build import Built, build_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_level="WARNING")


@pytest.fixture
def built(settings: Settings) -> Built:
    return build_server(settings)


@pytest.fixture
async def client(built: Built) -> AsyncIterator[Client]:
    async with Client(built.mcp, raise_exceptions=True) as c:
        yield c


def payload(result: CallToolResult) -> dict[str, Any]:
    """The JSON body of a tool result (success or error envelope)."""
    block = result.content[0]
    assert block.type == "text", block
    return json.loads(block.text)


def error_of(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error, payload(result)
    assert result.structured_content is None
    return payload(result)["error"]
