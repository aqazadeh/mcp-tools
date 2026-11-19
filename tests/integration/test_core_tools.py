"""Phase 3 tools through a real MCP client: batch, estimate, cancel, history, ledger, media."""

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest
from mcp import Client
from sqlalchemy import func, select

from mcp_tools.config import Settings
from mcp_tools.core.media.fetch import SafeFetcher
from mcp_tools.core.platform import Platform
from mcp_tools.db.schema import jobs, ledger_entries
from mcp_tools.server.build import Built, build_server
from mcp_tools.server.registry import Capabilities
from tests.conftest import error_of, payload
from tests.helpers.media_samples import HTML, png_bytes

pytestmark = pytest.mark.anyio


def _handler(request: httpx2.Request) -> httpx2.Response:
    bodies = {"/cat.png": png_bytes(64, 48), "/page": HTML}

    async def chunks():
        yield bodies[request.url.path]

    if request.url.path not in bodies:
        return httpx2.Response(404)
    return httpx2.Response(200, content=chunks())


async def _resolve(host: str, port: int) -> list[str]:
    return {"cdn.example.com": ["93.184.216.34"], "intra.example.com": ["10.0.0.5"]}.get(host, [])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        log_level="WARNING",
        mock_delay_ms=30,
        initial_credits=1_000,
        auto_approve_credits=200,
        max_credits_per_call=600,
    )


@pytest.fixture
def built(settings: Settings) -> Built:
    fetcher = SafeFetcher(resolver=_resolve, transport_factory=lambda: httpx2.MockTransport(_handler))
    return build_server(settings, platform=Platform(settings, fetcher=fetcher))


@pytest.fixture
async def client(built: Built) -> AsyncIterator[Client]:
    async with Client(built.mcp, raise_exceptions=True) as c:
        yield c


async def _count(built: Built, table: Any) -> int:
    async with built.platform.store.read_tx() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def _call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    schemas = {t.name: t.output_schema or {} for t in (await client.list_tools()).tools}
    result = await client.call_tool(tool, args)
    if not result.is_error:
        jsonschema.validate(result.structured_content, schemas[tool])
    return payload(result)


IMG = {"model": "mock_image", "prompt": "a fox"}
VID = {"model": "mock_video", "prompt": "a fox runs", "duration": 2}
AUD = {"model": "mock_audio", "prompt": "hello", "duration": 2}


async def test_video_and_audio_generate_real_files(client: Client) -> None:
    video = await _call(client, "generate_video", {"params": VID, "wait_seconds": 10})
    audio = await _call(client, "generate_audio", {"params": AUD, "wait_seconds": 10})
    assert video["credits"]["charged"] == 40 and audio["credits"]["charged"] == 4
    assert video["jobs"][0]["results"][0]["kind"] == "video"
    assert audio["jobs"][0]["results"][0]["mime"] == "audio/wav"
    wrong = error_of(await client.call_tool("generate_audio", {"params": VID}))
    assert wrong["code"] == "invalid_params" and wrong["recovery_tool"] == "generate_video"


async def test_estimate_prices_without_creating_anything(client: Client, built: Built) -> None:
    body = await _call(
        client,
        "estimate_cost",
        {
            "requests": [
                {"index": 7, "params": {**IMG, "count": 3, "resolution": "2k"}},
                {"index": 8, "params": {**VID, "duration": 10, "resolution": "720p"}},
                {"index": 9, "params": {"model": "nope"}},
                {"index": 10, "params": {"prompt": "no model"}},
                {"index": 11, "params": {**IMG, "bogus_field": 1}},
            ]
        },
    )
    by_index = {item["index"]: item for item in body["items"]}
    assert by_index[7]["credits"] == 60 and by_index[8]["credits"] == 400
    assert by_index[9]["error"]["code"] == "unknown_model" and by_index[10]["status"] == "rejected"
    assert by_index[11]["error"]["code"] == "invalid_params"
    assert body["total_credits"] == 460 and body["needs_max_credits"] is True and body["balance"] == 1_000
    assert "max_credits" in body["message"]
    assert await _count(built, jobs) == 0 and await _count(built, ledger_entries) == 1  # the opening grant


async def test_batch_rejects_bad_items_and_charges_only_good_ones(client: Client, built: Built) -> None:
    body = await _call(
        client,
        "generate_batch",
        {
            "requests": [
                {"index": 1, "params": {**IMG, "count": 2}},
                {"index": 2, "params": {"model": "nope", "prompt": "x"}},
                {"index": 3, "params": AUD},
                {"index": 4, "params": {**IMG, "medias": [{"role": "image", "value": "https://x.test/a.png"}]}},
            ],
            "idempotency_key": "batch-1",
        },
    )
    assert body["status"] == "submitted" and len(body["job_ids"]) == 3
    by_index = {item["index"]: item for item in body["items"]}
    assert len(by_index[1]["job_ids"]) == 2 and by_index[3]["status"] == "submitted"
    assert by_index[2]["status"] == "rejected" and by_index[4]["error"]["recovery_tool"] == "media_import_url"
    assert body["credits"] == {"charged": 24, "balance": 976}
    assert "2 request(s) were rejected" in body["message"]

    replay = await _call(
        client,
        "generate_batch",
        {
            "requests": [
                {"index": 1, "params": {**IMG, "count": 2}},
                {"index": 2, "params": {"model": "nope", "prompt": "x"}},
                {"index": 3, "params": AUD},
                {"index": 4, "params": {**IMG, "medias": [{"role": "image", "value": "https://x.test/a.png"}]}},
            ],
            "idempotency_key": "batch-1",
        },
    )
    assert replay["replayed"] is True and replay["job_ids"] == body["job_ids"]
    assert await _count(built, jobs) == 3

    waited = await _call(client, "jobs_wait", {"job_ids": body["job_ids"], "timeout_seconds": 10})
    assert waited["all_terminal"] and {j["status"] for j in waited["jobs"]} == {"completed"}


async def test_batch_budget_is_all_or_nothing(client: Client, built: Built) -> None:
    requests = [{"params": {**VID, "duration": 6}}, {"params": {**VID, "duration": 6}}]  # 240 > auto-approve 200
    gated = await _call(client, "generate_batch", {"requests": requests})
    assert gated["status"] == "approval_required" and gated["approval"]["estimated"] == 240
    assert [item["status"] for item in gated["items"]] == ["quoted", "quoted"]
    low = await _call(client, "generate_batch", {"requests": requests, "max_credits": 100})
    assert low["status"] == "approval_required" and low["approval"]["reason"] == "above_max_credits"
    ceiling = error_of(
        await client.call_tool(
            "generate_batch", {"requests": [{"params": {**VID, "duration": 10}}] * 4, "max_credits": 5_000}
        )
    )
    assert ceiling["code"] == "budget_exceeded"
    none_valid = await _call(client, "generate_batch", {"requests": [{"params": {"model": "nope"}}]})
    assert none_valid["status"] == "rejected" and "job_ids" not in none_valid
    assert await _count(built, jobs) == 0 and await _count(built, ledger_entries) == 1

    agreed = await _call(client, "generate_batch", {"requests": requests, "max_credits": 240})
    assert agreed["status"] == "submitted" and agreed["credits"]["balance"] == 760


@pytest.mark.parametrize("tool", ["generate_image", "generate_video", "generate_audio", "generate_batch"])
async def test_a_misspelled_cap_creates_nothing(client: Client, built: Built, tool: str) -> None:
    params = {"generate_image": IMG, "generate_video": VID, "generate_audio": AUD}.get(tool, IMG)
    args: dict[str, Any] = {"requests": [{"params": params}]} if tool == "generate_batch" else {"params": params}
    for typo in ("max_credit", "maxCredits", "max-credits", "budget"):
        err = error_of(await client.call_tool(tool, {**args, typo: 1}))
        assert err["code"] == "unknown_argument" and "max_credits" in err["allowed"]
    assert await _count(built, jobs) == 0 and await _count(built, ledger_entries) == 1


async def test_cancel_history_and_transactions(client: Client, built: Built) -> None:
    slow = await _call(client, "generate_image", {"params": {**IMG, "simulate": "slow"}})
    job_id = slow["jobs"][0]["job_id"]
    canceled = await _call(client, "job_cancel", {"job_id": job_id})
    final = await _call(client, "jobs_wait", {"job_ids": [job_id], "timeout_seconds": 10})
    assert final["jobs"][0]["status"] == "canceled"
    again = await _call(client, "job_cancel", {"job_id": job_id})
    assert again["refunded"] == 0 and "already finished" in again["message"]
    assert canceled["job"]["job_id"] == job_id
    assert error_of(await client.call_tool("job_cancel", {"job_id": "job_missing"}))["code"] == "not_found"

    done = await _call(client, "generate_image", {"params": {**IMG, "count": 3}, "wait_seconds": 10})
    done_ids = [j["job_id"] for j in done["jobs"]]

    shown = await _call(client, "show_jobs", {"job_ids": [*done_ids, job_id, "job_unknown"]})
    assert [j["status"] for j in shown["jobs"]] == ["completed"] * 3 + ["canceled", "not_found"]
    assert shown["all_terminal"] is True

    first = await _call(client, "show_generations", {"limit": 2})
    newest_first = sorted(done_ids, reverse=True)  # ids sort by time, then randomly within a millisecond
    assert [j["job_id"] for j in first["items"]] == newest_first[:2] and first["has_more"] is True
    second = await _call(client, "show_generations", {"limit": 2, "cursor": first["next_cursor"]})
    assert [j["job_id"] for j in second["items"]] == newest_first[2:] and "next_cursor" not in second
    everything = await _call(client, "show_generations", {"only_completed": False})
    assert len(everything["items"]) == 4
    assert (await _call(client, "show_generations", {"type": "video"}))["items"] == []
    assert error_of(await client.call_tool("show_generations", {"type": "gif"}))["allowed"] == [
        "image",
        "video",
        "audio",
    ]

    ledger = await _call(client, "transactions", {})
    kinds = [e["kind"] for e in ledger["items"]]
    assert kinds.count("spend") == 4 and kinds.count("refund") == 1 and kinds[-1] == "grant"
    refunds = await _call(client, "transactions", {"kind": "refund"})
    assert [e["job_id"] for e in refunds["items"]] == [job_id]
    assert (await _call(client, "balance", {}))["balance"] == 970


async def test_media_import_get_and_list(client: Client, built: Built) -> None:
    imported = await _call(client, "media_import_url", {"url": "https://cdn.example.com/cat.png?token=secret"})
    media = imported["media"]
    assert (media["kind"], media["mime"], media["width"], media["height"]) == ("image", "image/png", 64, 48)
    assert media["filename"] == "cat.png" and Path(media["local_path"]).is_file()

    for url, code in [
        ("https://intra.example.com/cat.png", "invalid_params"),
        ("http://cdn.example.com/cat.png", "invalid_params"),
        ("https://cdn.example.com/page", "invalid_params"),
        ("https://cdn.example.com/missing", "invalid_params"),
    ]:
        assert error_of(await client.call_tool("media_import_url", {"url": url}))["code"] == code

    inbox = built.platform.data.inbox
    (inbox / "photo.png").write_bytes(png_bytes(20, 10))
    local = await _call(client, "media_import_path", {"path": str(inbox / "photo.png")})
    assert local["media"]["width"] == 20
    refused = error_of(await client.call_tool("media_import_path", {"path": str(built.platform.data.database)}))
    assert refused["code"] == "not_found" and refused["allowed"] == [str(inbox)]
    missing = error_of(await client.call_tool("media_import_path", {"path": "/nonexistent/file.png"}))
    assert {k: v for k, v in missing.items()} == refused  # existence outside the roots is not revealed

    result = await client.call_tool("media_get", {"media_id": media["media_id"], "include_preview": True})
    assert payload(result)["preview"] == "attached"
    assert result.content[1].type == "image" and result.content[1].mime_type == "image/jpeg"
    plain = await _call(client, "media_get", {"media_id": media["media_id"]})
    assert "preview" not in plain and plain["media"]["media_id"] == media["media_id"]
    assert error_of(await client.call_tool("media_get", {"media_id": "med_nope"}))["recovery_tool"] == "media_list"

    used = await _call(
        client,
        "generate_image",
        {"params": {**IMG, "medias": [{"role": "image", "value": media["media_id"]}]}, "wait_seconds": 10},
    )
    assert used["jobs"][0]["status"] == "completed"

    listed = await _call(client, "media_list", {})
    assert len(listed["items"]) == 3
    assert len((await _call(client, "media_list", {"source": "imported"}))["items"]) == 2
    assert len((await _call(client, "media_list", {"source": "generated"}))["items"]) == 1
    assert (await _call(client, "media_list", {"kind": "audio"}))["items"] == []
    # nothing stored for the refused imports
    assert sum(1 for p in built.platform.data.blobs.rglob("*") if p.is_file()) == 3


async def test_local_import_is_not_offered_without_the_capability(settings: Settings) -> None:
    built = build_server(settings, capabilities=Capabilities())
    async with Client(built.mcp, raise_exceptions=True) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert "media_import_path" not in names and "media_import_url" in names


async def test_surface_budget_and_cross_references(client: Client, built: Built) -> None:
    listed = (await client.list_tools()).tools
    names = {t.name for t in listed}
    assert len(listed) == 37
    size = sum(len(t.model_dump_json(exclude_none=True)) for t in listed)
    assert size < 80_000, size  # about 1.7 KB per tool, output schemas included
    instructions = built.mcp.instructions or ""
    prefixes = "generate|jobs|job|media|models|show|estimate"
    mentioned = set(re.findall(rf"\b(?:{prefixes})_[a-z_]+\b", instructions)) - {
        "media_id",
        "job_id",
        "job_ids",
        "media_ids",
    }
    assert mentioned and mentioned <= names, mentioned - names
    for tool in built.tools:
        referenced = set(re.findall(rf"\b(?:{prefixes})_[a-z_]+\b", tool.description)) - {
            "media_id",
            "job_id",
            "job_ids",
            "media_ids",
        }
        assert referenced <= names, (tool.name, referenced - names)


async def test_transforms_take_one_image_and_nothing_else(client: Client, built: Built) -> None:
    source = await _call(client, "generate_image", {"params": IMG, "wait_seconds": 10})
    job_id = source["jobs"][0]["job_id"]
    width = source["jobs"][0]["results"][0]["width"]

    bigger = await _call(client, "upscale_image", {"image_id": job_id, "scale": 4, "wait_seconds": 10})
    assert bigger["jobs"][0]["status"] == "completed" and bigger["credits"]["charged"] == 10
    assert bigger["jobs"][0]["results"][0]["width"] == width * 4

    cutout = await _call(client, "remove_background", {"image_id": job_id, "wait_seconds": 10})
    assert cutout["jobs"][0]["results"][0]["mime"] == "image/png"

    assert error_of(await client.call_tool("upscale_image", {"image_id": job_id, "scale": 3}))["allowed"] == [2, 4]
    assert error_of(await client.call_tool("upscale_image", {"image_id": "med_nope"}))["code"] == "invalid_params"
    assert error_of(await client.call_tool("upscale_image", {"image_id": job_id, "prompt": "x"}))["code"] == (
        "unknown_argument"
    )
    wrong = error_of(await client.call_tool("upscale_image", {"image_id": job_id, "model": "mock_image"}))
    assert wrong["code"] == "invalid_params"


async def test_argument_type_errors_use_the_json_envelope(client: Client, built: Built) -> None:
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("generate_image", {"params": {**IMG, "count": "two"}}, "params.count"),
        ("generate_image", {"params": {**IMG, "medias": ["med_123"]}}, "params.medias.0"),
        ("generate_image", {"model": "mock_image", "prompt": "x"}, "params"),
        ("generate_image", {"params": IMG, "max_credits": "lots"}, "max_credits"),
        ("jobs_wait", {"job_ids": "job_x"}, "job_ids"),
        ("folders_create", {"name": "x"}, "parent_id"),
    ]
    for tool, arguments, field in cases:
        err = error_of(await client.call_tool(tool, arguments))
        assert err["code"] == "invalid_params" and field in err["message"], (tool, err)
        assert "pydantic" not in err["message"] and "input_value" not in err["message"] and tool in err["hint"]
    assert await _count(built, jobs) == 0


async def test_listings_identify_jobs_and_recommendations_match(client: Client) -> None:
    await _call(client, "generate_image", {"params": {**IMG, "prompt": "a red fox " * 30}, "wait_seconds": 10})
    (item,) = (await _call(client, "show_generations", {}))["items"]
    assert item["prompt"].startswith("a red fox") and len(item["prompt"]) == 120 and item["created_at_ms"] > 0
    assert error_of(await client.call_tool("show_generations", {"folder_id": "fld_" + "0" * 26}))["code"] == "not_found"

    speech = await _call(client, "models_explore", {"action": "recommend", "query": "narration voice"})
    assert all(m["output_type"] == "audio" for m in speech["items"]) and speech["hint"].startswith("Best match")
    vague = await _call(client, "models_explore", {"action": "recommend", "query": "zzz qqq"})
    assert vague["items"] and vague["hint"].startswith("No model matched")
    video = await _call(client, "generate_video", {"params": {**VID, "duration": 4}})
    assert "adjustments" not in video  # 4 -> 4 is not a correction worth reporting
