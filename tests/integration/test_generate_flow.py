"""generate_image / jobs_wait / balance through a real MCP client (in memory, mock provider)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import pytest
from mcp import Client
from sqlalchemy import func, select

from mcp_tools.config import Settings
from mcp_tools.core.platform import Platform
from mcp_tools.db.schema import jobs, ledger_entries
from mcp_tools.server.build import Built, build_server
from tests.conftest import error_of, payload
from tests.helpers.providers import CATALOG_DIR, ScriptedProvider

pytestmark = pytest.mark.anyio


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_level="WARNING", mock_delay_ms=40, initial_credits=1_000)


async def _count(built: Built, table: Any) -> int:
    async with built.platform.store.read_tx() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def _generate(client: Client, params: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return payload(await client.call_tool("generate_image", {"params": params, **extra}))


async def test_generate_wait_and_balance(client: Client) -> None:
    schemas = {t.name: t.output_schema or {} for t in (await client.list_tools()).tools}
    result = await client.call_tool(
        "generate_image",
        {
            "params": {"model": "mock_image", "prompt": "a red fox", "aspect_ratio": "2:1", "count": 2},
            "context": "t",
        },
    )
    body = payload(result)
    jsonschema.validate(result.structured_content, schemas["generate_image"])
    assert body["status"] == "submitted" and [j["status"] for j in body["jobs"]] == ["queued", "queued"]
    assert body["credits"] == {"charged": 20, "balance": 980}
    assert body["adjustments"][0]["to"] == "16:9" and body["next"]["tool"] == "jobs_wait"

    job_ids = [j["job_id"] for j in body["jobs"]]
    waited_result = await client.call_tool("jobs_wait", {"job_ids": job_ids, "timeout_seconds": 10})
    waited = payload(waited_result)
    jsonschema.validate(waited_result.structured_content, schemas["jobs_wait"])
    assert waited["all_terminal"] is True and "next" not in waited
    for job in waited["jobs"]:
        (output,) = job["results"]
        assert job["status"] == "completed" and job["credits"] == 10
        assert (output["kind"], output["mime"], output["width"], output["height"]) == (
            "image",
            "image/png",
            512,
            288,
        )
        assert output["media_id"].startswith("med_") and Path(output["local_path"]).is_file()
        assert "url" not in output  # stdio-style platform: no URL issuer

    balance = payload(await client.call_tool("balance", {}))
    assert (balance["balance"], balance["spent_today"]) == (980, 20)
    assert balance["auto_approve_credits"] == 500 and balance["credit_usd"] == 0.001


async def test_wait_seconds_returns_results_inline(client: Client) -> None:
    body = await _generate(client, {"model": "mock_image", "prompt": "x"}, wait_seconds=10)
    (job,) = body["jobs"]
    assert job["status"] == "completed" and job["results"][0]["kind"] == "image" and "next" not in body


async def test_a_finished_job_can_be_used_as_a_reference(client: Client) -> None:
    first = await _generate(client, {"model": "mock_image", "prompt": "base"}, wait_seconds=10)
    job_id = first["jobs"][0]["job_id"]
    media_id = first["jobs"][0]["results"][0]["media_id"]
    for value in (job_id, media_id):
        second = await _generate(
            client,
            {"model": "mock_image", "prompt": "variation", "medias": [{"value": value}]},
            wait_seconds=10,
        )
        assert second["jobs"][0]["status"] == "completed"
        assert second["adjustments"][0]["field"] == "medias[0].role"

    running = await _generate(client, {"model": "mock_image", "prompt": "slow", "simulate": "slow"})
    not_ready = error_of(
        await client.call_tool(
            "generate_image",
            {
                "params": {
                    "model": "mock_image",
                    "prompt": "x",
                    "medias": [{"value": running["jobs"][0]["job_id"]}],
                }
            },
        )
    )
    assert not_ready["code"] == "invalid_params" and not_ready["recovery_tool"] == "jobs_wait"


async def test_failed_and_moderated_jobs_are_refunded(client: Client) -> None:
    failed = await _generate(client, {"model": "mock_image", "prompt": "x", "simulate": "fail"}, wait_seconds=10)
    assert failed["jobs"][0]["status"] == "failed" and failed["jobs"][0]["error"]["code"] == "provider_error"
    moderated = await _generate(
        client, {"model": "mock_image", "prompt": "x", "simulate": "moderated"}, wait_seconds=10
    )
    assert moderated["jobs"][0]["status"] == "moderated" and moderated["jobs"][0]["error"]["code"] == "content_policy"
    assert payload(await client.call_tool("balance", {}))["balance"] == 1_000


async def test_approval_is_data_and_creates_nothing(client: Client, built: Built) -> None:
    schemas = {t.name: t.output_schema or {} for t in (await client.list_tools()).tools}
    under = await _generate(client, {"model": "mock_image", "prompt": "x", "resolution": "2k", "count": 4})
    assert under["status"] == "submitted"  # 80 credits: under the auto-approve amount

    low_cap = await client.call_tool(
        "generate_image", {"params": {"model": "mock_image", "prompt": "x", "count": 4}, "max_credits": 30}
    )
    body = payload(low_cap)
    jsonschema.validate(low_cap.structured_content, schemas["generate_image"])
    assert body["status"] == "approval_required" and body["jobs"] == []
    assert body["approval"] == {"estimated": 40, "limit": 30, "reason": "above_max_credits"}
    assert "Nothing was created" in body["message"]
    assert await _count(built, jobs) == 4  # only the first call created jobs

    allowed = await _generate(client, {"model": "mock_image", "prompt": "x", "count": 4}, max_credits=40)
    assert allowed["status"] == "submitted" and allowed["credits"]["charged"] == 40

    negative = error_of(
        await client.call_tool("generate_image", {"params": {"model": "mock_image", "prompt": "x"}, "max_credits": -1})
    )
    assert negative["code"] == "invalid_params"


async def test_server_limits_cannot_be_raised_by_the_caller(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        log_level="WARNING",
        mock_delay_ms=20,
        initial_credits=1_000,
        auto_approve_credits=15,
        max_credits_per_call=30,
        daily_cap_credits=50,
    )
    built = build_server(settings)
    async with Client(built.mcp, raise_exceptions=True) as client:
        gated = await _generate(client, {"model": "mock_image", "prompt": "x", "count": 2})
        assert gated["status"] == "approval_required" and gated["approval"]["reason"] == "above_auto_approve"

        ceiling = error_of(
            await client.call_tool(
                "generate_image",
                {"params": {"model": "mock_image", "prompt": "x", "count": 4}, "max_credits": 10_000},
            )
        )
        assert ceiling["code"] == "budget_exceeded" and ceiling["details"] == {"estimated": 40, "ceiling": 30}

        for _ in range(2):  # 2 x 20 credits = 40 of the 50 daily credits
            ok = await _generate(client, {"model": "mock_image", "prompt": "x", "count": 2}, max_credits=20)
            assert ok["status"] == "submitted"
        capped = error_of(
            await client.call_tool(
                "generate_image",
                {"params": {"model": "mock_image", "prompt": "x", "count": 2}, "max_credits": 20},
            )
        )
        assert capped["code"] == "budget_exceeded" and capped["recovery_tool"] == "balance"
        assert await _count(built, jobs) == 4 and await _count(built, ledger_entries) == 5  # grant + 4 spends


async def test_insufficient_credits_creates_nothing(tmp_path: Path) -> None:
    built = build_server(Settings(data_dir=tmp_path / "data", log_level="WARNING", initial_credits=15))
    async with Client(built.mcp, raise_exceptions=True) as client:
        err = error_of(
            await client.call_tool("generate_image", {"params": {"model": "mock_image", "prompt": "x", "count": 2}})
        )
        assert err["code"] == "insufficient_credits"
        assert await _count(built, jobs) == 0 and await _count(built, ledger_entries) == 1  # opening grant only


async def test_a_misspelled_cap_fails_loudly_and_spends_nothing(client: Client, built: Built) -> None:
    typo = error_of(
        await client.call_tool("generate_image", {"params": {"model": "mock_image", "prompt": "x"}, "max_credit": 5})
    )
    assert typo["code"] == "unknown_argument" and "max_credits" in typo["allowed"]
    assert await _count(built, jobs) == 0 and await _count(built, ledger_entries) == 1


async def test_request_errors_point_to_the_fix(client: Client) -> None:
    wrong_tool = error_of(await client.call_tool("generate_image", {"params": {"model": "mock_video", "prompt": "x"}}))
    assert wrong_tool["recovery_tool"] == "generate_video"
    transform = error_of(await client.call_tool("generate_image", {"params": {"model": "mock_upscale"}}))
    assert "upscale model" in transform["message"]
    unknown = error_of(await client.call_tool("generate_image", {"params": {"model": "nope", "prompt": "x"}}))
    assert unknown["code"] == "unknown_model"
    url = error_of(
        await client.call_tool(
            "generate_image",
            {"params": {"model": "mock_image", "prompt": "x", "medias": [{"value": "https://x.test/a.png"}]}},
        )
    )
    assert url["recovery_tool"] == "media_import_url"
    stringified = await _generate(client, '{"model": "mock_image", "prompt": "from a JSON string"}')  # type: ignore[arg-type]
    assert stringified["status"] == "submitted"


async def test_idempotency_key_replays_and_detects_reuse(client: Client, built: Built) -> None:
    params = {"model": "mock_image", "prompt": "same request"}
    first = await _generate(client, params, idempotency_key="key-1")
    again = await _generate(client, {"prompt": "same request", "model": "mock_image"}, idempotency_key="key-1")
    assert again["jobs"][0]["job_id"] == first["jobs"][0]["job_id"] and again["replayed"] is True
    assert "replayed" not in first and await _count(built, jobs) == 1
    assert payload(await client.call_tool("balance", {}))["balance"] == 990  # debited once

    reused = error_of(
        await client.call_tool(
            "generate_image",
            {"params": {"model": "mock_image", "prompt": "different"}, "idempotency_key": "key-1"},
        )
    )
    assert reused["code"] == "idempotency_key_reused"
    bad_key = error_of(await client.call_tool("generate_image", {"params": params, "idempotency_key": "has space"}))
    assert bad_key["code"] == "invalid_params"


async def test_jobs_wait_edge_cases(client: Client) -> None:
    slow = await _generate(client, {"model": "mock_image", "prompt": "x", "simulate": "slow"})
    job_id = slow["jobs"][0]["job_id"]
    snapshot = payload(
        await client.call_tool("jobs_wait", {"job_ids": [job_id, "job_" + "0" * 26], "timeout_seconds": 0})
    )
    assert [j["status"] for j in snapshot["jobs"]] == ["running", "not_found"] or [
        j["status"] for j in snapshot["jobs"]
    ] == ["queued", "not_found"]
    assert snapshot["all_terminal"] is False and 2 <= snapshot["poll_after_seconds"] <= 10
    assert snapshot["next"]["tool"] == "jobs_wait"
    too_many = error_of(await client.call_tool("jobs_wait", {"job_ids": [job_id] * 13}))
    assert too_many["code"] == "invalid_params"
    empty = error_of(await client.call_tool("jobs_wait", {"job_ids": []}))
    assert empty["code"] == "invalid_params"


@pytest.fixture
async def slow_submit(tmp_path: Path) -> AsyncIterator[tuple[Client, Built, ScriptedProvider]]:
    settings = Settings(
        data_dir=tmp_path / "data", catalog_dirs=str(CATALOG_DIR), log_level="WARNING", initial_credits=1_000
    )
    provider = ScriptedProvider(submit_delay=0.4)
    built = build_server(settings, platform=Platform(settings, providers=[provider]))
    async with Client(built.mcp) as client:
        yield client, built, provider


async def test_a_cancelled_wait_does_not_disturb_the_job(
    slow_submit: tuple[Client, Built, ScriptedProvider],
) -> None:
    """The client gives up while the provider call is in flight: the handler is cancelled, the job is not."""
    client, built, provider = slow_submit
    created = payload(await client.call_tool("generate_image", {"params": {"model": "fake_image", "prompt": "x"}}))
    job_id = created["jobs"][0]["job_id"]
    with pytest.raises(Exception):  # noqa: B017, PT011 - the SDK's timeout error type is not part of the contract
        await client.call_tool("jobs_wait", {"job_ids": [job_id], "timeout_seconds": 10}, read_timeout_seconds=0.05)
    await anyio.sleep(0.05)
    final = payload(await client.call_tool("jobs_wait", {"job_ids": [job_id], "timeout_seconds": 10}))
    assert final["jobs"][0]["status"] == "completed"
    assert len(provider.submits) == 1
    async with built.platform.store.read_tx() as conn:
        kinds = [
            r[0]
            for r in (await conn.execute(select(ledger_entries.c.kind).where(ledger_entries.c.ref_id == job_id))).all()
        ]
    assert kinds == ["spend"]
