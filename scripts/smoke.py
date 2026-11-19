"""End-to-end smoke test against a real server process, using only the keyless mock provider.

    uv run python scripts/smoke.py --transport stdio
    uv run python scripts/smoke.py --transport http

Starts `mcp-tools serve` as a subprocess on a throwaway data directory, walks through the main
flows the way a client would, and prints one line per step. Exits non-zero on the first failure.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000008000000080802000000"
    "4b6d29dc0000001249444154789c63fccfc0f09f010f60c2273950d20028de0110a5fb44d70000000049454e44ae426082"
)
STEPS: list[tuple[str, str]] = []


def step(name: str, detail: str = "") -> None:
    STEPS.append((name, detail))
    print(f"  PASS  {name:<34} {detail}")


def body(result: Any) -> dict[str, Any]:
    data = json.loads(result.content[0].text)
    if result.is_error:
        raise AssertionError(f"tool error: {data}")
    return data


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    return body(await client.call_tool(tool, arguments))


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def common_flow(client: Client) -> dict[str, Any]:
    tools = {t.name for t in (await client.list_tools()).tools}
    step("tools/list", f"{len(tools)} tools")

    recommended = await call(client, "models_explore", action="recommend", query="quick test image", type="image")
    model = recommended["items"][0]["id"]
    detail = await call(client, "models_explore", action="get", model_id=model)
    step("models_explore", f"chose {model} ({len(detail['model']['parameters'])} parameters)")

    params = {"model": model, "prompt": "a lighthouse at dusk", "aspect_ratio": "16:9", "count": 2}
    estimate = await call(client, "estimate_cost", requests=[{"params": params}])
    step("estimate_cost", f"{estimate['total_credits']} credits")

    project = (await call(client, "projects_create", name="Smoke test"))["project"]
    created = await call(
        client, "generate_image", params={**params, "folder_id": project["root_folder_id"]}, idempotency_key="smoke-1"
    )
    expect(created["status"] == "submitted" and len(created["jobs"]) == 2, f"unexpected: {created}")
    job_ids = [job["job_id"] for job in created["jobs"]]
    replay = await call(
        client, "generate_image", params={**params, "folder_id": project["root_folder_id"]}, idempotency_key="smoke-1"
    )
    expect(replay.get("replayed") is True and [j["job_id"] for j in replay["jobs"]] == job_ids, "replay made new jobs")
    step("generate_image + replay", f"{len(job_ids)} jobs, charged {created['credits']['charged']}")

    waited: dict[str, Any] = {}
    for _ in range(10):
        waited = await call(client, "jobs_wait", job_ids=job_ids, timeout_seconds=10)
        if waited["all_terminal"]:
            break
    expect(all(job["status"] == "completed" for job in waited["jobs"]), f"jobs did not complete: {waited}")
    media = waited["jobs"][0]["results"][0]
    step("jobs_wait", f"{media['mime']} {media['width']}x{media['height']}")

    looked = await client.call_tool("media_get", {"media_id": media["media_id"], "include_preview": True})
    expect(len(looked.content) == 2 and looked.content[1].type == "image", "no preview image attached")
    step("media_get preview", f"{len(looked.content[1].data)} base64 chars")

    refused = await client.call_tool("generate_image", {"params": params, "max_credit": 1})
    expect(refused.is_error and "unknown_argument" in refused.content[0].text, "a misspelled cap was accepted")
    step("misspelled cap is refused")

    assets = await call(client, "assets_list", folder_id=project["root_folder_id"])
    expect(len(assets["items"]) == 2, "jobs were not placed in the folder")
    step("project folder", f"{len(assets['items'])} assets")

    catalog = await call(client, "workflow_instructions")
    workflow = await call(client, "workflow_instructions", workflow=catalog["workflows"][0]["name"])
    step("workflow_instructions", f"{workflow['name']} ({len(workflow['instructions'])} chars)")

    app = await call(client, "apps_describe", app_id="image_tools", action="crop")
    cropped = await call(
        client,
        "apps_invoke",
        app_id="image_tools",
        action="crop",
        manifest_revision=app["manifest_revision"],
        arguments={"image_id": media["media_id"], "aspect_ratio": "1:1"},
    )
    square = cropped["output"]["media"]
    expect(square["width"] == square["height"], "crop is not square")
    step("apps_invoke image_tools.crop", f"{square['width']}x{square['height']}")

    balance = await call(client, "balance")
    step("balance", f"{balance['balance']} credits left")
    return media


@asynccontextmanager
async def stdio_session(data_dir: Path) -> AsyncIterator[Client]:
    env = {
        **os.environ,
        "MCP_TOOLS_DATA_DIR": str(data_dir),
        "MCP_TOOLS_MOCK_DELAY_MS": "150",
        "MCP_TOOLS_LOG_LEVEL": "WARNING",
    }
    server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_tools", "serve"], env=env)
    async with Client(stdio_client(server)) as client:
        yield client


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run_stdio(data_dir: Path) -> None:
    async with stdio_session(data_dir) as client:
        media = await common_flow(client)
        expect(Path(media["local_path"]).is_file(), "local_path does not exist")
        inbox = data_dir / "inbox"
        (inbox / "smoke.png").write_bytes(PNG)
        imported = await call(client, "media_import_path", path=str(inbox / "smoke.png"))
        step("media_import_path", imported["media"]["media_id"])
        outside = await client.call_tool("media_import_path", {"path": str(data_dir / "mcp-tools.sqlite")})
        expect(outside.is_error, "a file outside the import folders was read")
        step("import outside the inbox is refused")


async def run_http(data_dir: Path) -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "MCP_TOOLS_DATA_DIR": str(data_dir), "MCP_TOOLS_MOCK_DELAY_MS": "150"}
    log_path = data_dir.parent / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(  # noqa: ASYNC220
            [sys.executable, "-m", "mcp_tools", "serve", "--transport", "http", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            async with httpx2.AsyncClient(trust_env=False, timeout=10.0) as anonymous:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        if (await anonymous.get(f"{base}/healthz")).status_code == 200:
                            break
                    except httpx2.TransportError:
                        pass
                    expect(process.poll() is None and time.monotonic() < deadline, log_path.read_text())
                    await anyio.sleep(0.1)
                step("server is up", base)
                denied = await anonymous.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                expect(denied.status_code == 401, f"expected 401 without a key, got {denied.status_code}")
                step("request without API key", "401")

                match = re.search(r"new API key \(shown once, store it now\): (\S+)", log_path.read_text())
                expect(match is not None, "the server did not print an API key")
                assert match is not None
                headers = {"Authorization": f"Bearer {match.group(1)}"}
                async with (
                    httpx2.AsyncClient(trust_env=False, timeout=60.0, headers=headers) as authed,
                    Client(streamable_http_client(f"{base}/mcp", http_client=authed)) as client,
                ):
                    media = await common_flow(client)

                    slot = (await call(client, "media_upload", files=[{"filename": "ref.png"}]))["uploads"][0]
                    put = await anonymous.put(slot["upload_url"], content=PNG)
                    expect(put.status_code == 201, f"upload failed: {put.status_code} {put.text}")
                    confirmed = await call(client, "media_confirm", media_ids=[slot["media_id"]])
                    expect(confirmed["all_ready"], f"upload was not confirmed: {confirmed}")
                    step("media_upload + PUT + media_confirm", slot["media_id"])

                    used = await call(
                        client,
                        "generate_image",
                        params={
                            "model": "mock_image",
                            "prompt": "variation",
                            "medias": [{"role": "image", "value": slot["media_id"]}],
                        },
                        wait_seconds=10,
                    )
                    expect(used["jobs"][0]["status"] == "completed", f"reference generation failed: {used}")
                    step("generate with a reference")

                downloaded = await anonymous.get(media["url"])
                expect(downloaded.status_code == 200 and downloaded.content[:4] == b"\x89PNG", "signed download failed")
                tampered = await anonymous.get(media["url"][:-1] + ("0" if media["url"][-1] != "0" else "1"))
                expect(tampered.status_code == 403, "a tampered link was served")
                step("signed download", f"{len(downloaded.content)} bytes; tampered link 403")
        finally:
            process.terminate()
            process.wait(timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mcp-tools-smoke-") as temp:
        data_dir = Path(temp) / "data"
        print(f"smoke test over {args.transport}")
        try:
            anyio.run(run_stdio if args.transport == "stdio" else run_http, data_dir)
        except BaseException as exc:
            print(f"  FAIL  after {len(STEPS)} steps: {exc!r}")
            return 1
    print(f"all {len(STEPS)} steps passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
