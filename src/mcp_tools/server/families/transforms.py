"""Transforms family: single-purpose tools with strict, typed inputs.

Unlike the generate tools these take no free-form params: each does one thing to one file.
They run through the same job pipeline (budget gates, idempotency, refunds).
"""

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult
from pydantic import Field

from mcp_tools.core.errors import INVALID_PARAMS, PROVIDER_UNAVAILABLE, ApiError
from mcp_tools.core.platform import Platform
from mcp_tools.server.envelope import platform_of
from mcp_tools.server.families.generate import IDEMPOTENCY, MAX_CREDITS, SPEND, WAIT, GenerateResult, run_generation
from mcp_tools.server.params import require_choice
from mcp_tools.server.registry import ToolDef

_IMAGE = Field(description="The image to process: a media id (med_...) or the job id of a finished image job.")
_MODEL = Field(description="Optional model id for this task; by default the first available one.", max_length=64)


def _model_for(platform: Platform, task: Any, wanted: str) -> str:
    """The model to use for a task: the one asked for, else the first whose provider is usable."""
    if wanted:
        return wanted
    candidates = sorted(platform.catalog.by_task(task), key=lambda m: m.provider == "mock")  # placeholders last
    for model in candidates:
        if platform.providers.availability(model.provider).available:
            return model.id
    raise ApiError(
        PROVIDER_UNAVAILABLE,
        f"no model for {task} is available on this server",
        hint="Ask the operator to configure a provider that offers it.",
    )


async def upscale_image(
    ctx: Context,
    image_id: Annotated[str, _IMAGE],
    scale: Annotated[int, Field(description="Enlargement factor.", json_schema_extra={"enum": [2, 4]})] = 2,
    model: Annotated[str, _MODEL] = "",
    max_credits: Annotated[int | None, MAX_CREDITS] = None,
    idempotency_key: Annotated[str, IDEMPOTENCY] = "",
    wait_seconds: Annotated[int, WAIT] = 0,
) -> Annotated[CallToolResult, GenerateResult]:
    require_choice("scale", str(scale), ("2", "4"))
    platform = platform_of(ctx)
    model_id = _model_for(platform, "upscale", model)
    params: dict[str, Any] = {"model": model_id, "medias": [{"role": "image", "value": image_id}]}
    if any(p.name == "scale" for p in platform.catalog.get(model_id).parameters):
        params["scale"] = scale
    elif scale != 2:
        raise ApiError(
            INVALID_PARAMS,
            f"model {model_id} enlarges by a fixed factor and takes no scale",
            hint="Omit scale, or pass another upscale model.",
        )
    return await run_generation(
        ctx,
        tool="upscale_image",
        output_type="image",
        task="upscale",
        params=params,
        max_credits=max_credits,
        idempotency_key=idempotency_key,
        wait_seconds=wait_seconds,
        context="",
    )


async def remove_background(
    ctx: Context,
    image_id: Annotated[str, _IMAGE],
    model: Annotated[str, _MODEL] = "",
    max_credits: Annotated[int | None, MAX_CREDITS] = None,
    idempotency_key: Annotated[str, IDEMPOTENCY] = "",
    wait_seconds: Annotated[int, WAIT] = 0,
) -> Annotated[CallToolResult, GenerateResult]:
    params = {
        "model": _model_for(platform_of(ctx), "remove_background", model),
        "medias": [{"role": "image", "value": image_id}],
    }
    return await run_generation(
        ctx,
        tool="remove_background",
        output_type="image",
        task="remove_background",
        params=params,
        max_credits=max_credits,
        idempotency_key=idempotency_key,
        wait_seconds=wait_seconds,
        context="",
    )


_TAIL = (
    " Spends credits and returns a job id like the generate tools: if status is 'approval_required' nothing "
    "was created; wait with jobs_wait. The result is a new image; the original is kept."
)


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="upscale_image",
            family="transforms",
            handler=upscale_image,
            title="Upscale image",
            description=(
                "Enlarge one image 2x or 4x. Use it when the user wants a bigger or sharper version of an "
                "existing image; it does not change the content." + _TAIL
            ),
            annotations=SPEND,
            effects=frozenset({"spend", "network"}),
        ),
        ToolDef(
            name="remove_background",
            family="transforms",
            handler=remove_background,
            title="Remove background",
            description=(
                "Cut the subject out of one image and return it on a transparent background (PNG). Use it "
                "for product shots, stickers and compositing." + _TAIL
            ),
            annotations=SPEND,
            effects=frozenset({"spend", "network"}),
        ),
    ]
