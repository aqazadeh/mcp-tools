"""Projects, folders and placements through the MCP client."""

from pathlib import Path
from typing import Any

import jsonschema
import pytest
from mcp import Client

from mcp_tools.config import Settings
from tests.conftest import error_of, payload

pytestmark = pytest.mark.anyio


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", log_level="WARNING", mock_delay_ms=20)


async def call(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    schemas = {t.name: t.output_schema or {} for t in (await client.list_tools()).tools}
    result = await client.call_tool(tool, args)
    assert not result.is_error, payload(result)
    jsonschema.validate(result.structured_content, schemas[tool])
    return payload(result)


async def err(client: Client, tool: str, **args: Any) -> dict[str, Any]:
    return error_of(await client.call_tool(tool, args))


async def test_generate_into_a_folder_and_organise(client: Client) -> None:
    created = await call(client, "projects_create", name="  Spring   campaign ", idempotency_key="p1")
    project = created["project"]
    assert project["name"] == "Spring campaign" and project["archived"] is False
    again = await call(client, "projects_create", name="Spring campaign", idempotency_key="p1")
    assert again["replayed"] is True and again["project"]["project_id"] == project["project_id"]
    root = project["root_folder_id"]

    shots = (await call(client, "folders_create", parent_id=root, name="shots"))["folder"]
    drafts = (await call(client, "folders_create", parent_id=shots["folder_id"], name="drafts"))["folder"]
    assert (
        drafts["depth"] == 2
        and (await err(client, "folders_create", parent_id=root, name="shots"))["code"] == "conflict"
    )

    done = await call(
        client,
        "generate_image",
        params={"model": "mock_image", "prompt": "p", "count": 2, "folder_id": shots["folder_id"]},
        wait_seconds=10,
    )
    job_ids = [j["job_id"] for j in done["jobs"]]
    listed = await call(client, "assets_list", folder_id=shots["folder_id"])
    assert sorted(a["source_id"] for a in listed["items"]) == sorted(job_ids)
    assert all(a["job"]["status"] == "completed" and a["job"]["results"] for a in listed["items"])
    assert len((await call(client, "show_generations", folder_id=shots["folder_id"]))["items"]) == 2

    missing = await err(
        client, "generate_image", params={"model": "mock_image", "prompt": "p", "folder_id": "fld_" + "0" * 26}
    )
    assert missing["code"] == "not_found" and missing["recovery_tool"] == "folders_list"

    media_id = done["jobs"][0]["results"][0]["media_id"]
    attached = await call(
        client, "assets_attach", folder_id=drafts["folder_id"], source_ids=[job_ids[0], media_id, "job_x", job_ids[0]]
    )
    assert [i["status"] for i in attached["items"]] == ["attached", "attached", "not_found", "already_there"]

    asset = attached["items"][1]["asset_id"]
    moved = await call(client, "assets_move", asset_ids=[asset, "ast_nope"], folder_id=root)
    assert [i["status"] for i in moved["items"]] == ["moved", "not_found"]
    root_assets = await call(client, "assets_list", folder_id=root)
    assert root_assets["items"][0]["media"]["media_id"] == media_id

    assert (await err(client, "assets_remove", asset_ids=[root_assets["items"][0]["asset_id"]]))[
        "code"
    ] == "confirmation_required"
    removed = await call(client, "assets_remove", asset_ids=[root_assets["items"][0]["asset_id"]], confirm_remove=True)
    assert removed["items"][0]["status"] == "removed"
    assert (await call(client, "media_get", media_id=media_id))["media"]["media_id"] == media_id  # the file stays

    tree = await call(client, "folders_list", project_id=project["project_id"])
    assert [(f["name"], f["asset_count"]) for f in tree["items"]] == [("(root)", 0), ("shots", 2), ("drafts", 1)]


async def test_folder_moves_renames_and_deletes(client: Client) -> None:
    project = (await call(client, "projects_create", name="P"))["project"]
    other = (await call(client, "projects_create", name="Q"))["project"]
    root = project["root_folder_id"]
    a = (await call(client, "folders_create", parent_id=root, name="a"))["folder"]["folder_id"]
    b = (await call(client, "folders_create", parent_id=a, name="b"))["folder"]["folder_id"]
    c = (await call(client, "folders_create", parent_id=root, name="c"))["folder"]["folder_id"]

    assert "into itself" in (await err(client, "folders_update", folder_id=a, parent_id=b))["message"]
    assert "into itself" in (await err(client, "folders_update", folder_id=a, parent_id=a))["message"]
    assert (
        "another project"
        in (await err(client, "folders_update", folder_id=a, parent_id=other["root_folder_id"]))["message"]
    )
    assert "root folder" in (await err(client, "folders_update", folder_id=root, name="x"))["message"]
    assert (await err(client, "folders_update", folder_id=c, name="a"))["code"] == "conflict"
    assert (await err(client, "folders_create", parent_id=root, name="a/b"))["code"] == "invalid_params"

    moved = (await call(client, "folders_update", folder_id=a, parent_id=c, name="renamed"))["folder"]
    assert (moved["parent_id"], moved["name"], moved["depth"]) == (c, "renamed", 2)
    tree = {
        f["folder_id"]: f["depth"]
        for f in (await call(client, "folders_list", project_id=project["project_id"]))["items"]
    }
    assert tree[b] == 3

    deep = b
    for level in range(4, 9):
        deep = (await call(client, "folders_create", parent_id=deep, name=f"l{level}"))["folder"]["folder_id"]
    assert "8 levels" in (await err(client, "folders_create", parent_id=deep, name="too deep"))["message"]

    job = (
        await call(
            client, "generate_image", params={"model": "mock_image", "prompt": "p", "folder_id": b}, wait_seconds=10
        )
    )["jobs"][0]["job_id"]
    assert (await err(client, "folders_delete", folder_id=c))["code"] == "confirmation_required"
    assert (await err(client, "folders_delete", folder_id=root, confirm_delete=True))[
        "recovery_tool"
    ] == "projects_delete"
    gone = await call(client, "folders_delete", folder_id=c, confirm_delete=True)
    assert gone["folders_removed"] == 8 and gone["placements_removed"] == 1
    assert (await call(client, "show_jobs", job_ids=[job]))["jobs"][0]["status"] == "completed"  # the job stays
    assert (await err(client, "assets_list", folder_id=b))["code"] == "not_found"


async def test_project_archive_and_delete(client: Client) -> None:
    project = (await call(client, "projects_create", name="Old", description="d"))["project"]
    pid = project["project_id"]
    job = (
        await call(
            client,
            "generate_image",
            params={"model": "mock_image", "prompt": "p", "folder_id": project["root_folder_id"]},
            wait_seconds=10,
        )
    )["jobs"][0]["job_id"]
    archived = await call(client, "projects_update", project_id=pid, archived=True, name="Older", description="")
    assert archived["project"]["archived"] is True and "description" not in archived["project"]
    assert (await call(client, "projects_list"))["items"] == []
    assert len((await call(client, "projects_list", include_archived=True))["items"]) == 1
    assert (await err(client, "projects_update", project_id=pid))["code"] == "invalid_params"
    assert (await err(client, "projects_delete", project_id=pid))["code"] == "confirmation_required"
    deleted = await call(client, "projects_delete", project_id=pid, confirm_delete=True)
    assert deleted["folders_removed"] == 1 and deleted["placements_removed"] == 1
    assert (await err(client, "projects_delete", project_id=pid, confirm_delete=True))["code"] == "not_found"
    assert (await call(client, "show_jobs", job_ids=[job]))["jobs"][0]["status"] == "completed"
    assert (await call(client, "show_generations"))["items"][0]["job_id"] == job
