"""Helpers for tool arguments.

Types at the SDK boundary are deliberately permissive: the SDK reports its own validation
failures as raw pydantic text outside our JSON error contract. So enumerations are
published in the JSON Schema (for the model's benefit) but checked here, where a failure
becomes an `ApiError` with the allowed values and a hint.
"""

import contextlib
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

from mcp_tools.core.errors import INVALID_PARAMS, ApiError


def _slim(node: Any) -> None:
    """Shrink a published schema: drop titles and write optional fields without a null variant.

    Results are sent without nulls, and tools/list is read by a model on every session, so the
    `anyOf: [X, null]` + `default: null` + `title` that pydantic emits per field is pure overhead.
    """
    if isinstance(node, list):
        for item in node:
            _slim(item)
        return
    if not isinstance(node, dict):
        return
    if isinstance(node.get("title"), str):
        del node["title"]
    variants = node.get("anyOf")
    if isinstance(variants, list) and len(variants) == 2 and node.get("default") is None:
        rest = [v for v in variants if v != {"type": "null"}]
        if len(rest) == 1 and isinstance(rest[0], dict):
            del node["anyOf"]
            node.pop("default", None)
            node.update(rest[0])
    for key, value in node.items():
        if key in ("properties", "$defs") and isinstance(value, dict):
            for child in value.values():
                _slim(child)
        else:
            _slim(value)


def _slim_schema(schema: dict[str, Any], _model: type[BaseModel]) -> None:
    _slim(schema)


class Wire(BaseModel):
    """Base for models whose JSON Schema is published in tools/list."""

    model_config = ConfigDict(json_schema_extra=_slim_schema)


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


class MediaRef(Wire):
    """A reference file for a generation."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_slim_schema)

    role: str = Field(
        default="",
        description="A role from the model's medias[]; may be omitted when the model has exactly one.",
    )
    value: str = Field(description="A media id (med_...) or the job id (job_...) of a finished job. Never a URL.")


class GenerateParams(Wire):
    """Common request fields. Model-specific parameters are passed alongside them."""

    model_config = ConfigDict(extra="allow", json_schema_extra=_slim_schema)

    model: str = Field(description="Model id from models_explore.")
    prompt: str = Field(default="", description="What to generate.")
    aspect_ratio: str = Field(default="", description="One of the model's aspect_ratios, e.g. '16:9'.")
    count: int = Field(default=1, description="1-4 variants of this same request; each is a separate job.")
    duration: float | None = Field(default=None, description="Seconds, for models that have a duration.")
    medias: list[MediaRef] = Field(default_factory=list, description="Reference media, by id.")
    folder_id: str = Field(default="", description="Destination folder (fld_...) of a project; optional.")

    @model_validator(mode="before")
    @classmethod
    def _decode_stringified(cls, data: Any) -> Any:
        """Some clients send nested arrays as JSON strings; repair that instead of failing."""
        if isinstance(data, dict) and isinstance(data.get("medias"), str):
            with contextlib.suppress(ValueError):
                data = {**data, "medias": json.loads(data["medias"])}
        return data

    def raw(self) -> dict[str, Any]:
        return self.model_dump()


class NextStep(Wire):
    tool: str
    after_seconds: int | None = None
    hint: str | None = None
