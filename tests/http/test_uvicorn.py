"""The real thing: `mcp-tools serve --transport http` in a subprocess, spoken to over a socket."""

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import payload

pytestmark = pytest.mark.anyio


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_serve_http_end_to_end(tmp_path: Path) -> None:
    port = _free_port()
    env = {**os.environ, "MCP_TOOLS_DATA_DIR": str(tmp_path / "data"), "MCP_TOOLS_MOCK_DELAY_MS": "20"}
    env.pop("MCP_TOOLS_API_KEY", None)
    log = (tmp_path / "server.log").open("wb")
    process = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-m", "mcp_tools", "serve", "--transport", "http", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx2.AsyncClient(trust_env=False, timeout=5.0) as probe:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if (await probe.get(f"{base}/healthz")).status_code == 200:
                        break
                except httpx2.TransportError:
                    pass
                assert process.poll() is None and time.monotonic() < deadline, (tmp_path / "server.log").read_text()
                time.sleep(0.1)  # noqa: ASYNC251
            assert (
                await probe.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            ).status_code == 401
            assert "server" not in (await probe.get(f"{base}/healthz")).headers

        text = (tmp_path / "server.log").read_text()
        match = re.search(r"new API key \(shown once, store it now\): (\S+)", text)
        assert match, text
        key = match.group(1)
        async with (
            httpx2.AsyncClient(trust_env=False, timeout=30.0, headers={"Authorization": f"Bearer {key}"}) as authed,
            Client(streamable_http_client(f"{base}/mcp", http_client=authed)) as c,
        ):
            done = payload(
                await c.call_tool(
                    "generate_image", {"params": {"model": "mock_image", "prompt": "over http"}, "wait_seconds": 10}
                )
            )
            url = done["jobs"][0]["results"][0]["url"]
            assert url.startswith(f"{base}/files/med_")
        async with httpx2.AsyncClient(trust_env=False, timeout=10.0) as anonymous:
            image = await anonymous.get(url)
            assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        logged = (tmp_path / "server.log").read_text()
        assert "/files/med_" not in logged and url.split("s=")[1] not in logged  # signed URLs stay out of logs
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        log.close()
    final = (tmp_path / "server.log").read_text()
    # uvicorn shuts down cleanly, then re-raises the signal it was stopped with
    assert process.returncode in (0, -15) and "Application shutdown complete" in final, final
