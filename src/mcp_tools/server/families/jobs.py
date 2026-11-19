"""Jobs family: wait for and inspect generation jobs."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field

from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.core.pagination import page_request
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import NextStep, Wire, choice, optional_choice
from mcp_tools.server.registry import ToolDef
from mcp_tools.server.ui.resources import RESULTS


class JobsResult(Wire):
    kind: Literal["jobs"] = "jobs"
    schema_version: int = 1
    jobs: list[dict[str, Any]]
    all_terminal: bool
    poll_after_seconds: int | None = None
    next: NextStep | None = None


async def jobs_wait(
    ctx: Context,
    job_ids: Annotated[list[str], Field(description="1-12 job ids returned by a generate tool.")],
    timeout_seconds: Annotated[
        int, Field(description="How long to wait for the jobs to finish, 0-15. Use 0 for a snapshot.")
    ] = 10,
) -> Annotated[CallToolResult, JobsResult]:
    platform = platform_of(ctx)
    waited = await platform.jobs.wait(list(job_ids), timeout=float(timeout_seconds), principal=platform.principal)
    next_step = None
    if not waited.all_terminal:
        next_step = NextStep(
            tool="jobs_wait",
            after_seconds=waited.poll_after_seconds,
            hint="Some jobs are still running; call again with the same job_ids.",
        )
    return ok(
        JobsResult(
            jobs=[view.to_dict() for view in waited.jobs],
            all_terminal=waited.all_terminal,
            poll_after_seconds=waited.poll_after_seconds,
            next=next_step,
        )
    )


MAX_SHOW_JOBS = 60
OUTPUT_TYPES = ("image", "video", "audio")


class JobCancelResult(Wire):
    kind: Literal["job_cancel"] = "job_cancel"
    schema_version: int = 1
    job: dict[str, Any]
    refunded: int = Field(description="Credits returned right away. 0 while a running job is still stopping.")
    message: str


class GenerationsResult(Wire):
    kind: Literal["generations"] = "generations"
    schema_version: int = 1
    items: list[dict[str, Any]]
    has_more: bool = False
    next_cursor: str | None = None
    display: str = "none"


async def job_cancel(
    ctx: Context,
    job_id: Annotated[str, Field(description="The job to cancel (job_...).", max_length=64)],
) -> Annotated[CallToolResult, JobCancelResult]:
    platform = platform_of(ctx)
    view, refunded, was_active = await platform.jobs.cancel(job_id, principal=platform.principal)
    if not was_active:
        message = f"The job had already finished with status {view.status}; nothing changed."
    elif view.status == "canceled":
        message = "Canceled; the credits were refunded."
    elif view.terminal:
        message = f"The job finished with status {view.status} before it could be canceled."
    else:
        message = "Cancellation requested. The job stops at its next check; credits are refunded when it does."
    return ok(JobCancelResult(job=view.to_dict(), refunded=refunded, message=message))


async def show_jobs(
    ctx: Context,
    job_ids: Annotated[list[str], Field(description="1-60 job ids.")],
) -> Annotated[CallToolResult, JobsResult]:
    platform = platform_of(ctx)
    if not job_ids or len(job_ids) > MAX_SHOW_JOBS:
        raise ApiError(INVALID_PARAMS, f"pass 1 to {MAX_SHOW_JOBS} job ids", hint="Show larger sets in groups.")
    views = await platform.jobs.get([str(j) for j in job_ids], principal=platform.principal)
    all_terminal = all(v.terminal for v in views)
    return ok(
        JobsResult(
            jobs=[v.to_dict() for v in views],
            all_terminal=all_terminal,
            next=None if all_terminal else NextStep(tool="jobs_wait", hint="Wait for the unfinished jobs."),
        )
    )


async def show_generations(
    ctx: Context,
    type: Annotated[str, choice("Only this output type; omit for all.", OUTPUT_TYPES)] = "",
    only_completed: Annotated[bool, Field(description="Leave out failed, canceled and running jobs.")] = True,
    folder_id: Annotated[str, Field(description="Only jobs generated into this folder (fld_...).", max_length=64)] = "",
    limit: Annotated[int, Field(description="Page size, 1-50 (default 20).")] = 0,
    cursor: Annotated[str, Field(description="next_cursor from the previous page; keep the same filters.")] = "",
) -> Annotated[CallToolResult, GenerationsResult]:
    platform = platform_of(ctx)
    page = await platform.jobs.history(
        page_request(limit, cursor),
        principal=platform.principal,
        output_type=optional_choice("type", type, OUTPUT_TYPES),
        only_completed=only_completed,
        folder_id=folder_id or None,
    )
    return ok(
        GenerationsResult(
            items=[view.to_dict() for view in page.items], has_more=page.has_more, next_cursor=page.next_cursor
        )
    )


_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="jobs_wait",
            family="jobs",
            handler=jobs_wait,
            title="Wait for jobs",
            description=(
                "Wait for generation jobs and return their status and results. Read-only; costs nothing and never "
                "resubmits. Returns when every job is terminal (completed, failed, canceled, moderated, not_found) "
                "or the time is up; if all_terminal is false, wait poll_after_seconds and call again. A completed "
                "job lists results with media_id (usable as a medias value) and local_path or url. Failed and "
                "moderated jobs are refunded automatically and carry error.code / error.message."
            ),
            annotations=_READ,
            visibility=("model", "app"),
        ),
        ToolDef(
            name="job_cancel",
            family="jobs",
            handler=job_cancel,
            title="Cancel job",
            description=(
                "Stop one unfinished generation job. A job that has not started is canceled and refunded at "
                "once; a running one is asked to stop and is refunded when it does (confirm with jobs_wait). "
                "A finished job is left as it is. Use it only when the user no longer wants the result."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
            ),
            effects=frozenset({"write"}),
        ),
        ToolDef(
            name="show_jobs",
            family="jobs",
            handler=show_jobs,
            ui_uri=RESULTS.uri,
            title="Show jobs",
            description=(
                "Show the current status and results of up to 60 jobs by id, without waiting. Read-only. Use it "
                "to present finished results or to re-read jobs from earlier in the conversation; to wait for "
                "running jobs use jobs_wait instead."
            ),
            annotations=_READ,
        ),
        ToolDef(
            name="show_generations",
            family="jobs",
            handler=show_generations,
            ui_uri=RESULTS.uri,
            title="Show generations",
            description=(
                "List past generation jobs, newest first, with their results. Read-only. Filter by output type "
                "or folder; by default only completed jobs are listed. Use it when the user refers to earlier "
                "work whose job ids are not in the conversation. Pages with limit and cursor."
            ),
            annotations=_READ,
        ),
    ]
