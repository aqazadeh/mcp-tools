from typing import Annotated

import pytest
from mcp.server.mcpserver import Context
from mcp_types import CallToolResult
from pydantic import BaseModel

from mcp_tools.config import ALL_FAMILIES, Settings
from mcp_tools.server.envelope import ok
from mcp_tools.server.registry import Capabilities, RegistryError, ToolDef, collect, validate_defs


class Res(BaseModel):
    value: int = 0
    note: str | None = None


class BadRes(BaseModel):
    note: str | None  # nullable but required


async def good(ctx: Context, a: int = 0) -> Annotated[CallToolResult, Res]:
    return ok(Res(value=a))


def _def(**overrides: object) -> ToolDef:
    base: dict[str, object] = {
        "name": "good_tool",
        "handler": good,
        "description": "Does a thing.",
        "family": "x",
    }
    base.update(overrides)
    return ToolDef(**base)  # type: ignore[arg-type]


def test_valid_definition_passes_and_reports_arguments() -> None:
    d = _def()
    validate_defs([d])
    assert d.arg_names == frozenset({"a"})
    assert d.result_model is Res


def test_duplicate_names_are_an_error() -> None:
    with pytest.raises(RegistryError, match="duplicate"):
        validate_defs([_def(), _def()])


@pytest.mark.parametrize("name", ["Bad", "1abc", "has-dash", "x" * 49, "dotted.name", ""])
def test_names_must_match_the_pattern(name: str) -> None:
    with pytest.raises(RegistryError, match="tool names must match"):
        validate_defs([_def(name=name)])


@pytest.mark.parametrize("description", ["", "   ", "x" * 601])
def test_description_length_is_enforced(description: str) -> None:
    with pytest.raises(RegistryError, match="description"):
        validate_defs([_def(description=description)])


def test_handler_must_be_async() -> None:
    def sync(ctx: Context) -> Annotated[CallToolResult, Res]:
        return ok(Res())

    with pytest.raises(RegistryError, match="async def"):
        validate_defs([_def(handler=sync)])


def test_handler_needs_ctx_and_no_var_keyword() -> None:
    async def no_ctx(a: int = 0) -> Annotated[CallToolResult, Res]:
        return ok(Res())

    async def kwargs(ctx: Context, **extra: int) -> Annotated[CallToolResult, Res]:
        return ok(Res())

    with pytest.raises(RegistryError, match="ctx"):
        validate_defs([_def(handler=no_ctx)])
    with pytest.raises(RegistryError, match="kwargs"):
        validate_defs([_def(handler=kwargs)])


def test_return_type_must_be_annotated_calltoolresult() -> None:
    async def plain(ctx: Context) -> Res:
        return Res()

    with pytest.raises(RegistryError, match="Annotated"):
        validate_defs([_def(handler=plain)])  # type: ignore[arg-type]


def test_nullable_result_fields_need_defaults() -> None:
    async def bad(ctx: Context) -> Annotated[CallToolResult, BadRes]:
        return ok(BadRes(note=None))

    with pytest.raises(RegistryError, match="needs a default"):
        validate_defs([_def(handler=bad)])


def test_tools_with_effects_cannot_be_called_from_widgets() -> None:
    with pytest.raises(RegistryError, match="must not be callable from widgets"):
        validate_defs([_def(visibility=("model", "app"), effects=frozenset({"spend"}))])
    # read-only tools may be opened to widgets; app helpers are the explicit exception
    validate_defs([_def(visibility=("model", "app"))])
    validate_defs([_def(visibility=("app",), effects=frozenset({"write"}), app_helper=True)])
    with pytest.raises(RegistryError, match="app helpers"):
        validate_defs([_def(visibility=("model", "app"), effects=frozenset({"write"}), app_helper=True)])


def test_ui_uri_scheme_is_checked() -> None:
    with pytest.raises(RegistryError, match="ui://"):
        validate_defs([_def(ui_uri="https://example.test/x.html")])


def test_collect_returns_valid_definitions_for_every_family() -> None:
    defs = collect(ALL_FAMILIES, Capabilities(url_issuer=True, local_paths=True, ui=True))
    validate_defs(defs)
    assert "models_explore" in {d.name for d in defs}


def test_unknown_family_names_are_rejected_in_settings() -> None:
    with pytest.raises(ValueError, match="unknown tool families"):
        _ = Settings(families="catalog,nope").enabled_families
    assert Settings(families=" catalog ").enabled_families == ("catalog",)
