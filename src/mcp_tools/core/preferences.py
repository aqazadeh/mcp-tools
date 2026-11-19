"""Workspace preferences: a few named defaults the model can read and change.

The set of keys is fixed in code. Preferences are advice for the calling model (which model
to reach for, which aspect ratio, which folder); they never alter budgets or limits, which
live in the server configuration and are out of reach of any tool.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.catalog.loader import Catalog
from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.core.principal import Principal
from mcp_tools.core.projects.service import ProjectService
from mcp_tools.db.schema import preferences
from mcp_tools.db.store import Store

MODEL_KEYS = {"default_image_model": "image", "default_video_model": "video", "default_audio_model": "audio"}
KEYS = (*MODEL_KEYS, "default_aspect_ratio", "default_folder_id", "style_notes")
MAX_NOTES = 600


class PreferenceService:
    def __init__(self, store: Store, catalog: Catalog, projects: ProjectService) -> None:
        self._store = store
        self._catalog = catalog
        self._projects = projects

    async def get(self, *, principal: Principal) -> dict[str, Any]:
        async with self._store.read_tx() as conn:
            rows = (
                await conn.execute(
                    select(preferences.c.key, preferences.c.value_json).where(
                        preferences.c.workspace_id == principal.workspace_id
                    )
                )
            ).all()
        return {key: value for key, value in rows if key in KEYS}

    def _check(self, key: str, value: Any) -> Any:
        if key in MODEL_KEYS:
            model = self._catalog.get(str(value))  # raises unknown_model with a hint
            if model.output_type != MODEL_KEYS[key] or model.task != "generate":
                raise ApiError(
                    INVALID_PARAMS,
                    f"{key} needs a {MODEL_KEYS[key]} generation model, {model.id} is not one",
                    hint="Pick a model with models_explore.",
                    recovery_tool="models_explore",
                )
            return model.id
        if not isinstance(value, str) or not value.strip():
            raise ApiError(INVALID_PARAMS, f"{key} must be a non-empty string", hint="Pass null to clear it.")
        value = " ".join(value.split())
        if key == "default_aspect_ratio":
            parts = value.split(":")
            if len(parts) != 2 or not all(p.isascii() and p.isdigit() and 0 < int(p) < 100 for p in parts):
                raise ApiError(INVALID_PARAMS, "default_aspect_ratio must look like 16:9", hint="Use W:H.")
        if key == "style_notes" and len(value) > MAX_NOTES:
            raise ApiError(INVALID_PARAMS, f"style_notes is limited to {MAX_NOTES} characters", hint="Shorten it.")
        return value

    async def update(self, patch: dict[str, Any], *, principal: Principal) -> dict[str, Any]:
        """Set the given keys; a null value clears a key. All or nothing."""
        if not isinstance(patch, dict) or not patch:
            raise ApiError(INVALID_PARAMS, "patch must be a non-empty object", hint="Pass the keys to change.")
        unknown = sorted(set(patch) - set(KEYS))
        if unknown:
            raise ApiError(
                INVALID_PARAMS,
                f"unknown preference key(s): {', '.join(unknown)}",
                hint="Only the listed keys exist; limits and budgets are not preferences.",
                allowed=list(KEYS),
            )
        cleaned = {key: (None if value is None else self._check(key, value)) for key, value in patch.items()}

        async def unit(conn: AsyncConnection) -> None:
            folder = cleaned.get("default_folder_id")
            if folder is not None:
                await self._projects.folder_row(conn, folder, principal)
            for key, value in cleaned.items():
                await conn.execute(
                    delete(preferences).where(
                        preferences.c.workspace_id == principal.workspace_id, preferences.c.key == key
                    )
                )
                if value is not None:
                    await conn.execute(
                        preferences.insert().values(workspace_id=principal.workspace_id, key=key, value_json=value)
                    )

        await self._store.write(unit)
        return await self.get(principal=principal)
