"""Preferences and housekeeping."""

import os
import time
from pathlib import Path

import pytest
from mcp import Client

from mcp_tools.config import Settings
from mcp_tools.core.maintenance import collect_garbage
from mcp_tools.server.build import Built
from tests.conftest import error_of, payload
from tests.helpers.media_samples import png_bytes

pytestmark = pytest.mark.anyio


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_level="WARNING", mock_delay_ms=20)


async def test_preferences_round_trip_and_validation(client: Client) -> None:
    empty = payload(await client.call_tool("preferences_get", {}))
    assert empty["preferences"] == {} and "default_image_model" in empty["available_keys"]

    project = payload(await client.call_tool("projects_create", {"name": "P"}))["project"]
    saved = payload(
        await client.call_tool(
            "preferences_update",
            {
                "patch": {
                    "default_image_model": "mock_image",
                    "default_aspect_ratio": "9:16",
                    "default_folder_id": project["root_folder_id"],
                    "style_notes": "  warm   light ",
                }
            },
        )
    )["preferences"]
    assert saved == {
        "default_image_model": "mock_image", "default_aspect_ratio": "9:16",
        "default_folder_id": project["root_folder_id"], "style_notes": "warm light",
    }  # fmt: skip
    cleared = payload(await client.call_tool("preferences_update", {"patch": {"style_notes": None}}))["preferences"]
    assert "style_notes" not in cleared and cleared["default_aspect_ratio"] == "9:16"

    for patch, code in [
        ({"daily_cap_credits": 999999}, "invalid_params"),
        ({"max_credits_per_call": 1}, "invalid_params"),
        ({"default_image_model": "mock_video"}, "invalid_params"),
        ({"default_image_model": "mock_upscale"}, "invalid_params"),
        ({"default_video_model": "nope"}, "unknown_model"),
        ({"default_aspect_ratio": "wide"}, "invalid_params"),
        ({"default_folder_id": "fld_" + "0" * 26}, "not_found"),
        ({"style_notes": "x" * 601}, "invalid_params"),
        ({}, "invalid_params"),
        ({"default_aspect_ratio": "1:1", "bogus": 1}, "invalid_params"),
    ]:
        assert error_of(await client.call_tool("preferences_update", {"patch": patch}))["code"] == code, patch
    # a rejected patch changes nothing, and budgets stay what the server says
    assert payload(await client.call_tool("preferences_get", {}))["preferences"] == cleared
    assert payload(await client.call_tool("balance", {}))["daily_cap"] == 10_000


async def test_gc_removes_only_what_nothing_can_use(client: Client, built: Built) -> None:
    platform = built.platform
    kept = payload(
        await client.call_tool("generate_image", {"params": {"model": "mock_image", "prompt": "p"}, "wait_seconds": 10})
    )
    kept_path = Path(kept["jobs"][0]["results"][0]["local_path"])

    media_id, nonce, _ = await platform.media.reserve_upload(
        principal=platform.principal, filename="x.png", ttl_seconds=1
    )

    async def once():
        yield png_bytes()

    assert await platform.media.receive_upload(media_id, nonce, once(), max_bytes=10_000)
    orphan = platform.data.blobs / "media" / "zz" / ("med_" + "0" * 26 + ".png")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(png_bytes())
    fresh = orphan.with_name("med_" + "1" * 26 + ".png")
    fresh.write_bytes(png_bytes())
    partial = platform.data.blobs / "tmp" / "dead.part"
    partial.write_bytes(b"x")
    old = time.time() - 3 * 86_400
    os.utime(orphan, (old, old))
    os.utime(partial, (old, old))
    await client.call_tool(
        "generate_image", {"params": {"model": "mock_image", "prompt": "q"}, "idempotency_key": "k1"}
    )

    time.sleep(1.1)  # noqa: ASYNC251 - let the upload slot expire
    files_before = sum(1 for p in platform.data.blobs.rglob("*") if p.is_file())
    preview = await collect_garbage(platform, dry_run=True)
    assert preview["expired_uploads"] == 1 and preview["orphan_files"] == 1 and preview["temporary_files"] == 1
    assert sum(1 for p in platform.data.blobs.rglob("*") if p.is_file()) == files_before  # a dry run deletes nothing

    report = await collect_garbage(platform)
    assert report["expired_uploads"] == 1 and report["orphan_files"] == 1 and report["temporary_files"] == 1
    assert report["old_media"] == 0 and report["replay_keys"] == 0  # the replay key is still inside its window
    assert not orphan.exists() and not partial.exists()
    assert fresh.exists() and kept_path.exists()  # recent files and registered media are left alone
    assert error_of(await client.call_tool("media_get", {"media_id": media_id}))["code"] == "not_found"

    everything = await collect_garbage(platform, media_older_than_days=0)
    assert everything["old_media"] == 0 and kept_path.exists()
