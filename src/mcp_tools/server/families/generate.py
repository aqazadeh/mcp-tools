"""Generate family: create generation jobs."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field, ValidationError

from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.core.jobs.service import MAX_BATCH, GenerationRequest, Outcome
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import GenerateParams, NextStep, Wire
from mcp_tools.server.registry import ToolDef


class JobRef(Wire):
    index: int
    job_id: str
    status: str
    results: list[dict[str, Any]] | None = None
    error: dict[str, Any] | None = None


class Credits(Wire):
    estimated: int | None = None
    charged: int | None = None
    balance: int | None = None


class GenerateResult(Wire):
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


async def generate_video(
    ctx: Context,
    params: Annotated[GenerateParams, _PARAMS],
    max_credits: Annotated[int | None, _MAX_CREDITS] = None,
    idempotency_key: Annotated[str, _IDEMPOTENCY] = "",
    wait_seconds: Annotated[int, _WAIT] = 0,
    context: Annotated[str, _CONTEXT] = "",
) -> Annotated[CallToolResult, GenerateResult]:
    return await _generate(
        ctx,
        tool="generate_video",
        output_type="video",
        params=params,
        max_credits=max_credits,
        idempotency_key=idempotency_key,
        wait_seconds=wait_seconds,
        context=context,
    )


async def generate_audio(
    ctx: Context,
    params: Annotated[GenerateParams, _PARAMS],
    max_credits: Annotated[int | None, _MAX_CREDITS] = None,
    idempotency_key: Annotated[str, _IDEMPOTENCY] = "",
    wait_seconds: Annotated[int, _WAIT] = 0,
    context: Annotated[str, _CONTEXT] = "",
) -> Annotated[CallToolResult, GenerateResult]:
    return await _generate(
        ctx,
        tool="generate_audio",
        output_type="audio",
        params=params,
        max_credits=max_credits,
        idempotency_key=idempotency_key,
        wait_seconds=wait_seconds,
        context=context,
    )


# -- batch and estimate ----------------------------------------------------------------------


class BatchRequest(Wire):
    index: int | None = Field(default=None, description="Your own number for this request; echoed in the result.")
    params: dict[str, Any] = Field(description="Same shape as the `params` of a generate tool (model, prompt, …).")


class BatchItem(Wire):
    index: int
    status: str = Field(description="submitted, quoted or rejected.")
    model: str | None = None
    job_ids: list[str] | None = None
    credits: int | None = None
    breakdown: dict[str, Any] | None = None
    adjustments: list[dict[str, Any]] | None = None
    error: dict[str, Any] | None = None


class BatchResult(Wire):
    kind: Literal["batch"] = "batch"
    schema_version: int = 1
    status: str = Field(description="submitted, approval_required (nothing was created) or rejected (no valid item).")
    items: list[BatchItem]
    job_ids: list[str] | None = None
    credits: Credits | None = None
    approval: dict[str, Any] | None = None
    replayed: bool | None = None
    display: str = "none"
    next: NextStep | None = None
    message: str | None = None


class EstimateResult(Wire):
    kind: Literal["estimate"] = "estimate"
    schema_version: int = 1
    items: list[BatchItem]
    total_credits: int
    balance: int | None = None
    needs_max_credits: bool = Field(description="True when a generate call for this needs an explicit max_credits.")
    exceeds_call_ceiling: bool = False
    message: str | None = None


def _parse_requests(requests: list[BatchRequest]) -> tuple[list[GenerationRequest], dict[int, ApiError]]:
    if not requests or len(requests) > MAX_BATCH:
        raise ApiError(INVALID_PARAMS, f"send 1 to {MAX_BATCH} requests", hint="Split larger sets into several calls.")
    indices = [item.index if item.index is not None else position for position, item in enumerate(requests)]
    if len(set(indices)) != len(indices):
        raise ApiError(INVALID_PARAMS, "request indices must be unique", hint="Number the requests 0, 1, 2 …")
    parsed: list[GenerationRequest] = []
    errors: dict[int, ApiError] = {}
    for index, item in zip(indices, requests, strict=True):
        try:
            parsed.append(GenerationRequest(index=index, params=GenerateParams.model_validate(item.params).raw()))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "params"
            errors[index] = ApiError(
                INVALID_PARAMS,
                f"params.{where}: {first['msg']}",
                hint="Call models_explore with action 'get' for the fields this model accepts.",
                recovery_tool="models_explore",
            )
    return parsed, errors


def _batch_items(outcome: Outcome | None, errors: dict[int, ApiError], *, ok_status: str) -> list[BatchItem]:
    items: dict[int, BatchItem] = {
        index: BatchItem(index=index, status="rejected", error=err.to_dict()) for index, err in errors.items()
    }
    if outcome is not None:
        for index, err in outcome.errors.items():
            items[index] = BatchItem(index=index, status="rejected", error=err.to_dict())
        jobs_by_index: dict[int, list[str]] = {}
        for job in outcome.jobs:
            jobs_by_index.setdefault(job.index, []).append(job.job_id)
        for quoted in outcome.items:
            items[quoted.index] = BatchItem(
                index=quoted.index,
                status=ok_status,
                model=quoted.model,
                job_ids=jobs_by_index.get(quoted.index),
                credits=quoted.credits,
                breakdown=quoted.breakdown if ok_status == "quoted" else None,
                adjustments=quoted.adjustments or None,
            )
    return [items[index] for index in sorted(items)]


_REQUESTS = Field(description="1-12 independent requests; each has `params` like a generate tool and an `index`.")


async def generate_batch(
    ctx: Context,
    requests: Annotated[list[BatchRequest], _REQUESTS],
    max_credits: Annotated[int | None, _MAX_CREDITS] = None,
    idempotency_key: Annotated[str, _IDEMPOTENCY] = "",
    context: Annotated[str, _CONTEXT] = "",
) -> Annotated[CallToolResult, BatchResult]:
    platform = platform_of(ctx)
    parsed, errors = _parse_requests(requests)
    if not parsed:
        return ok(
            BatchResult(
                status="rejected",
                items=_batch_items(None, errors, ok_status="submitted"),
                message="Nothing was created: every request is invalid. Fix the errors and send them again.",
            )
        )
    outcome = await platform.jobs.create(
        parsed,
        principal=platform.principal,
        tool="generate_batch",
        output_type=None,
        max_credits=max_credits,
        idempotency_key=idempotency_key or None,
        client_context=context[:500] or None,
        strict=False,
    )
    if outcome.status == "approval_required":
        return ok(
            BatchResult(
                status="approval_required",
                items=_batch_items(outcome, errors, ok_status="quoted"),
                credits=Credits(estimated=outcome.credits_total),
                approval={"estimated": outcome.credits_total, "limit": outcome.limit, "reason": outcome.reason},
                message=approval_message(outcome),
            )
        )
    job_ids = [job.job_id for job in outcome.jobs]
    if not job_ids:
        return ok(
            BatchResult(
                status="rejected",
                items=_batch_items(outcome, errors, ok_status="submitted"),
                message="Nothing was created: every request is invalid. Fix the errors and send them again.",
            )
        )
    rejected = len(errors) + len(outcome.errors)
    return ok(
        BatchResult(
            status="submitted",
            items=_batch_items(outcome, errors, ok_status="submitted"),
            job_ids=job_ids,
            credits=Credits(charged=outcome.credits_total, balance=outcome.balance),
            replayed=True if outcome.replayed else None,
            next=NextStep(tool="jobs_wait", after_seconds=2, hint="Pass job_ids to jobs_wait, at most 12 per call."),
            message=f"{rejected} request(s) were rejected and not charged; see their `error`." if rejected else None,
        )
    )


async def estimate_cost(
    ctx: Context,
    requests: Annotated[list[BatchRequest], _REQUESTS],
) -> Annotated[CallToolResult, EstimateResult]:
    platform = platform_of(ctx)
    parsed, errors = _parse_requests(requests)
    outcome = await platform.jobs.quote(parsed, principal=platform.principal) if parsed else None
    total = outcome.credits_total if outcome else 0
    settings = platform.settings
    balance = outcome.balance if outcome else None
    message = None
    if total > settings.max_credits_per_call:
        message = f"Above the server's per-call ceiling ({settings.max_credits_per_call}); split it into smaller calls."
    elif balance is not None and total > balance:
        message = "The balance is too low for this."
    elif total > settings.auto_approve_credits:
        message = f"Tell the user the estimate; if they agree, generate with max_credits of at least {total}."
    return ok(
        EstimateResult(
            items=_batch_items(outcome, errors, ok_status="quoted"),
            total_credits=total,
            balance=balance,
            needs_max_credits=total > settings.auto_approve_credits,
            exceeds_call_ceiling=total > settings.max_credits_per_call,
            message=message,
        )
    )


_SPEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

_COMMON = (
    "Spends credits. Returns job ids at once; results arrive later. If status is 'approval_required', nothing "
    "was created: tell the user the estimate and call again with max_credits only if they agree. Medias are "
    "passed by id (med_ or job_), never as URLs. Server-side corrections come back in `adjustments`. After a "
    "timeout, retry with the same idempotency_key instead of resubmitting blindly. Next: jobs_wait."
)


def _generate_def(name: str, handler: Any, title: str, lead: str) -> ToolDef:
    return ToolDef(
        name=name,
        family="generate",
        handler=handler,
        title=title,
        description=lead + " " + _COMMON,
        annotations=_SPEND,
        effects=frozenset({"spend", "network"}),
    )


def tools() -> list[ToolDef]:
    return [
        _generate_def(
            "generate_image",
            generate_image,
            "Generate image",
            "Create images with one model (params.model from models_explore; count 1-4 variants).",
        ),
        _generate_def(
            "generate_video",
            generate_video,
            "Generate video",
            "Create a video clip with one model (params.model from models_explore; duration in seconds). "
            "Video is the most expensive output: estimate_cost first when unsure.",
        ),
        _generate_def(
            "generate_audio",
            generate_audio,
            "Generate audio",
            "Create speech, music or sound with one model (params.model from models_explore).",
        ),
        ToolDef(
            name="generate_batch",
            family="generate",
            handler=generate_batch,
            title="Generate batch",
            description=(
                "Create several independent generations in one call (1-12 requests, any mix of image, video and "
                "audio models). Spends credits. Invalid requests are rejected one by one and cost nothing; the "
                "budget check covers the whole call, so 'approval_required' means nothing was created. Each "
                "result item echoes your `index`. For variants of one prompt use `count` on a generate tool "
                "instead. Retry with the same idempotency_key after a timeout. Next: jobs_wait with job_ids."
            ),
            annotations=_SPEND,
            effects=frozenset({"spend", "network"}),
        ),
        ToolDef(
            name="estimate_cost",
            family="generate",
            handler=estimate_cost,
            title="Estimate cost",
            description=(
                "Price 1-12 generation requests without creating anything. Read-only; costs nothing and reserves "
                "nothing. Takes the same `params` as the generate tools and returns credits per request, the "
                "total, corrections the server would apply, and whether a generate call would need an explicit "
                "max_credits. Use it before expensive work (video, large batches) or when the user asks the price."
            ),
            annotations=_READ,
        ),
    ]
