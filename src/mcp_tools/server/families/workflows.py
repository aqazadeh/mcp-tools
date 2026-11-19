"""Workflows family: step-by-step instructions the model loads on demand."""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field

from mcp_tools.core.errors import NOT_FOUND, ApiError
from mcp_tools.core.workflows.loader import Workflow
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import Wire
from mcp_tools.server.registry import ToolDef


class WorkflowResult(Wire):
    kind: Literal["workflow"] = "workflow"
    schema_version: int = 1
    mode: str = Field(description="catalog: the list of workflows. workflow: one workflow's instructions.")
    workflows: list[dict[str, Any]] | None = None
    name: str | None = None
    version: str | None = None
    revision: str | None = None
    instructions: str | None = None
    files: list[dict[str, Any]] | None = None
    message: str | None = None


class WorkflowFileResult(Wire):
    kind: Literal["workflow_file"] = "workflow_file"
    schema_version: int = 1
    workflow: str
    path: str
    size_bytes: int
    sha256: str
    contents: str | None = None


def _find(workflows: dict[str, Workflow], name: str) -> Workflow:
    workflow = workflows.get(name.strip())
    if workflow is None:
        raise ApiError(
            NOT_FOUND,
            f"no workflow named {name!r}",
            hint="Call workflow_instructions without arguments to list them.",
            recovery_tool="workflow_instructions",
            allowed=sorted(workflows),
        )
    return workflow


async def workflow_instructions(
    ctx: Context,
    workflow: Annotated[str, Field(description="Workflow name; omit to list the workflows.", max_length=80)] = "",
) -> Annotated[CallToolResult, WorkflowResult]:
    workflows = platform_of(ctx).workflows
    if not workflow.strip():
        return ok(
            WorkflowResult(
                mode="catalog",
                workflows=[w.summary() for w in sorted(workflows.values(), key=lambda w: (not w.builtin, w.name))],
                message="Call again with `workflow` set to a name to load its instructions.",
            )
        )
    found = _find(workflows, workflow)
    return ok(
        WorkflowResult(
            mode="workflow",
            name=found.name,
            version=found.version,
            revision=found.digest,
            instructions=found.instructions,
            files=[{"path": f.path, "size_bytes": f.size} for f in found.files.values()] or None,
            message="Follow these steps with this server's tools. Read a listed file with workflow_file when a "
            "step refers to it.",
        )
    )


async def workflow_file(
    ctx: Context,
    workflow: Annotated[str, Field(description="Workflow name.", max_length=80)],
    path: Annotated[str, Field(description="A path from that workflow's `files` list, exactly.", max_length=200)],
    include_contents: Annotated[bool, Field(description="false returns only size and checksum.")] = True,
) -> Annotated[CallToolResult, WorkflowFileResult]:
    found = _find(platform_of(ctx).workflows, workflow)
    entry = found.files.get(path)  # a lookup in the manifest; the request never becomes a filesystem path
    if entry is None:
        raise ApiError(
            NOT_FOUND,
            f"workflow {found.name} has no file {path!r}",
            hint="Use a path from the files list of workflow_instructions.",
            recovery_tool="workflow_instructions",
            allowed=sorted(found.files),
        )
    return ok(
        WorkflowFileResult(
            workflow=found.name,
            path=entry.path,
            size_bytes=entry.size,
            sha256=entry.sha256,
            contents=entry.text if include_contents else None,
        )
    )


_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="workflow_instructions",
            family="workflows",
            handler=workflow_instructions,
            title="Workflow instructions",
            description=(
                "Load a tested step-by-step method for a multi-step creative task. Without arguments it lists "
                "the workflows with a one-line description each; with `workflow` it returns that workflow's "
                "instructions and its reference files. Read-only. Call it before starting multi-shot videos, "
                "product photo sets or other work that takes several generate calls, then follow the steps."
            ),
            annotations=_READ,
        ),
        ToolDef(
            name="workflow_file",
            family="workflows",
            handler=workflow_file,
            title="Workflow file",
            description=(
                "Read one reference file of a workflow (a template, a checklist, examples). Read-only. Only "
                "paths listed by workflow_instructions for that workflow can be read."
            ),
            annotations=_READ,
        ),
    ]
