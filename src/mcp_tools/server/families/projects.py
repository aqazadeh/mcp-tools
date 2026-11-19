"""Projects family: organise results into projects and folders.

Folders hold placements that point at jobs and media; deleting a folder or a project, or
removing an asset, never deletes the job or the file behind it.
"""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field

from mcp_tools.core.errors import CONFIRMATION_REQUIRED, ApiError
from mcp_tools.core.pagination import page_request
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import Wire
from mcp_tools.server.registry import Effect, ToolDef


class ProjectResult(Wire):
    kind: Literal["project"] = "project"
    schema_version: int = 1
    project: dict[str, Any]
    replayed: bool | None = None
    message: str | None = None


class ProjectListResult(Wire):
    kind: Literal["projects"] = "projects"
    schema_version: int = 1
    items: list[dict[str, Any]]
    has_more: bool = False
    next_cursor: str | None = None


class FolderResult(Wire):
    kind: Literal["folder"] = "folder"
    schema_version: int = 1
    folder: dict[str, Any]


class FolderListResult(Wire):
    kind: Literal["folders"] = "folders"
    schema_version: int = 1
    items: list[dict[str, Any]]


class DeletedResult(Wire):
    kind: Literal["deleted"] = "deleted"
    schema_version: int = 1
    deleted: str
    folders_removed: int
    placements_removed: int
    message: str = "Jobs and media files were kept; only the organisation was removed."


class AssetListResult(Wire):
    kind: Literal["assets"] = "assets"
    schema_version: int = 1
    folder_id: str
    items: list[dict[str, Any]]
    has_more: bool = False
    next_cursor: str | None = None


class AssetChangeResult(Wire):
    kind: Literal["asset_changes"] = "asset_changes"
    schema_version: int = 1
    items: list[dict[str, Any]]
    message: str | None = None


def _confirmed(flag: bool, name: str, what: str) -> None:
    if flag is not True:
        raise ApiError(
            CONFIRMATION_REQUIRED,
            f"{what} needs {name}: true",
            hint=f"Ask the user first. Only if they agree, call again with {name} set to true.",
        )


_PROJECT_ID = Field(description="A project id (prj_...).", max_length=64)
_FOLDER_ID = Field(description="A folder id (fld_...).", max_length=64)
_LIMIT = Field(description="Page size, 1-50 (default 20).")
_CURSOR = Field(description="next_cursor from the previous page.")


async def projects_list(
    ctx: Context,
    include_archived: Annotated[bool, Field(description="Also list archived projects.")] = False,
    limit: Annotated[int, _LIMIT] = 0,
    cursor: Annotated[str, _CURSOR] = "",
) -> Annotated[CallToolResult, ProjectListResult]:
    platform = platform_of(ctx)
    page = await platform.projects.list_projects(
        page_request(limit, cursor), principal=platform.principal, include_archived=include_archived
    )
    return ok(ProjectListResult(items=page.items, has_more=page.has_more, next_cursor=page.next_cursor))


async def projects_create(
    ctx: Context,
    name: Annotated[str, Field(description="Project name, 1-100 characters.", max_length=200)],
    description: Annotated[str, Field(description="Optional note on what the project is for.", max_length=2000)] = "",
    idempotency_key: Annotated[
        str, Field(description="Optional. Reuse the same key when retrying after a timeout.", max_length=128)
    ] = "",
) -> Annotated[CallToolResult, ProjectResult]:
    platform = platform_of(ctx)
    project, replayed = await platform.projects.create_project(
        name, description=description, principal=platform.principal, idempotency_key=idempotency_key or None
    )
    return ok(
        ProjectResult(
            project=project,
            replayed=True if replayed else None,
            message="Generate into it by passing root_folder_id (or a subfolder's id) as params.folder_id.",
        )
    )


async def projects_update(
    ctx: Context,
    project_id: Annotated[str, _PROJECT_ID],
    name: Annotated[str, Field(description="New name; omit to keep.", max_length=200)] = "",
    description: Annotated[str | None, Field(description="New description; empty string clears it.")] = None,
    archived: Annotated[bool | None, Field(description="true archives (hides) the project; false restores it.")] = None,
) -> Annotated[CallToolResult, ProjectResult]:
    platform = platform_of(ctx)
    project = await platform.projects.update_project(
        project_id, principal=platform.principal, name=name or None, description=description, archived=archived
    )
    return ok(ProjectResult(project=project))


async def projects_delete(
    ctx: Context,
    project_id: Annotated[str, _PROJECT_ID],
    confirm_delete: Annotated[
        bool, Field(description="Must be true. Set it only after the user agreed.", json_schema_extra={"const": True})
    ] = False,
) -> Annotated[CallToolResult, DeletedResult]:
    platform = platform_of(ctx)
    _confirmed(confirm_delete, "confirm_delete", "deleting a project")
    counts = await platform.projects.delete_project(project_id, principal=platform.principal)
    return ok(
        DeletedResult(deleted=project_id, folders_removed=counts["folders"], placements_removed=counts["placements"])
    )


async def folders_list(
    ctx: Context,
    project_id: Annotated[str, Field(description="List the whole folder tree of this project.", max_length=64)] = "",
    parent_id: Annotated[
        str, Field(description="Or: list only the direct subfolders of this folder.", max_length=64)
    ] = "",
) -> Annotated[CallToolResult, FolderListResult]:
    platform = platform_of(ctx)
    items = await platform.projects.list_folders(
        principal=platform.principal, project_id=project_id or None, parent_id=parent_id or None
    )
    return ok(FolderListResult(items=items))


async def folders_create(
    ctx: Context,
    parent_id: Annotated[
        str, Field(description="Parent folder (fld_...); a project's root_folder_id for the top level.")
    ],
    name: Annotated[str, Field(description="Folder name, 1-100 characters, unique in its parent.", max_length=200)],
) -> Annotated[CallToolResult, FolderResult]:
    platform = platform_of(ctx)
    return ok(
        FolderResult(
            folder=await platform.projects.create_folder(name, parent_id=parent_id, principal=platform.principal)
        )
    )


async def folders_update(
    ctx: Context,
    folder_id: Annotated[str, _FOLDER_ID],
    name: Annotated[str, Field(description="New name; omit to keep.", max_length=200)] = "",
    parent_id: Annotated[str, Field(description="Move under this folder of the same project; omit to keep.")] = "",
) -> Annotated[CallToolResult, FolderResult]:
    platform = platform_of(ctx)
    folder = await platform.projects.update_folder(
        folder_id, principal=platform.principal, name=name or None, parent_id=parent_id or None
    )
    return ok(FolderResult(folder=folder))


async def folders_delete(
    ctx: Context,
    folder_id: Annotated[str, _FOLDER_ID],
    confirm_delete: Annotated[
        bool, Field(description="Must be true. Set it only after the user agreed.", json_schema_extra={"const": True})
    ] = False,
) -> Annotated[CallToolResult, DeletedResult]:
    platform = platform_of(ctx)
    _confirmed(confirm_delete, "confirm_delete", "deleting a folder")
    counts = await platform.projects.delete_folder(folder_id, principal=platform.principal)
    return ok(
        DeletedResult(deleted=folder_id, folders_removed=counts["folders"], placements_removed=counts["placements"])
    )


async def assets_list(
    ctx: Context,
    folder_id: Annotated[str, _FOLDER_ID],
    limit: Annotated[int, _LIMIT] = 0,
    cursor: Annotated[str, _CURSOR] = "",
) -> Annotated[CallToolResult, AssetListResult]:
    platform = platform_of(ctx)
    page = await platform.projects.list_assets(folder_id, page_request(limit, cursor), principal=platform.principal)
    job_ids = [a.source_id for a in page.items if a.source_type == "job"]
    media_ids = [a.source_id for a in page.items if a.source_type == "media"]
    job_views = {v.job_id: v for v in await platform.jobs.get(job_ids, principal=platform.principal)} if job_ids else {}
    media_rows = {m.id: m for m in await platform.media.get_many(media_ids, principal=platform.principal)}
    items: list[dict[str, Any]] = []
    for asset in page.items:
        item: dict[str, Any] = {
            "asset_id": asset.asset_id,
            "source_type": asset.source_type,
            "source_id": asset.source_id,
        }
        if asset.source_type == "job" and asset.source_id in job_views:
            item["job"] = job_views[asset.source_id].to_dict()
        elif asset.source_id in media_rows:
            item["media"] = platform.jobs.media_view(media_rows[asset.source_id]).to_dict()
        items.append(item)
    return ok(AssetListResult(folder_id=folder_id, items=items, has_more=page.has_more, next_cursor=page.next_cursor))


async def assets_attach(
    ctx: Context,
    folder_id: Annotated[str, _FOLDER_ID],
    source_ids: Annotated[list[str], Field(description="1-50 job ids (job_...) and/or media ids (med_...).")],
) -> Annotated[CallToolResult, AssetChangeResult]:
    platform = platform_of(ctx)
    items = await platform.projects.attach(folder_id, [str(s) for s in source_ids], principal=platform.principal)
    return ok(AssetChangeResult(items=items))


async def assets_move(
    ctx: Context,
    asset_ids: Annotated[list[str], Field(description="1-50 asset ids (ast_...) from assets_list.")],
    folder_id: Annotated[str, Field(description="Destination folder (fld_...).", max_length=64)],
) -> Annotated[CallToolResult, AssetChangeResult]:
    platform = platform_of(ctx)
    items = await platform.projects.move_assets([str(a) for a in asset_ids], folder_id, principal=platform.principal)
    return ok(AssetChangeResult(items=items, message="Moved assets get a new asset_id."))


async def assets_remove(
    ctx: Context,
    asset_ids: Annotated[list[str], Field(description="1-50 asset ids (ast_...) from assets_list.")],
    confirm_remove: Annotated[
        bool, Field(description="Must be true. Set it only after the user agreed.", json_schema_extra={"const": True})
    ] = False,
) -> Annotated[CallToolResult, AssetChangeResult]:
    platform = platform_of(ctx)
    _confirmed(confirm_remove, "confirm_remove", "removing assets from a folder")
    items = await platform.projects.remove_assets([str(a) for a in asset_ids], principal=platform.principal)
    return ok(AssetChangeResult(items=items, message="The jobs and files themselves were kept."))


_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_DESTROY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
_W: frozenset[Effect] = frozenset({"write"})


def _def(name: str, handler: Any, title: str, description: str, annotations: ToolAnnotations) -> ToolDef:
    return ToolDef(
        name=name,
        family="projects",
        handler=handler,
        title=title,
        description=description,
        annotations=annotations,
        effects=frozenset() if annotations is _READ else _W,
    )


def tools() -> list[ToolDef]:
    return [
        _def(
            "projects_list",
            projects_list,
            "List projects",
            "List projects, newest first, with their root_folder_id. Read-only. Use it to find where the user's "
            "work is organised before generating into a folder or browsing assets.",
            _READ,
        ),
        _def(
            "projects_create",
            projects_create,
            "Create project",
            "Create a project: a named container with a root folder. Use it when the user starts a body of work "
            "they want kept together (a campaign, a storyboard). Pass the returned root_folder_id as "
            "params.folder_id to generate into it. Costs no credits.",
            _WRITE,
        ),
        _def(
            "projects_update",
            projects_update,
            "Update project",
            "Rename a project, change its description, or archive / restore it. Archiving only hides it from "
            "projects_list; nothing is deleted.",
            _WRITE,
        ),
        _def(
            "projects_delete",
            projects_delete,
            "Delete project",
            "Delete a project with all its folders and placements. The jobs and media files themselves are kept "
            "and stay usable by id. Cannot be undone: ask the user first and pass confirm_delete: true only "
            "after they agreed. Prefer archiving (projects_update) when unsure.",
            _DESTROY,
        ),
        _def(
            "folders_list",
            folders_list,
            "List folders",
            "List a project's folder tree (project_id) or the direct subfolders of one folder (parent_id), with "
            "asset counts. Read-only.",
            _READ,
        ),
        _def(
            "folders_create",
            folders_create,
            "Create folder",
            "Create a subfolder under a folder of a project (use the project's root_folder_id for the top "
            "level). Names are unique within a parent; nesting is limited to 8 levels.",
            _WRITE,
        ),
        _def(
            "folders_update",
            folders_update,
            "Rename or move folder",
            "Rename a folder and/or move it, with everything in it, under another folder of the same project. "
            "The root folder cannot be changed.",
            _WRITE,
        ),
        _def(
            "folders_delete",
            folders_delete,
            "Delete folder",
            "Delete a folder, its subfolders and their placements. The jobs and media files themselves are "
            "kept. Cannot be undone: ask the user first and pass confirm_delete: true only after they agreed.",
            _DESTROY,
        ),
        _def(
            "assets_list",
            assets_list,
            "List folder assets",
            "List what a folder contains, newest first: each asset points at a job (with its results) or a "
            "media item. Read-only. Subfolders are listed by folders_list, not here.",
            _READ,
        ),
        _def(
            "assets_attach",
            assets_attach,
            "Add to folder",
            "Put existing jobs or media into a folder by id. The source is not copied: the same job or file "
            "can sit in several folders. Attaching something twice is harmless. To generate straight into a "
            "folder, pass params.folder_id to a generate tool instead.",
            _WRITE,
        ),
        _def(
            "assets_move",
            assets_move,
            "Move assets",
            "Move assets (by asset_id from assets_list) to another folder, in the same or another project.",
            _WRITE,
        ),
        _def(
            "assets_remove",
            assets_remove,
            "Remove from folder",
            "Take assets out of a folder. Only the placement is removed; the job or media file is kept and "
            "stays usable by id. Ask the user first and pass confirm_remove: true only after they agreed.",
            _DESTROY,
        ),
    ]
