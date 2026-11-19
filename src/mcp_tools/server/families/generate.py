"""Generate family: create generation jobs."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from mcp_tools.core.jobs.service import GenerationRequest, Outcome
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import GenerateParams, NextStep
from mcp_tools.server.registry import ToolDef


class JobRef(BaseModel):
    index: int
    job_id: str
    status: str
    results: list[dict[str, Any]] | None = None
    error: dict[str, Any] | None = None


class Credits(BaseModel):
    estimated: int | None = None
    charged: int | None = None
    balance: int | None = None


class GenerateResult(BaseModel):
    kind: Literal["generation"] = "generation"
    schema_version: int = 1
    status: str = Field(description="submitted: jobs were created. approval_required: nothing was created.")
    jobs: list[JobRef] = Field(default_factory=list)
    credits: Credits | None = None
    adjustments: list[dict[str, Any]] | None = None
    approval: dict[str, Any] | None = None
    replayed: bool | None = None
    display: str = "none"
    next: NextStep | None = None
    message: str | None = None


def approval_message(outcome: Outcome) -> str:
    if outcome.reason == "above_max_credits":
        return (
            f"Nothing was created: the estimate is {outcome.credits_total} credits, above max_credits "
            f"({outcome.limit}). Tell the user the estimate; if they agree, call again with a higher max_credits."
        )
    return (
        f"Nothing was created: the estimate is {outcome.credits_total} credits, above the amount this server "
        f"spends without an explicit cap ({outcome.limit}). Tell the user the estimate; if they agree, call "
        f"again with max_credits set to at least {outcome.credits_total}."
    )


async def _generate(
    ctx: Context,
    *,
    tool: str,
    output_type: str,
    params: GenerateParams,
    max_credits: int | None,
    idempotency_key: str,
    wait_seconds: int,
    context: str,
) -> CallToolResult:
    platform = platform_of(ctx)
    outcome = await platform.jobs.create(
        [GenerationRequest(index=0, params=params.raw())],
        principal=platform.principal,
        tool=tool,
        output_type=output_type,
        max_credits=max_credits,
        idempotency_key=idempotency_key or None,
        client_context=context[:500] or None,
        strict=True,
    )
    adjustments = outcome.items[0].adjustments if outcome.items else []
    if outcome.status == "approval_required":
        return ok(
            GenerateResult(
                status="approval_required",
                credits=Credits(estimated=outcome.credits_total),
                adjustments=adjustments or None,
                approval={
                    "estimated": outcome.credits_total,
                    "limit": outcome.limit,
                    "reason": outcome.reason,
                },
                message=approval_message(outcome),
            )
        )

    refs = [JobRef(index=position, job_id=job.job_id, status="queued") for position, job in enumerate(outcome.jobs)]
    next_step: NextStep | None = NextStep(tool="jobs_wait", after_seconds=2, hint="Pass these job_ids to jobs_wait.")
    if wait_seconds > 0:
        waited = await platform.jobs.wait(
            [r.job_id for r in refs], timeout=float(wait_seconds), principal=platform.principal
        )
        by_id = {view.job_id: view for view in waited.jobs}
        for ref in refs:
            view = by_id[ref.job_id]
            ref.status = view.status
            ref.results = [r.to_dict() for r in view.results] or None
            ref.error = view.error
        next_step = (
            None if waited.all_terminal else NextStep(tool="jobs_wait", after_seconds=waited.poll_after_seconds or 2)
        )
    return ok(
        GenerateResult(
            status="submitted",
            jobs=refs,
            credits=Credits(charged=outcome.credits_total, balance=outcome.balance),
            adjustments=adjustments or None,
            replayed=True if outcome.replayed else None,
            next=next_step,
        )
    )


_PARAMS = Field(description="Request for one model. Model-specific parameters go next to the common fields.")
_MAX_CREDITS = Field(
    description="Most this call may spend, in credits. Needed when the estimate exceeds the server's auto-approve "
    "amount; it can never raise the server's own limits."
)
_IDEMPOTENCY = Field(
    description="Optional. Reuse the same key when retrying this same request after a timeout to avoid a "
    "duplicate; never reuse it for a different request.",
    max_length=128,
)
_WAIT = Field(description="Wait up to this many seconds (0-12) for results before returning.")
_CONTEXT = Field(description="One short sentence on the user's goal. For server logs only.", max_length=500)


async def generate_image(
    ctx: Context,
    params: Annotated[GenerateParams, _PARAMS],
    max_credits: Annotated[int | None, _MAX_CREDITS] = None,
    idempotency_key: Annotated[str, _IDEMPOTENCY] = "",
    wait_seconds: Annotated[int, _WAIT] = 0,
    context: Annotated[str, _CONTEXT] = "",
) -> Annotated[CallToolResult, GenerateResult]:
    return await _generate(
        ctx,
        tool="generate_image",
        output_type="image",
        params=params,
        max_credits=max_credits,
        idempotency_key=idempotency_key,
        wait_seconds=wait_seconds,
        context=context,
    )


_SPEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

_COMMON = (
    "Spends credits. Returns job ids at once; results arrive later. If status is 'approval_required', nothing "
    "was created: tell the user the estimate and call again with max_credits only if they agree. Medias are "
    "passed by id (med_ or job_), never as URLs. Server-side corrections come back in `adjustments`. After a "
    "timeout, retry with the same idempotency_key instead of resubmitting blindly. Next: jobs_wait."
)


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="generate_image",
            family="generate",
            handler=generate_image,
            title="Generate image",
            description=(
                "Create images with one model (params.model from models_explore; count 1-4 variants). " + _COMMON
            ),
            annotations=_SPEND,
            effects=frozenset({"spend", "network"}),
        )
    ]
