"""The app registry through its three meta-tools."""

from pathlib import Path
from typing import Any

import jsonschema
import pytest
from mcp import Client

from mcp_tools.config import Settings
from mcp_tools.server.build import Built
from tests.conftest import error_of, payload

pytestmark = pytest.mark.anyio


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_level="WARNING", mock_delay_ms=20, auto_approve_credits=25)


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    schemas = {t.name: t.output_schema or {} for t in (await client.list_tools()).tools}
    result = await client.call_tool(tool, args)
    assert not result.is_error, payload(result)
    jsonschema.validate(result.structured_content, schemas[tool])
    return payload(result)


async def invoke(client: Client, app_id: str, action: str, arguments: dict[str, Any], **extra: Any) -> dict[str, Any]:
    revision = (await call(client, "apps_describe", app_id=app_id))["manifest_revision"]
    return await call(
        client, "apps_invoke", app_id=app_id, action=action, manifest_revision=revision, arguments=arguments, **extra
    )


async def image(client: Client) -> dict[str, Any]:
    done = await call(
        client, "generate_image", params={"model": "mock_image", "prompt": "p", "aspect_ratio": "16:9"}, wait_seconds=10
    )
    return done["jobs"][0]["results"][0]


async def test_search_describe_invoke(client: Client, built: Built) -> None:
    found = await call(client, "apps_search", query="crop an image to square")
    assert found["items"][0]["app_id"] == "image_tools" and "crop" in found["items"][0]["actions"]
    assert {a["app_id"] for a in (await call(client, "apps_search"))["items"]} == {"image_tools", "storyboard"}
    assert (await call(client, "apps_search", query="quantum chemistry"))["items"] == []

    overview = await call(client, "apps_describe", app_id="image_tools")
    assert (
        all("input_schema" not in a for a in overview["app"]["actions"]) and overview["next"]["tool"] == "apps_describe"
    )
    detail = await call(client, "apps_describe", app_id="image_tools", action="crop")
    (crop,) = detail["app"]["actions"]
    assert crop["input_schema"]["required"] == ["image_id", "aspect_ratio"]
    assert crop["annotations"] == {"read_only": False, "destructive": False, "spends_credits": False}

    source = await image(client)
    before = (await call(client, "balance"))["balance"]
    cropped = await invoke(client, "image_tools", "crop", {"image_id": source["media_id"], "aspect_ratio": "1:1"})
    media = cropped["output"]["media"]
    assert cropped["status"] == "completed" and cropped["run_id"].startswith("run_")
    assert media["width"] == media["height"] == source["height"] and Path(media["local_path"]).is_file()

    small = await invoke(
        client, "image_tools", "resize", {"image_id": source["media_id"], "width": 100, "format": "jpeg"}
    )
    assert (small["output"]["media"]["width"], small["output"]["media"]["mime"]) == (100, "image/jpeg")
    cover = await invoke(
        client, "image_tools", "resize", {"image_id": source["media_id"], "width": 64, "height": 64, "fit": "cover"}
    )
    assert (cover["output"]["media"]["width"], cover["output"]["media"]["height"]) == (64, 64)
    webp = await invoke(client, "image_tools", "convert", {"image_id": media["media_id"], "format": "webp"})
    assert webp["output"]["media"]["mime"] == "image/webp"
    sheet = await invoke(
        client, "image_tools", "contact_sheet",
        {"image_ids": [source["media_id"], media["media_id"], small["output"]["media"]["media_id"]], "cell_size": 64},
    )  # fmt: skip
    assert sheet["output"]["count"] == 3 and sheet["output"]["media"]["width"] > 3 * 64
    assert (await call(client, "balance"))["balance"] == before  # local operations are free


async def test_invoke_refuses_stale_manifests_and_bad_arguments(client: Client) -> None:
    source = await image(client)
    revision = (await call(client, "apps_describe", app_id="image_tools"))["manifest_revision"]

    async def refused(**overrides: Any) -> dict[str, Any]:
        args = {"app_id": "image_tools", "action": "crop", "manifest_revision": revision,
                "arguments": {"image_id": source["media_id"], "aspect_ratio": "1:1"}, **overrides}  # fmt: skip
        return error_of(await client.call_tool("apps_invoke", args))

    stale = await refused(manifest_revision="000000000000")
    assert stale["code"] == "conflict" and stale["recovery_tool"] == "apps_describe"
    assert stale["details"]["manifest_revision"] == revision
    assert (await refused(app_id="nope"))["allowed"] == ["image_tools", "storyboard"]
    assert (await refused(action="explode"))["code"] == "not_found"
    for bad in (
        {"image_id": source["media_id"]},
        {"image_id": source["media_id"], "aspect_ratio": "wide"},
        {"image_id": source["media_id"], "aspect_ratio": "1:1", "extra": 1},
        {"image_id": 5, "aspect_ratio": "1:1"},
    ):
        assert (await refused(arguments=bad))["code"] == "invalid_params"
    assert (await refused(arguments={"image_id": "med_" + "0" * 26, "aspect_ratio": "1:1"}))["code"] == "not_found"
    assert (await refused(arguments={"image_id": source["media_id"], "aspect_ratio": "0:1"}))[
        "code"
    ] == "invalid_params"
    assert (await refused(max_credit=5))["code"] == "unknown_argument"


async def test_storyboard_is_a_tracked_paid_run(client: Client, built: Built) -> None:
    shots = [{"prompt": "a fox wakes up"}, {"prompt": "the fox runs"}, {"prompt": "the fox sleeps"}]
    arguments = {"model": "mock_image", "style": "watercolour, soft light", "shots": shots, "aspect_ratio": "16:9"}

    gated = await invoke(client, "storyboard", "create", arguments)
    assert gated["status"] == "approval_required" and gated["output"]["estimated"] == 30 and "run_id" not in gated
    assert (await call(client, "show_generations", only_completed=False))["items"] == []

    started = await invoke(client, "storyboard", "create", arguments, max_credits=30, idempotency_key="sb-1")
    assert started["status"] == "running" and started["credits"] == 30 and len(started["output"]["job_ids"]) == 3
    again = await invoke(client, "storyboard", "create", arguments, max_credits=30, idempotency_key="sb-1")
    assert again["run_id"] == started["run_id"] and again["replayed"] is True

    await call(client, "jobs_wait", job_ids=started["output"]["job_ids"], timeout_seconds=10)
    status = await invoke(client, "storyboard", "status", {"run_id": started["run_id"]})
    assert status["status"] == "completed" and status["output"]["done"] is True and status["output"]["completed"] == 3
    assert [s["shot"] for s in status["output"]["shots"]] == [1, 2, 3] and "contact_sheet" in status["output"]["next"]

    async with built.platform.store.read_tx() as conn:
        from sqlalchemy import select

        from mcp_tools.db.schema import jobs

        prompts = [
            r[0]["prompt"] for r in (await conn.execute(select(jobs.c.params_json).order_by(jobs.c.group_index))).all()
        ]
    assert prompts[0] == "watercolour, soft light. a fox wakes up"

    slow = await invoke(
        client, "storyboard", "create",
        {"model": "mock_image", "shots": [{"prompt": "a"}, {"prompt": "b"}]}, max_credits=20,
    )  # fmt: skip
    revision = (await call(client, "apps_describe", app_id="storyboard"))["manifest_revision"]
    cancel_args = {
        "app_id": "storyboard",
        "action": "cancel",
        "manifest_revision": revision,
        "arguments": {"run_id": slow["run_id"]},
    }
    assert error_of(await client.call_tool("apps_invoke", cancel_args))["code"] == "confirmation_required"
    cancelled = payload(await client.call_tool("apps_invoke", {**cancel_args, "confirm_destructive": True}))
    assert cancelled["status"] == "completed" and "cancel_requested_for" in cancelled["output"]
    wrong = {**cancel_args, "action": "status", "arguments": {"run_id": cancelled["run_id"]}}
    assert error_of(await client.call_tool("apps_invoke", wrong))["code"] in ("invalid_params", "not_found")
