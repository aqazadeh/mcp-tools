"""Helpers for tool arguments.

Types at the SDK boundary are deliberately permissive: the SDK reports its own validation
failures as raw pydantic text outside our JSON error contract. So enumerations are
published in the JSON Schema (for the model's benefit) but checked here, where a failure
becomes an `ApiError` with the allowed values and a hint.
"""

from collections.abc import Sequence
from typing import Any

from pydantic import Field
from pydantic.fields import FieldInfo

from mcp_tools.core.errors import INVALID_PARAMS, ApiError


def choice(description: str, options: Sequence[str]) -> FieldInfo:
    """A string field whose allowed values appear in the schema as an enum."""
    return Field(description=description, json_schema_extra={"enum": list(options)})


def require_choice(name: str, value: str, options: Sequence[str], *, tool: str | None = None) -> str:
    if value not in options:
        raise ApiError(
            INVALID_PARAMS,
            f"{name} must be one of the allowed values, got {value!r}",
            hint=f"Use one of: {', '.join(repr(o) for o in options)}.",
            recovery_tool=tool,
            allowed=list(options),
        )
    return value


def optional_choice(name: str, value: str, options: Sequence[str]) -> str | None:
    """Like `require_choice`, but an empty string means "not set"."""
    if value == "":
        return None
    return require_choice(name, value, options)


def json_object(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(INVALID_PARAMS, f"{name} must be an object", hint=f"Pass {name} as a JSON object.")
    return value
