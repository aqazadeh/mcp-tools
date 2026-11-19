"""Turn a caller's `params` into a canonical request for one model.

Soft problems are fixed and reported as `adjustments` (a clamped number, the nearest aspect
ratio, a defaulted media role). Hard problems are errors that name the allowed values: an
unknown key, a wrong type or a missing required field. A key that is silently dropped
would spend credits on the wrong output, so nothing is dropped silently.
"""

import math
from dataclasses import dataclass, field
from typing import Any

from mcp_tools.core.catalog.spec import COMMON_FIELDS, MediaRoleSpec, ModelSpec, ParamSpec
from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.core.ids import id_prefix

MAX_COUNT = 4
MAX_PROMPT_CHARS = 20_000
MAX_MEDIAS = 40


@dataclass(frozen=True)
class MediaRefIn:
    role: str
    value: str  # a med_ or job_ id


@dataclass
class NormalizedRequest:
    model_id: str
    prompt: str | None
    aspect_ratio: str | None
    count: int
    duration: float | None
    params: dict[str, Any]
    medias: list[MediaRefIn]
    folder_id: str | None
    adjustments: list[dict[str, Any]] = field(default_factory=list)

    def canonical(self) -> dict[str, Any]:
        """What is stored with the job and hashed for idempotency."""
        return {
            "model": self.model_id,
            "prompt": self.prompt,
            "aspect_ratio": self.aspect_ratio,
            "duration": self.duration,
            "params": dict(sorted(self.params.items())),
            "medias": [{"role": m.role, "value": m.value} for m in self.medias],
            "folder_id": self.folder_id,
        }


def _invalid(message: str, *, hint: str, allowed: list[Any] | None = None, model: ModelSpec | None = None) -> ApiError:
    details = {"model": model.id} if model else None
    return ApiError(
        INVALID_PARAMS, message, hint=hint, recovery_tool="models_explore", allowed=allowed, details=details
    )


def _adjust(out: NormalizedRequest, name: str, before: Any, after: Any, reason: str) -> None:
    out.adjustments.append({"field": name, "from": before, "to": after, "reason": reason})


def _ratio(text: str) -> float | None:
    try:
        width, height = text.split(":")
        value = float(width) / float(height)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _nearest_ratio(wanted: str, allowed: list[str]) -> str | None:
    target = _ratio(wanted)
    if target is None:
        return None
    scored = [(abs(math.log(target / r)), option) for option in allowed if (r := _ratio(option)) is not None]
    return min(scored)[1] if scored else None


def _coerce(spec: ParamSpec, value: Any, model: ModelSpec, out: NormalizedRequest) -> Any:
    """Check one model parameter against its spec; clamp and coerce where that is unambiguous."""
    original = value
    if spec.type == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise _invalid(f"{spec.name} must be true or false", hint="Pass a JSON boolean.", model=model)
    elif spec.type in ("integer", "number"):
        if isinstance(value, bool):
            raise _invalid(f"{spec.name} must be a number", hint="Pass a JSON number.", model=model)
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise _invalid(f"{spec.name} must be a number", hint="Pass a JSON number.", model=model) from None
        if not isinstance(value, int | float) or not math.isfinite(value):
            raise _invalid(f"{spec.name} must be a number", hint="Pass a JSON number.", model=model)
        if spec.type == "integer":
            value = round(value)
        if spec.options is not None and value not in spec.options:
            numeric = [o for o in spec.options if isinstance(o, int | float)]
            value = min(numeric, key=lambda o: abs(o - value)) if numeric else value
        if spec.min is not None and value < spec.min:
            value = type(value)(spec.min) if spec.type == "integer" else spec.min
        if spec.max is not None and value > spec.max:
            value = type(value)(spec.max) if spec.type == "integer" else spec.max
        if spec.type == "integer":
            value = int(value)
    else:  # string
        if not isinstance(value, str):
            if spec.options is not None and value in spec.options:
                pass
            elif isinstance(value, int | float) and not isinstance(value, bool):
                value = str(value)
            else:
                raise _invalid(f"{spec.name} must be a string", hint="Pass a JSON string.", model=model)
        if spec.options is not None and value not in spec.options:
            folded = {str(o).lower(): o for o in spec.options}
            match = folded.get(str(value).lower())
            if match is None:
                raise _invalid(
                    f"{spec.name} does not accept {original!r}",
                    hint=f"Use one of the allowed values for model {model.id}.",
                    allowed=list(spec.options),
                    model=model,
                )
            value = match
    if value != original or type(value) is not type(original):
        _adjust(out, spec.name, original, value, "coerced to the nearest value the model accepts")
    return value


def _medias(raw: Any, model: ModelSpec, out: NormalizedRequest) -> list[MediaRefIn]:
    if raw in (None, "", []):
        raw = []
    if not isinstance(raw, list):
        raise _invalid("medias must be a list of {role, value}", hint="Pass a JSON array.", model=model)
    if len(raw) > MAX_MEDIAS:
        raise _invalid(f"at most {MAX_MEDIAS} medias are allowed", hint="Send fewer reference files.", model=model)
    roles = {m.role: m for m in model.medias}
    if raw and not roles:
        raise _invalid(
            f"model {model.id} does not accept reference media",
            hint="Remove medias or choose a model whose 'input' is 'media'.",
            model=model,
        )
    refs: list[MediaRefIn] = []
    counts: dict[str, int] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            raise _invalid(
                f"medias[{index}] must be an object with a string 'value'",
                hint="Use {'role': <role>, 'value': <med_ or job_ id>}.",
                model=model,
            )
        value = item["value"].strip()
        if id_prefix(value) not in ("med", "job"):
            is_url = value.lower().startswith(("http://", "https://", "data:", "file:"))
            raise ApiError(
                INVALID_PARAMS,
                f"medias[{index}].value must be a media id (med_...) or a job id (job_...)",
                hint="Import a URL with media_import_url first and pass the returned media_id."
                if is_url
                else "Use an id returned by a media or generate tool.",
                recovery_tool="media_import_url" if is_url else "media_list",
            )
        role = item.get("role") or ""
        if not isinstance(role, str):
            raise _invalid(f"medias[{index}].role must be a string", hint="Use a role the model lists.", model=model)
        if role not in roles:
            if len(roles) == 1 and not role:
                role = next(iter(roles))
                _adjust(out, f"medias[{index}].role", "", role, "the model has a single media role")
            else:
                raise _invalid(
                    f"medias[{index}].role {role!r} is not a role of model {model.id}",
                    hint="Use one of the model's media roles.",
                    allowed=sorted(roles),
                    model=model,
                )
        counts[role] = counts.get(role, 0) + 1
        refs.append(MediaRefIn(role=role, value=value))
    for role, spec in roles.items():
        given = counts.get(role, 0)
        if given > spec.max:
            raise _invalid(
                f"role {role!r} accepts at most {spec.max} media, got {given}",
                hint="Send fewer files for that role.",
                model=model,
            )
        if spec.required and given == 0:
            raise _invalid(
                f"model {model.id} requires a media with role {role!r}",
                hint=f"Upload or import the file, then pass {{'role': '{role}', 'value': <media id>}}.",
                model=model,
            )
    return refs


def role_accepts(spec: MediaRoleSpec, kind: str) -> bool:
    return kind in spec.kinds


def normalize_request(model: ModelSpec, raw: dict[str, Any]) -> NormalizedRequest:
    out = NormalizedRequest(
        model_id=model.id,
        prompt=None,
        aspect_ratio=None,
        count=1,
        duration=None,
        params={},
        medias=[],
        folder_id=None,
    )
    specs = {p.name: p for p in model.parameters}
    unknown = sorted(k for k in raw if k not in COMMON_FIELDS and k not in specs)
    if unknown:
        raise _invalid(
            f"unknown parameter(s) for model {model.id}: {', '.join(unknown)}",
            hint="Remove them or call models_explore with action 'get' to see what this model accepts.",
            allowed=sorted([*specs, *(COMMON_FIELDS - {"model"})]),
            model=model,
        )

    # prompt
    prompt = raw.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise _invalid("prompt must be a string", hint="Pass the prompt as text.", model=model)
    prompt = (prompt or "").strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        raise _invalid(f"prompt is longer than {MAX_PROMPT_CHARS} characters", hint="Shorten it.", model=model)
    if model.prompt == "none":
        if prompt:
            _adjust(out, "prompt", prompt[:40], None, "this model takes no prompt")
    elif model.prompt == "required" and not prompt:
        raise _invalid(f"model {model.id} requires a prompt", hint="Describe what to generate.", model=model)
    else:
        out.prompt = prompt or None

    # aspect ratio
    aspect = raw.get("aspect_ratio")
    if aspect is not None and not isinstance(aspect, str):
        raise _invalid("aspect_ratio must be a string such as '16:9'", hint="Pass it as text.", model=model)
    aspect = (aspect or "").strip()
    if model.aspect_ratios:
        if not aspect:
            out.aspect_ratio = model.aspect_ratios[0]
        elif aspect in model.aspect_ratios:
            out.aspect_ratio = aspect
        else:
            nearest = _nearest_ratio(aspect, model.aspect_ratios)
            if nearest is None:
                raise _invalid(
                    f"aspect_ratio {aspect!r} is not valid",
                    hint="Use one of the model's aspect ratios.",
                    allowed=list(model.aspect_ratios),
                    model=model,
                )
            _adjust(out, "aspect_ratio", aspect, nearest, "nearest aspect ratio the model supports")
            out.aspect_ratio = nearest
    elif aspect:
        _adjust(out, "aspect_ratio", aspect, None, "this model has no aspect ratio setting")

    # count
    count = raw.get("count", 1)
    if isinstance(count, str) and count.strip().lstrip("-").isdigit():
        count = int(count)
    if isinstance(count, bool) or not isinstance(count, int):
        raise _invalid("count must be a whole number", hint=f"Use 1 to {MAX_COUNT}.", model=model)
    clamped = min(max(count, 1), MAX_COUNT)
    if clamped != count:
        _adjust(out, "count", count, clamped, f"count is limited to 1..{MAX_COUNT}")
    out.count = clamped

    # duration (a common field, but only models that declare it take one)
    duration = raw.get("duration")
    duration_spec = specs.get("duration")
    if duration_spec is None:
        if duration is not None:
            _adjust(out, "duration", duration, None, "this model has no duration setting")
    else:
        value = duration if duration is not None else duration_spec.default
        if value is None and duration_spec.required:
            raise _invalid(f"model {model.id} requires a duration", hint="Pass duration in seconds.", model=model)
        if value is not None:
            out.duration = float(_coerce(duration_spec, value, model, out))

    # model-specific parameters
    for name, spec in specs.items():
        if name == "duration":
            continue
        if name in raw and raw[name] is not None:
            out.params[name] = _coerce(spec, raw[name], model, out)
        elif spec.default is not None:
            out.params[name] = spec.default
        elif spec.required:
            raise _invalid(
                f"model {model.id} requires parameter {name!r}",
                hint="Call models_explore with action 'get' to see the allowed values.",
                allowed=list(spec.options) if spec.options else None,
                model=model,
            )

    out.medias = _medias(raw.get("medias"), model, out)

    folder_id = raw.get("folder_id")
    if folder_id:
        if id_prefix(folder_id) != "fld":
            raise ApiError(
                INVALID_PARAMS,
                "folder_id must be a folder id (fld_...)",
                hint="Use root_folder_id from projects_create or an id from folders_list, or omit it.",
                recovery_tool="projects_list",
            )
        out.folder_id = folder_id
    return out
