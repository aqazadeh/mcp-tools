"""Server-level instructions: the routing prompt a client shows its model.

Composed from the enabled tool families so it never mentions a tool that is not registered.
Kept short on purpose; per-tool policy lives in the tool descriptions.
"""

from collections.abc import Sequence

_INTRO = (
    "mcp-tools generates images, video and audio through several providers behind one tool surface. "
    "Tool results are compact JSON; errors are JSON with a `code`, a `hint` and sometimes a `recovery_tool`."
)

_FAMILY_NOTES: dict[str, str] = {
    "catalog": (
        "Models: call models_explore (action 'recommend' with the goal, or 'list'/'search') to pick a model, "
        "then action 'get' for the parameters and media roles that model accepts. Never guess a model id."
    ),
    "generate": (
        "Generating: generate_* tools spend credits and return job ids immediately. Reference files are passed "
        "by id (med_... or the job_... of a finished job), never as URLs. For several independent requests use "
        "generate_batch; estimate_cost prices work without creating it. If a call returns status "
        "'approval_required', nothing was created: tell the user the estimated credits and only call again with "
        "max_credits if they agree. After a timeout, retry with the same idempotency_key; never resubmit blindly."
    ),
    "jobs": (
        "Results: pass the job ids to jobs_wait until all_terminal is true. Failed and moderated jobs are "
        "refunded automatically; report error.message to the user instead of retrying in a loop."
    ),
    "media": (
        "Files: when the user gives a link or a local file, import it first (media_import_url, or "
        "media_import_path where offered) and use the returned media_id. To look at an image yourself, call "
        "media_get with include_preview."
    ),
    "projects": (
        "Organising: projects hold folders; pass a folder id as params.folder_id to generate into it, or use "
        "assets_attach afterwards. Deleting a project or folder never deletes jobs or files, but still ask the "
        "user before any delete or remove call."
    ),
    "workflows": (
        "Multi-step work: before a multi-shot video, a product photo set or similar, call "
        "workflow_instructions (no arguments lists them) and follow the workflow that fits."
    ),
    "appreg": (
        "More capabilities: when no tool fits (resizing or cropping an image, a contact sheet, a storyboard "
        "run), look in apps_search, read the action with apps_describe, then run it with apps_invoke."
    ),
    "billing": "Budget: balance shows credits and the server's limits, which tools cannot change.",
}


def build_instructions(families: Sequence[str]) -> str:
    notes = [_FAMILY_NOTES[name] for name in families if name in _FAMILY_NOTES]
    return "\n".join([_INTRO, *notes])
