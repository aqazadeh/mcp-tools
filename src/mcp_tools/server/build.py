"""Compose the MCP server.

`build_server()` is a factory: every call returns a fresh `MCPServer` and `Platform`.
There are no module-level singletons because the lifespan is entered once per transport
run and once per in-process test client, and a session manager can only run once.

Order matters (mcp 2.3.0): extensions are consumed when `MCPServer` is constructed, so UI
tools and their HTML must already be registered on `Apps` at that point.
"""

import logging
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server import MCPServer
from mcp.server.apps import Apps

from mcp_tools import __version__
from mcp_tools.config import Settings
from mcp_tools.core.platform import Platform
from mcp_tools.server.envelope import guard
from mcp_tools.server.instructions import build_instructions
from mcp_tools.server.registry import Capabilities, ToolDef, collect, validate_defs

SERVER_NAME = "mcp-tools"
LEGACY_UI_META_KEY = "ui/resourceUri"  # older hosts read the flat key; the SDK writes only the nested one

_NOISY_LOGGERS = ("httpx2", "httpcore2", "httpx", "httpcore")


@dataclass(frozen=True)
class Built:
    mcp: MCPServer[Platform]
    platform: Platform
    tools: tuple[ToolDef, ...]
    capabilities: Capabilities


def configure_logging(level: str) -> None:
    """Log to stderr only: on stdio, stdout is the protocol channel."""
    root = logging.getLogger()
    if not any(getattr(h, "_mcp_tools", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler._mcp_tools = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level.upper())
    for name in _NOISY_LOGGERS:  # these log full request URLs (signed URLs included) at INFO
        logging.getLogger(name).setLevel(logging.WARNING)


def _tool_meta(d: ToolDef) -> dict[str, object] | None:
    meta: dict[str, object] = dict(d.meta)
    if d.ui_uri is None:
        meta["ui"] = {"visibility": list(d.visibility)}
    else:
        meta[LEGACY_UI_META_KEY] = d.ui_uri
    return meta or None


def build_server(
    settings: Settings | None = None,
    *,
    capabilities: Capabilities | None = None,
    extra_tools: Sequence[ToolDef] = (),
) -> Built:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    caps = capabilities or Capabilities(ui=settings.ui_enabled)
    platform = Platform(settings)

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Platform]) -> AsyncIterator[Platform]:
        async with platform.running():
            yield platform

    families = settings.enabled_families
    defs = [*collect(families, caps), *extra_tools]
    validate_defs(defs)

    apps = Apps()
    ui_defs = [d for d in defs if d.ui_uri is not None and caps.ui]
    for d in ui_defs:
        apps.tool(
            resource_uri=d.ui_uri,  # type: ignore[arg-type]
            visibility=d.visibility,
            meta=_tool_meta(d),
            name=d.name,
            title=d.title,
            description=d.description.strip(),
            annotations=d.annotations,
            structured_output=True,
        )(guard(d))

    mcp: MCPServer[Platform] = MCPServer(
        SERVER_NAME,
        title="mcp-tools · media studio",
        version=__version__,
        instructions=build_instructions(families),
        extensions=[apps],
        lifespan=lifespan,
        log_level=settings.log_level.upper(),  # type: ignore[arg-type]
    )
    ui_names = {d.name for d in ui_defs}
    for d in defs:
        if d.name in ui_names:
            continue
        mcp.add_tool(
            guard(d),
            name=d.name,
            title=d.title,
            description=d.description.strip(),
            annotations=d.annotations,
            meta=_tool_meta(d) if d.ui_uri is None else dict(d.meta) or None,
            structured_output=True,
        )
    return Built(mcp=mcp, platform=platform, tools=tuple(defs), capabilities=caps)
