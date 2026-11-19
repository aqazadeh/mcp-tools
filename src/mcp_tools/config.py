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

    data_dir: Path = Field(default=Path("~/.mcp-tools"), description="Where the database, blobs and keys live.")
    families: str = Field(default="", description="Comma-separated tool families to enable; empty enables all.")
    ui_enabled: bool = Field(default=True, description="Bind UI widgets (MCP Apps) to the tools that have one.")
    catalog_dirs: str = Field(default="", description=f"Extra catalog directories, separated by {os.pathsep!r}.")
    workflow_dirs: str = Field(
        default="",
        description=f"Folders with your own workflow bundles, separated by {os.pathsep!r}. Each must belong to "
        "you and not be writable by others; their workflows appear as user/<name>.",
    )
    log_level: str = Field(default="INFO")

    job_concurrency: int = Field(default=4, ge=1, le=32, description="Jobs advanced in parallel.")
    shutdown_grace_seconds: float = Field(default=1.5, ge=0, description="How long stop waits for the runner.")
    lease_ttl_ms: int = Field(default=30_000, ge=200, description="How long a job lease lasts without a heartbeat.")
    mock_delay_ms: int | None = Field(
        default=None, ge=0, description="Override how long mock jobs take (tests and demos)."
    )

    local_imports: bool = Field(default=True, description="Offer media_import_path (reads allowed local folders).")
    import_roots: str = Field(
        default="",
        description=f"Extra folders media_import_path may read, separated by {os.pathsep!r}. "
        "The data directory's inbox is always allowed.",
    )
    egress: str = Field(
        default="auto",
        description="How URLs are fetched: direct (connections pinned to vetted addresses), proxy (through the "
        "environment's proxy; weaker checks), off, or auto (proxy if the environment sets one).",
    )

    # HTTP transport
    http_host: str = Field(default="127.0.0.1", description="Address the HTTP server binds to.")
    http_port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: str = Field(
        default="", description="URL clients reach this server at (https://host[:port]); used in signed file links."
    )
    http_allowed_hosts: str = Field(default="", description="Extra accepted Host header values, comma-separated.")
    http_allowed_origins: str = Field(default="", description="Accepted browser Origin values, comma-separated.")
    api_key: str = Field(default="", description="API key for HTTP; if empty one is generated and stored hashed.")

    # Budget (estimates; 1 credit = 0.001 USD). These are the hard limits: nothing a tool
    # caller passes can raise them.
    initial_credits: int = Field(default=10_000, ge=0, description="Opening balance of a new data directory.")
    daily_cap_credits: int = Field(default=10_000, ge=0, description="Hard cap on credits spent per UTC day.")
    max_credits_per_call: int = Field(default=5_000, ge=0, description="Hard ceiling for one tool call.")
    auto_approve_credits: int = Field(
        default=500, ge=0, description="Calls estimated above this need an explicit max_credits."
    )

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
    def workflow_paths(self) -> tuple[Path, ...]:
        return tuple(Path(p).expanduser() for p in self.workflow_dirs.split(os.pathsep) if p.strip())

    @property
    def import_root_paths(self) -> tuple[Path, ...]:
        return tuple(Path(p).expanduser() for p in self.import_roots.split(os.pathsep) if p.strip())

    @property
    def catalog_paths(self) -> tuple[Path, ...]:
        return tuple(Path(p).expanduser() for p in self.catalog_dirs.split(os.pathsep) if p.strip())
