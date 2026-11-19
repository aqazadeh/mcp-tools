"""Jobs family: wait for and inspect generation jobs."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import NextStep
from mcp_tools.server.registry import ToolDef


class JobsResult(BaseModel):
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
            annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
            visibility=("model", "app"),
        )
    ]
