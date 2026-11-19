"""Projects, folders and placements.

A source (a job or a media item) is stored once. Folders hold *placements* that point at
sources, so the same result can sit in several folders and removing it from a folder, or
deleting a folder or a whole project, never deletes the file or the job behind it.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.billing.idempotency import IdempotencyStore, check_key, request_hash
from mcp_tools.core.clock import Clock
from mcp_tools.core.errors import CONFLICT, INVALID_PARAMS, NOT_FOUND, QUOTA_EXCEEDED, ApiError
from mcp_tools.core.ids import id_prefix, is_id, new_id
from mcp_tools.core.pagination import Page, PageRequest, encode_cursor
from mcp_tools.core.principal import Principal
from mcp_tools.db.schema import folders, jobs, media, placements, projects
from mcp_tools.db.sql import insert_ignore
from mcp_tools.db.store import Store

MAX_DEPTH = 8
MAX_NAME_CHARS = 100
MAX_PROJECTS = 500
MAX_FOLDERS_PER_PROJECT = 2000
MAX_ATTACH = 50
ROOT_NAME = "(root)"


@dataclass(frozen=True)
class Placement:
    asset_id: str
    folder_id: str
    source_type: str
    source_id: str
    created_at: int


def clean_name(name: str, what: str) -> str:
    cleaned = " ".join("".join(ch for ch in str(name) if ch.isprintable()).split())
    if not cleaned or len(cleaned) > MAX_NAME_CHARS:
        raise ApiError(
            INVALID_PARAMS, f"{what} name must be 1 to {MAX_NAME_CHARS} characters", hint="Use a short name."
        )
    if cleaned == ROOT_NAME or "/" in cleaned:
        raise ApiError(INVALID_PARAMS, f"{what} name must not contain '/'", hint="Use a plain name.")
    return cleaned


def _missing(what: str, identifier: str, tool: str) -> ApiError:
    return ApiError(NOT_FOUND, f"no {what} with id {identifier!r}", hint=f"List them with {tool}.", recovery_tool=tool)


def _project_view(row: Any) -> dict[str, Any]:
    view = {
        "project_id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "root_folder_id": row["root_folder_id"],
        "archived": row["archived_at"] is not None,
        "created_at_ms": row["created_at"],
    }
    return {k: v for k, v in view.items() if v is not None}


def _folder_view(row: Any) -> dict[str, Any]:
    view = {
        "folder_id": row["id"],
        "project_id": row["project_id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "depth": row["depth"],
        "is_root": row["parent_id"] is None,
    }
    return {k: v for k, v in view.items() if v is not None}


class ProjectService:
    def __init__(self, store: Store, clock: Clock, idempotency: IdempotencyStore) -> None:
        self._store = store
        self._clock = clock
        self._idempotency = idempotency

    # -- lookups used inside transactions ---------------------------------------------------
    async def _project(self, conn: AsyncConnection, project_id: str, principal: Principal) -> Any:
        row = None
        if is_id(project_id, "prj"):
            row = (
                (
                    await conn.execute(
                        select(projects).where(
                            projects.c.id == project_id, projects.c.workspace_id == principal.workspace_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise _missing("project", project_id, "projects_list")
        return row

    async def folder_row(self, conn: AsyncConnection, folder_id: str, principal: Principal) -> Any:
        """The folder, if it exists in a project of this workspace. Raises not_found otherwise."""
        row = None
        if is_id(folder_id, "fld"):
            row = (
                (
                    await conn.execute(
                        select(folders)
                        .join(projects, projects.c.id == folders.c.project_id)
                        .where(folders.c.id == folder_id, projects.c.workspace_id == principal.workspace_id)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise _missing("folder", folder_id, "folders_list")
        return row

    async def require_folder(self, folder_id: str, principal: Principal) -> None:
        async with self._store.read_tx() as conn:
            await self.folder_row(conn, folder_id, principal)

    async def place(self, conn: AsyncConnection, folder: Any, source_type: str, source_id: str, now: int) -> str:
        """Put a source into a folder; attaching it twice returns the existing placement."""
        asset_id = new_id("ast", now)
        inserted = (
            await conn.execute(
                insert_ignore(conn, placements, conflict=["folder_id", "source_type", "source_id"])
                .values(
                    id=asset_id,
                    folder_id=folder["id"],
                    project_id=folder["project_id"],
                    source_type=source_type,
                    source_id=source_id,
                    created_at=now,
                )
                .returning(placements.c.id)
            )
        ).first()
        if inserted is not None:
            return asset_id
        existing = (
            await conn.execute(
                select(placements.c.id).where(
                    placements.c.folder_id == folder["id"],
                    placements.c.source_type == source_type,
                    placements.c.source_id == source_id,
                )
            )
        ).scalar_one()
        return str(existing)

    # -- projects ---------------------------------------------------------------------------
    async def create_project(
        self, name: str, *, description: str | None, principal: Principal, idempotency_key: str | None = None
    ) -> tuple[dict[str, Any], bool]:
        name = clean_name(name, "project")
        description = (description or "").strip()[:2000] or None
        key = check_key(idempotency_key) if idempotency_key else None
        digest = request_hash({"name": name, "description": description})
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> tuple[str, bool]:
            if key is not None:
                stored = await self._idempotency.begin(conn, principal.workspace_id, "projects_create", key, digest)
                if stored is not None:
                    return stored["project_id"], True
            count = (
                await conn.execute(
                    select(func.count()).select_from(projects).where(projects.c.workspace_id == principal.workspace_id)
                )
            ).scalar_one()
            if count >= MAX_PROJECTS:
                raise ApiError(QUOTA_EXCEEDED, "too many projects", hint="Delete or reuse an existing project.")
            project_id, root_id = new_id("prj", now), new_id("fld", now)
            await conn.execute(
                projects.insert().values(
                    id=project_id,
                    workspace_id=principal.workspace_id,
                    name=name,
                    description=description,
                    root_folder_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await conn.execute(
                folders.insert().values(
                    id=root_id, project_id=project_id, parent_id=None, name=ROOT_NAME, depth=0, created_at=now
                )
            )
            await conn.execute(update(projects).where(projects.c.id == project_id).values(root_folder_id=root_id))
            if key is not None:
                await self._idempotency.finish(
                    conn, principal.workspace_id, "projects_create", key, {"project_id": project_id}
                )
            return project_id, False

        project_id, replayed = await self._store.write(unit)
        return await self.get_project(project_id, principal=principal), replayed

    async def get_project(self, project_id: str, *, principal: Principal) -> dict[str, Any]:
        async with self._store.read_tx() as conn:
            return _project_view(await self._project(conn, project_id, principal))

    async def list_projects(
        self, page: PageRequest, *, principal: Principal, include_archived: bool = False
    ) -> Page[dict[str, Any]]:
        query = select(projects).where(projects.c.workspace_id == principal.workspace_id)
        if not include_archived:
            query = query.where(projects.c.archived_at.is_(None))
        before = (page.cursor or {}).get("before")
        if isinstance(before, str):
            query = query.where(projects.c.id < before)
        async with self._store.read_tx() as conn:
            rows = (await conn.execute(query.order_by(projects.c.id.desc()).limit(page.limit + 1))).mappings().all()
        items = [_project_view(r) for r in rows[: page.limit]]
        more = len(rows) > page.limit
        cursor = encode_cursor({"before": items[-1]["project_id"]}) if more and items else None
        return Page(items=items, has_more=more, next_cursor=cursor)

    async def update_project(
        self,
        project_id: str,
        *,
        principal: Principal,
        name: str | None = None,
        description: str | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        now = self._clock.now_ms()
        values: dict[str, Any] = {"updated_at": now}
        if name is not None:
            values["name"] = clean_name(name, "project")
        if description is not None:
            values["description"] = description.strip()[:2000] or None
        if archived is not None:
            values["archived_at"] = now if archived else None
        if len(values) == 1:
            raise ApiError(INVALID_PARAMS, "nothing to change", hint="Pass name, description or archived.")

        async def unit(conn: AsyncConnection) -> None:
            await self._project(conn, project_id, principal)
            await conn.execute(update(projects).where(projects.c.id == project_id).values(**values))

        await self._store.write(unit)
        return await self.get_project(project_id, principal=principal)

    async def delete_project(self, project_id: str, *, principal: Principal) -> dict[str, int]:
        """Delete a project with its folders and placements. Jobs and media are kept."""

        async def unit(conn: AsyncConnection) -> dict[str, int]:
            await self._project(conn, project_id, principal)
            folder_ids = select(folders.c.id).where(folders.c.project_id == project_id)
            counts = {
                "folders": (
                    await conn.execute(
                        select(func.count()).select_from(folders).where(folders.c.project_id == project_id)
                    )
                ).scalar_one(),
                "placements": (
                    await conn.execute(
                        select(func.count()).select_from(placements).where(placements.c.project_id == project_id)
                    )
                ).scalar_one(),
            }
            await conn.execute(update(jobs).where(jobs.c.folder_id.in_(folder_ids)).values(folder_id=None))
            await conn.execute(delete(placements).where(placements.c.project_id == project_id))
            await conn.execute(update(projects).where(projects.c.id == project_id).values(root_folder_id=None))
            await conn.execute(delete(folders).where(folders.c.project_id == project_id))
            await conn.execute(delete(projects).where(projects.c.id == project_id))
            return counts

        return await self._store.write(unit)

    # -- folders ----------------------------------------------------------------------------
    async def list_folders(
        self, *, principal: Principal, project_id: str | None = None, parent_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Direct children of `parent_id`, or the whole tree of `project_id` (root first)."""
        async with self._store.read_tx() as conn:
            if parent_id:
                parent = await self.folder_row(conn, parent_id, principal)
                query = select(folders).where(folders.c.parent_id == parent["id"])
            elif project_id:
                project = await self._project(conn, project_id, principal)
                query = select(folders).where(folders.c.project_id == project["id"])
            else:
                raise ApiError(
                    INVALID_PARAMS, "pass project_id or parent_id", hint="Find a project with projects_list."
                )
            rows = (await conn.execute(query.order_by(folders.c.depth, folders.c.name))).mappings().all()
            counts = dict(
                (
                    await conn.execute(
                        select(placements.c.folder_id, func.count())
                        .where(placements.c.folder_id.in_([r["id"] for r in rows]))
                        .group_by(placements.c.folder_id)
                    )
                ).all()
            )
        return [{**_folder_view(r), "asset_count": counts.get(r["id"], 0)} for r in rows]

    async def create_folder(self, name: str, *, parent_id: str, principal: Principal) -> dict[str, Any]:
        name = clean_name(name, "folder")
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> Any:
            parent = await self.folder_row(conn, parent_id, principal)
            if parent["depth"] + 1 > MAX_DEPTH:
                raise ApiError(
                    INVALID_PARAMS, f"folders can be nested {MAX_DEPTH} levels deep", hint="Use a flatter layout."
                )
            total = (
                await conn.execute(
                    select(func.count()).select_from(folders).where(folders.c.project_id == parent["project_id"])
                )
            ).scalar_one()
            if total >= MAX_FOLDERS_PER_PROJECT:
                raise ApiError(QUOTA_EXCEEDED, "too many folders in this project", hint="Reuse existing folders.")
            folder_id = new_id("fld", now)
            inserted = (
                await conn.execute(
                    insert_ignore(conn, folders, conflict=["parent_id", "name"])
                    .values(
                        id=folder_id,
                        project_id=parent["project_id"],
                        parent_id=parent["id"],
                        name=name,
                        depth=parent["depth"] + 1,
                        created_at=now,
                    )
                    .returning(folders.c.id)
                )
            ).first()
            if inserted is None:
                raise ApiError(
                    CONFLICT,
                    f"a folder named {name!r} already exists there",
                    hint="Use the existing folder (folders_list) or pick another name.",
                    recovery_tool="folders_list",
                )
            return (await conn.execute(select(folders).where(folders.c.id == folder_id))).mappings().one()

        return _folder_view(await self._store.write(unit))

    async def update_folder(
        self, folder_id: str, *, principal: Principal, name: str | None = None, parent_id: str | None = None
    ) -> dict[str, Any]:
        """Rename and/or move a folder (with everything in it) inside its project."""
        new_name = clean_name(name, "folder") if name is not None else None
        if new_name is None and not parent_id:
            raise ApiError(INVALID_PARAMS, "nothing to change", hint="Pass name and/or parent_id.")

        async def unit(conn: AsyncConnection) -> Any:
            folder = await self.folder_row(conn, folder_id, principal)
            if folder["parent_id"] is None:
                raise ApiError(
                    INVALID_PARAMS,
                    "a project's root folder cannot be renamed or moved",
                    hint="Rename the project instead.",
                )
            values: dict[str, Any] = {}
            if new_name is not None:
                values["name"] = new_name
            if parent_id and parent_id != folder["parent_id"]:
                target = await self.folder_row(conn, parent_id, principal)
                if target["project_id"] != folder["project_id"]:
                    raise ApiError(
                        INVALID_PARAMS,
                        "a folder cannot move to another project",
                        hint="Attach its assets there instead.",
                    )
                subtree = await self._subtree(conn, folder["id"])
                if target["id"] in subtree:
                    raise ApiError(
                        INVALID_PARAMS,
                        "a folder cannot move into itself or its own subfolders",
                        hint="Pick another parent.",
                    )
                shift = target["depth"] + 1 - folder["depth"]
                deepest = max(subtree.values())
                if deepest + shift > MAX_DEPTH:
                    raise ApiError(
                        INVALID_PARAMS, f"folders can be nested {MAX_DEPTH} levels deep", hint="Use a flatter layout."
                    )
                values["parent_id"] = target["id"]
                if shift:
                    await conn.execute(
                        update(folders).where(folders.c.id.in_(list(subtree))).values(depth=folders.c.depth + shift)
                    )
            if values:
                clash = (
                    await conn.execute(
                        select(folders.c.id).where(
                            folders.c.parent_id == values.get("parent_id", folder["parent_id"]),
                            folders.c.name == values.get("name", folder["name"]),
                            folders.c.id != folder["id"],
                        )
                    )
                ).first()
                if clash is not None:
                    raise ApiError(CONFLICT, "a folder with that name already exists there", hint="Pick another name.")
                await conn.execute(update(folders).where(folders.c.id == folder["id"]).values(**values))
            return (await conn.execute(select(folders).where(folders.c.id == folder["id"]))).mappings().one()

        return _folder_view(await self._store.write(unit))

    @staticmethod
    async def _subtree(conn: AsyncConnection, folder_id: str) -> dict[str, int]:
        """Ids (with depth) of a folder and everything below it."""
        found: dict[str, int] = {}
        frontier = [folder_id]
        first = (await conn.execute(select(folders.c.depth).where(folders.c.id == folder_id))).scalar_one()
        found[folder_id] = first
        while frontier:
            rows = (
                await conn.execute(select(folders.c.id, folders.c.depth).where(folders.c.parent_id.in_(frontier)))
            ).all()
            frontier = [r[0] for r in rows if r[0] not in found]
            found.update({r[0]: r[1] for r in rows})
        return found

    async def delete_folder(self, folder_id: str, *, principal: Principal) -> dict[str, int]:
        """Delete a folder, its subfolders and their placements. Jobs and media are kept."""

        async def unit(conn: AsyncConnection) -> dict[str, int]:
            folder = await self.folder_row(conn, folder_id, principal)
            if folder["parent_id"] is None:
                raise ApiError(
                    INVALID_PARAMS,
                    "the root folder is removed by deleting its project",
                    hint="Use projects_delete.",
                    recovery_tool="projects_delete",
                )
            ids = list(await self._subtree(conn, folder["id"]))
            removed = (
                await conn.execute(select(func.count()).select_from(placements).where(placements.c.folder_id.in_(ids)))
            ).scalar_one()
            await conn.execute(update(jobs).where(jobs.c.folder_id.in_(ids)).values(folder_id=None))
            await conn.execute(delete(placements).where(placements.c.folder_id.in_(ids)))
            await conn.execute(delete(folders).where(folders.c.id == folder["id"]))  # children cascade
            return {"folders": len(ids), "placements": removed}

        return await self._store.write(unit)

    # -- assets (placements) ----------------------------------------------------------------
    async def list_assets(self, folder_id: str, page: PageRequest, *, principal: Principal) -> Page[Placement]:
        before = (page.cursor or {}).get("before")
        async with self._store.read_tx() as conn:
            folder = await self.folder_row(conn, folder_id, principal)
            query = select(placements).where(placements.c.folder_id == folder["id"])
            if isinstance(before, str):
                query = query.where(placements.c.id < before)
            rows = (await conn.execute(query.order_by(placements.c.id.desc()).limit(page.limit + 1))).mappings().all()
        items = [
            Placement(r["id"], r["folder_id"], r["source_type"], r["source_id"], r["created_at"])
            for r in rows[: page.limit]
        ]
        more = len(rows) > page.limit
        return Page(
            items=items,
            has_more=more,
            next_cursor=encode_cursor({"before": items[-1].asset_id}) if more and items else None,
        )

    async def attach(self, folder_id: str, source_ids: list[str], *, principal: Principal) -> list[dict[str, Any]]:
        """Place jobs or media into a folder. Per id: attached, already_there, or not_found."""
        if not source_ids or len(source_ids) > MAX_ATTACH:
            raise ApiError(INVALID_PARAMS, f"pass 1 to {MAX_ATTACH} source ids", hint="Attach larger sets in groups.")
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> list[dict[str, Any]]:
            folder = await self.folder_row(conn, folder_id, principal)
            results: list[dict[str, Any]] = []
            for source_id in source_ids:
                prefix = id_prefix(source_id)
                table, kind = (jobs, "job") if prefix == "job" else (media, "media") if prefix == "med" else (None, "")
                exists = (
                    table is not None
                    and (
                        await conn.execute(
                            select(table.c.id).where(
                                table.c.id == source_id, table.c.workspace_id == principal.workspace_id
                            )
                        )
                    ).first()
                )
                if not exists:
                    results.append({"source_id": str(source_id), "status": "not_found"})
                    continue
                before_count = (
                    await conn.execute(
                        select(func.count())
                        .select_from(placements)
                        .where(placements.c.folder_id == folder["id"], placements.c.source_id == source_id)
                    )
                ).scalar_one()
                asset_id = await self.place(conn, folder, kind, source_id, now)
                results.append(
                    {
                        "source_id": source_id,
                        "asset_id": asset_id,
                        "status": "already_there" if before_count else "attached",
                    }
                )
            return results

        return await self._store.write(unit)

    async def move_assets(self, asset_ids: list[str], folder_id: str, *, principal: Principal) -> list[dict[str, Any]]:
        if not asset_ids or len(asset_ids) > MAX_ATTACH:
            raise ApiError(INVALID_PARAMS, f"pass 1 to {MAX_ATTACH} asset ids", hint="Move larger sets in groups.")
        now = self._clock.now_ms()

        async def unit(conn: AsyncConnection) -> list[dict[str, Any]]:
            target = await self.folder_row(conn, folder_id, principal)
            results: list[dict[str, Any]] = []
            for asset_id in asset_ids:
                row = await self._placement(conn, asset_id, principal)
                if row is None:
                    results.append({"asset_id": str(asset_id), "status": "not_found"})
                    continue
                if row["folder_id"] == target["id"]:
                    results.append({"asset_id": asset_id, "status": "already_there"})
                    continue
                await conn.execute(delete(placements).where(placements.c.id == asset_id))
                new_asset = await self.place(conn, target, row["source_type"], row["source_id"], now)
                results.append({"asset_id": new_asset, "status": "moved", "source_id": row["source_id"]})
            return results

        return await self._store.write(unit)

    async def remove_assets(self, asset_ids: list[str], *, principal: Principal) -> list[dict[str, Any]]:
        """Remove placements. The jobs and files they point at are kept."""
        if not asset_ids or len(asset_ids) > MAX_ATTACH:
            raise ApiError(INVALID_PARAMS, f"pass 1 to {MAX_ATTACH} asset ids", hint="Remove larger sets in groups.")

        async def unit(conn: AsyncConnection) -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for asset_id in asset_ids:
                row = await self._placement(conn, asset_id, principal)
                if row is None:
                    results.append({"asset_id": str(asset_id), "status": "not_found"})
                    continue
                await conn.execute(delete(placements).where(placements.c.id == asset_id))
                if row["source_type"] == "job":
                    await conn.execute(
                        update(jobs)
                        .where(jobs.c.id == row["source_id"], jobs.c.folder_id == row["folder_id"])
                        .values(folder_id=None)
                    )
                results.append({"asset_id": asset_id, "status": "removed", "source_id": row["source_id"]})
            return results

        return await self._store.write(unit)

    @staticmethod
    async def _placement(conn: AsyncConnection, asset_id: str, principal: Principal) -> Any:
        if not is_id(asset_id, "ast"):
            return None
        return (
            (
                await conn.execute(
                    select(placements)
                    .join(projects, projects.c.id == placements.c.project_id)
                    .where(placements.c.id == asset_id, projects.c.workspace_id == principal.workspace_id)
                )
            )
            .mappings()
            .first()
        )
