"""Declarative tool registry.

The SDK consumes extensions when `MCPServer` is constructed, silently ignores a second
tool with the same name, and only warns about bad names. So tools are described as
`ToolDef` values, validated here (violations are errors), and registered by
`server/build.py` in the right order.
"""

import importlib
import inspect
import re
import types
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Union

from mcp_types import CallToolResult, ToolAnnotations
from pydantic import BaseModel

Handler = Callable[..., Awaitable[CallToolResult]]
Visibility = Literal["model", "app"]
Effect = Literal["write", "spend", "filesystem", "network"]

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
MAX_DESCRIPTION_CHARS = 600
CONTEXT_PARAM = "ctx"

FAMILY_MODULES: dict[str, str] = {
    "catalog": "mcp_tools.server.families.catalog",
    "generate": "mcp_tools.server.families.generate",
    "jobs": "mcp_tools.server.families.jobs",
    "media": "mcp_tools.server.families.media",
    "billing": "mcp_tools.server.families.billing",
}


class RegistryError(Exception):
    """A tool definition breaks a registry rule. Raised while building the server."""


@dataclass(frozen=True)
class Capabilities:
    """What this server process can do; tools needing a missing capability are not registered."""

    url_issuer: bool = False  # can mint signed upload/download URLs (HTTP mode)
    local_paths: bool = False  # may read files from allowed local roots
    ui: bool = False  # bind widgets

    def has(self, names: frozenset[str]) -> bool:
        return all(getattr(self, name, False) for name in names)


@dataclass(frozen=True)
class ToolDef:
    name: str
    handler: Handler
    description: str
    family: str
    title: str | None = None
    annotations: ToolAnnotations | None = None
    visibility: tuple[Visibility, ...] = ("model",)
    ui_uri: str | None = None
    effects: frozenset[Effect] = frozenset()
    requires: frozenset[str] = frozenset()
    app_helper: bool = False  # app-only helper bound to a server-minted session (e.g. upload chunks)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def arg_names(self) -> frozenset[str]:
        return frozenset(name for name in inspect.signature(self.handler).parameters if name != CONTEXT_PARAM)

    @property
    def result_model(self) -> type[BaseModel]:
        return _result_model(self.handler)


def _result_model(handler: Handler) -> type[BaseModel]:
    try:
        hints = typing.get_type_hints(handler, include_extras=True)
    except Exception as exc:
        raise RegistryError(f"{handler.__name__}: cannot resolve type annotations: {exc}") from exc
    ret = hints.get("return")
    if typing.get_origin(ret) is not Annotated:
        raise RegistryError(f"{handler.__name__}: return type must be Annotated[CallToolResult, <BaseModel>]")
    base, *extras = typing.get_args(ret)
    model = extras[0] if extras else None
    if base is not CallToolResult or not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise RegistryError(f"{handler.__name__}: return type must be Annotated[CallToolResult, <BaseModel>]")
    return model


def _allows_none(annotation: Any) -> bool:
    if annotation is None or annotation is type(None):
        return True
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _allows_none(typing.get_args(annotation)[0])
    if origin is Union or origin is types.UnionType:
        return any(_allows_none(arg) for arg in typing.get_args(annotation))
    return False


def _check_result_model(tool: str, model: type[BaseModel]) -> None:
    """Results are sent without nulls, so a nullable field must not be required."""
    for name, info in model.model_fields.items():
        if info.alias is not None and info.alias != name:
            raise RegistryError(f"{tool}: result field {name!r} uses an alias; results are dumped by field name")
        if _allows_none(info.annotation) and info.is_required():
            raise RegistryError(f"{tool}: nullable result field {name!r} needs a default")


def validate_defs(defs: Sequence[ToolDef]) -> None:
    seen: set[str] = set()
    for d in defs:
        if d.name in seen:
            raise RegistryError(f"duplicate tool name {d.name!r}")
        seen.add(d.name)
        if not NAME_PATTERN.match(d.name):
            raise RegistryError(f"{d.name!r}: tool names must match {NAME_PATTERN.pattern}")
        text = d.description.strip()
        if not text or len(text) > MAX_DESCRIPTION_CHARS:
            raise RegistryError(f"{d.name}: description must be 1..{MAX_DESCRIPTION_CHARS} characters, got {len(text)}")
        handler = d.handler
        if not inspect.iscoroutinefunction(handler) or not inspect.isfunction(handler):
            raise RegistryError(f"{d.name}: handler must be a plain `async def` function")
        params = inspect.signature(handler).parameters
        if CONTEXT_PARAM not in params:
            raise RegistryError(f"{d.name}: handler must take a `{CONTEXT_PARAM}: Context` parameter")
        for pname, param in params.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise RegistryError(f"{d.name}: *args / **kwargs are not allowed")
            if pname.startswith("_"):
                raise RegistryError(f"{d.name}: parameter {pname!r} must not start with an underscore")
            if param.annotation is inspect.Parameter.empty:
                raise RegistryError(f"{d.name}: parameter {pname!r} needs a type annotation")
        _check_result_model(d.name, _result_model(handler))
        if not d.visibility or not set(d.visibility) <= {"model", "app"}:
            raise RegistryError(f"{d.name}: visibility must be a non-empty subset of ('model', 'app')")
        if "app" in d.visibility and d.effects and not d.app_helper:
            raise RegistryError(f"{d.name}: tools with effects {sorted(d.effects)} must not be callable from widgets")
        if d.app_helper and d.visibility != ("app",):
            raise RegistryError(f"{d.name}: app helpers must have visibility ('app',)")
        if d.ui_uri is not None and not d.ui_uri.startswith("ui://"):
            raise RegistryError(f"{d.name}: ui_uri must use the ui:// scheme")


def collect(families: Sequence[str], capabilities: Capabilities) -> list[ToolDef]:
    """Gather the tool definitions of the enabled families that this process can serve."""
    defs: list[ToolDef] = []
    for family in families:
        module_name = FAMILY_MODULES.get(family)
        if module_name is None:
            continue  # family not implemented yet
        module = importlib.import_module(module_name)
        for d in module.tools():
            if d.family != family:
                raise RegistryError(f"{d.name}: declared family {d.family!r} but lives in {family!r}")
            if capabilities.has(d.requires) and (d.ui_uri is None or capabilities.ui or "model" in d.visibility):
                defs.append(d)
    return defs
