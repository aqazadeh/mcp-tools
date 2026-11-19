"""Model catalog data model.

The catalog is data, not code: every model a tool can address is described by a
`ModelSpec` loaded from YAML. The *public* view mirrors the catalog item returned by
Higgsfield's `models_explore` (id, name, provider_name, description, output_type,
parameters, medias, aspect_ratios, tags). Fields marked private never leave the server.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OutputType = Literal["image", "video", "audio"]
MediaKind = Literal["image", "video", "audio"]
Task = Literal["generate", "upscale", "remove_background"]
ParamType = Literal["string", "integer", "number", "boolean"]

# Request fields every model shares; a model-specific parameter may not reuse these names
# unless it is the catalog's own description of that field (e.g. a `duration` ParamSpec).
COMMON_FIELDS = frozenset({"model", "prompt", "aspect_ratio", "count", "duration", "medias", "folder_id"})


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParamSpec(_Spec):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType
    description: str = ""
    required: bool = False
    default: Any = None
    options: list[Any] | None = None
    min: float | None = None
    max: float | None = None
    # private: name of the field on the provider side (defaults to `name`)
    provider_key: str | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.description:
            out["description"] = self.description
        if self.default is not None:
            out["default"] = self.default
        if self.options is not None:
            out["options"] = self.options
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        return out


class MediaRoleSpec(_Spec):
    role: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kinds: list[MediaKind] = Field(min_length=1)
    max: int = Field(default=1, ge=1, le=30)
    required: bool = False
    description: str = ""
    # private: provider input field and whether it takes a list
    provider_field: str | None = None
    as_list: bool = False

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "kinds": list(self.kinds), "max": self.max}
        if self.required:
            out["required"] = True
        if self.description:
            out["description"] = self.description
        return out


class PricingRule(_Spec):
    """Estimated price in credits (1 credit = 0.001 USD)."""

    unit: Literal["per_output", "per_second", "per_1k_chars"]
    credits: int = Field(ge=0)
    # parameter name -> option value (as string) -> multiplier
    multipliers: dict[str, dict[str, float]] = Field(default_factory=dict)
    minimum: int = Field(default=1, ge=0)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"unit": self.unit, "credits": self.credits}
        if self.multipliers:
            out["multipliers"] = self.multipliers
        return out


class ModelSpec(_Spec):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$", max_length=64)
    name: str
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    provider_name: str
    description: str
    output_type: OutputType
    task: Task = "generate"
    prompt: Literal["required", "optional", "none"] = "required"
    parameters: list[ParamSpec] = Field(default_factory=list)
    medias: list[MediaRoleSpec] = Field(default_factory=list)
    aspect_ratios: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    pricing: PricingRule
    # --- private -------------------------------------------------------------------------
    provider_ref: str = ""
    execution: Literal["async", "sync"] = "async"
    common_map: dict[str, str] = Field(default_factory=dict)
    output_hosts: list[str] = Field(default_factory=list)
    deadline_seconds: int = Field(default=900, ge=10, le=7200)
    verified_at: str | None = None
    hidden: bool = False

    @model_validator(mode="after")
    def _check(self) -> "ModelSpec":
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"model {self.id}: duplicate parameter names")
        clash = (set(names) & COMMON_FIELDS) - {"duration"}
        if clash:
            raise ValueError(f"model {self.id}: parameters shadow common fields: {sorted(clash)}")
        roles = [m.role for m in self.medias]
        if len(roles) != len(set(roles)):
            raise ValueError(f"model {self.id}: duplicate media roles")
        for name in self.pricing.multipliers:
            if name not in names and name != "aspect_ratio":
                raise ValueError(f"model {self.id}: pricing multiplier for unknown parameter {name!r}")
        return self

    @property
    def accepts_media(self) -> bool:
        return bool(self.medias)

    def param(self, name: str) -> ParamSpec | None:
        return next((p for p in self.parameters if p.name == name), None)

    def media_role(self, role: str) -> MediaRoleSpec | None:
        return next((m for m in self.medias if m.role == role), None)

    def summary(self, *, available: bool, reason: str | None) -> dict[str, Any]:
        """Compact public view for list / search / recommend."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "provider_name": self.provider_name,
            "description": self.description,
            "output_type": self.output_type,
            "input": "media" if self.accepts_media else "text",
            "price": f"{self.pricing.credits} credits {self.pricing.unit.replace('_', ' ')}",
            "available": available,
        }
        if self.task != "generate":
            out["task"] = self.task
        if self.aspect_ratios:
            out["aspect_ratios"] = list(self.aspect_ratios)
        if self.tags:
            out["tags"] = list(self.tags)
        if not available and reason:
            out["unavailable_reason"] = reason
        return out

    def detail(self, *, available: bool, reason: str | None) -> dict[str, Any]:
        """Full public view for `get`: everything a caller needs to build `params`."""
        out = self.summary(available=available, reason=reason)
        out["prompt"] = self.prompt
        out["parameters"] = [p.public() for p in self.parameters]
        out["medias"] = [m.public() for m in self.medias]
        out["pricing"] = self.pricing.public()
        return out
