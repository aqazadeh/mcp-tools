"""Widgets (MCP Apps): what the server publishes, who may call what, and the widget itself in a browser."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.shared.exceptions import MCPError

from mcp_tools.config import Settings
from mcp_tools.server.build import LEGACY_UI_META_KEY, build_server
from mcp_tools.server.ui.previews import META_KEY
from mcp_tools.server.ui.resources import RESULTS, WIDGETS
from tests.conftest import payload

pytestmark = pytest.mark.anyio

HARNESS = Path(__file__).resolve().parents[1] / "widget_harness" / "run.cjs"
UI_TOOLS = {"generate_image", "generate_video", "generate_audio", "show_jobs", "show_generations"}
IMG = {"model": "mock_image", "prompt": "p"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_level="WARNING", mock_delay_ms=20)


def apps_client(server: Any) -> Client:
    return Client(server, raise_exceptions=True, extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})])


def test_widget_html_is_static_and_uses_no_dangerous_api() -> None:
    forbidden = [
        r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\(", r"new Function",
        r"\bfetch\(", r"XMLHttpRequest", r"WebSocket", r"window\.open", r"<form", r"<link", r"srcdoc",
        r"\son[a-z]+\s*=\s*[\"']", r"src\s*=\s*[\"']https?:", r"@import", r"url\(", r"localStorage",
        r"document\.cookie",
        r"\{\{", r"\{%",
    ]  # fmt: skip
    for widget in WIDGETS.values():
        for pattern in forbidden:
            assert not re.search(pattern, widget.html), (widget.name, pattern)
        assert widget.html.count("<script") == 1 and "event.source !== window.parent" in widget.html
        assert re.fullmatch(r"ui://mcp-tools/[a-z]+\.[0-9a-f]{10}\.html", widget.uri)
        assert len(widget.html) < 40_000


async def test_ui_tools_point_at_a_readable_resource(settings: Settings) -> None:
    built = build_server(settings)
    async with apps_client(built.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        bound = {name for name, t in tools.items() if (t.meta or {}).get("ui", {}).get("resourceUri")}
        assert bound == UI_TOOLS
        for name in UI_TOOLS:
            meta = tools[name].meta or {}
            assert meta["ui"] == {"resourceUri": RESULTS.uri, "visibility": ["model"]}
            assert meta[LEGACY_UI_META_KEY] == RESULTS.uri  # older hosts read the flat key
        assert (tools["jobs_wait"].meta or {})["ui"] == {"visibility": ["model", "app"]}
        assert (tools["media_previews"].meta or {})["ui"] == {"visibility": ["app"]}

        (content,) = (await c.read_resource(RESULTS.uri)).contents
        assert content.mime_type == APP_MIME_TYPE and content.text == RESULTS.html  # type: ignore[union-attr]

        done = payload(await c.call_tool("generate_image", {"params": IMG, "wait_seconds": 10}))
        assert done["display"] == "widget"
        media_id = done["jobs"][0]["results"][0]["media_id"]
        previews = payload(await c.call_tool("media_previews", {"media_ids": [media_id, "med_" + "0" * 26]}))
        (item,) = previews["items"]
        assert item["data_url"].startswith("data:image/jpeg;base64,") and len(item["data_url"]) < 60_000


async def test_widget_results_carry_previews_in_meta(settings: Settings) -> None:
    """Hosts that render the widget but do not forward its own tool calls still get previews."""
    built = build_server(settings)
    async with apps_client(built.mcp) as c:
        result = await c.call_tool("generate_image", {"params": {**IMG, "count": 2}, "wait_seconds": 10})
        done = payload(result)
        ids = [job["results"][0]["media_id"] for job in done["jobs"]]
        inline = (result.meta or {})[META_KEY]
        assert set(inline) == set(ids)
        assert all(url.startswith("data:image/jpeg;base64,") and len(url) < 60_000 for url in inline.values())
        assert META_KEY not in json.dumps(done)  # the model-facing payload stays small

        shown = await c.call_tool("show_jobs", {"job_ids": [j["job_id"] for j in done["jobs"]]})
        assert set((shown.meta or {})[META_KEY]) == set(ids)
        history = await c.call_tool("show_generations", {})
        assert set(ids) <= set((history.meta or {})[META_KEY])

        queued = await c.call_tool("generate_image", {"params": IMG})  # not finished yet: nothing to preview
        assert not (queued.meta or {}).get(META_KEY)


async def test_clients_without_apps_do_not_get_widget_helpers(settings: Settings) -> None:
    built = build_server(settings)
    async with Client(built.mcp, raise_exceptions=True) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "media_previews" not in names and names >= UI_TOOLS
        with pytest.raises(MCPError):
            await c.call_tool("media_previews", {"media_ids": ["med_" + "0" * 26]})
        plain = payload(await c.call_tool("generate_image", {"params": IMG, "wait_seconds": 10}))
        assert plain["display"] == "none" and plain["jobs"][0]["results"]  # complete without any widget
        assert META_KEY not in ((await c.call_tool("show_generations", {})).meta or {})  # nobody would see them


async def test_ui_can_be_switched_off(tmp_path: Path) -> None:
    built = build_server(Settings(data_dir=tmp_path / "data", log_level="WARNING", ui_enabled=False))
    async with apps_client(built.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "media_previews" not in tools
        assert all("resourceUri" not in (t.meta or {}).get("ui", {}) for t in tools.values())
        assert LEGACY_UI_META_KEY not in (tools["generate_image"].meta or {})


def test_widget_runs_in_a_sandboxed_frame_under_the_default_csp() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    widget = Path(str(HARNESS.parent / "results.under-test.html"))
    widget.write_text(RESULTS.html, encoding="utf-8")
    try:
        run = subprocess.run(
            [node, str(HARNESS), str(widget)], capture_output=True, text=True, timeout=120, check=False
        )
    finally:
        widget.unlink(missing_ok=True)
    if run.returncode != 0 and ("Cannot find module" in run.stderr or "Executable doesn't exist" in run.stderr):
        pytest.skip("playwright with chromium is not available")
    assert run.returncode == 0, run.stderr[-2000:]
    report = json.loads(run.stdout)

    for mode in ("normal", "late"):
        result = report[mode]
        assert result["problems"] == [] and result["network"] == [], (mode, result["problems"], result["network"])
        sent = [m for m in result["log"] if isinstance(m, dict)]
        assert not any(m.get("pwned") for m in sent)
        methods = [m.get("method") for m in sent if "method" in m]
        assert methods[0] == "ui/initialize" and "ui/notifications/initialized" in methods
        assert all(m.get("jsonrpc") == "2.0" for m in sent)
        init = next(m for m in sent if m.get("method") == "ui/initialize")
        assert set(init["params"]) == {"appInfo", "appCapabilities", "protocolVersion"}
        assert init["params"]["protocolVersion"] == "2026-01-26" and {"name", "version"} <= set(
            init["params"]["appInfo"]
        )
        sizes = [m["params"] for m in sent if m.get("method") == "ui/notifications/size-changed"]
        assert sizes and all(set(p) == {"width", "height"} and p["height"] > 0 for p in sizes)
        assert result["dom"]["theme"] == "dark"
    assert [m.get("method") for m in report["late"]["log"] if isinstance(m, dict)].count("ui/initialize") == 2

    normal = report["normal"]
    sent = [m for m in normal["log"] if isinstance(m, dict)]
    calls = [m["params"] for m in sent if m.get("method") == "tools/call"]
    assert {c["name"] for c in calls} == {"jobs_wait", "media_previews"}  # only read-only tools
    waits = [c for c in calls if c["name"] == "jobs_wait"]
    assert len(waits) == 2 and all(c["arguments"] == {"job_ids": ["job_a"], "timeout_seconds": 0} for c in waits)
    answers = {m["id"]: m for m in sent if "id" in m and "method" not in m}
    assert answers[9001] == {"jsonrpc": "2.0", "id": 9001, "result": {}}
    assert answers[9002]["error"]["code"] == -32601

    dom = normal["dom"]
    assert dom["tiles"] == 2 and dom["injected"] == 0
    assert dom["images"] == ["data:image/jpeg;base64,|1"]  # only the data: image was accepted, and it decoded
    assert "1 of 2 ready" in dom["text"] and "<b>x</b>.png" in dom["text"] and "<img src=x" in dom["text"]
