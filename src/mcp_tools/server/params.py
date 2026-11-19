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


class MediaRef(BaseModel):
    """A reference file for a generation."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(
        default="",
        description="A role from the model's medias[]; may be omitted when the model has exactly one.",
    )
    value: str = Field(description="A media id (med_...) or the job id (job_...) of a finished job. Never a URL.")


class GenerateParams(BaseModel):
    """Common request fields. Model-specific parameters are passed alongside them."""

    model_config = ConfigDict(extra="allow")

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


class NextStep(BaseModel):
    tool: str
    after_seconds: int | None = None
    hint: str | None = None
