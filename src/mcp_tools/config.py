"""Runtime configuration. Every value can be set through a `MCP_TOOLS_*` environment variable."""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ALL_FAMILIES = (
    "catalog",
    "generate",
    "jobs",
    "media",
    "billing",
    "transforms",
    "projects",
    "workflows",
    "appreg",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_TOOLS_", extra="ignore")

    data_dir: Path = Field(
        default=Path("~/.mcp-tools"), description="Where the database, blobs and keys live."
    )
    families: str = Field(
        default="", description="Comma-separated tool families to enable; empty enables all."
    )
    ui_enabled: bool = Field(
        default=True, description="Bind UI widgets (MCP Apps) to the tools that have one."
    )
    catalog_dirs: str = Field(
        default="", description=f"Extra catalog directories, separated by {os.pathsep!r}."
    )
    log_level: str = Field(default="INFO")

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def enabled_families(self) -> tuple[str, ...]:
        names = [name.strip() for name in self.families.split(",") if name.strip()]
        if not names:
            return ALL_FAMILIES
        unknown = sorted(set(names) - set(ALL_FAMILIES))
        if unknown:
            raise ValueError(f"unknown tool families: {unknown}; known: {list(ALL_FAMILIES)}")
        return tuple(name for name in ALL_FAMILIES if name in names)

    @property
    def catalog_paths(self) -> tuple[Path, ...]:
        return tuple(Path(p).expanduser() for p in self.catalog_dirs.split(os.pathsep) if p.strip())
