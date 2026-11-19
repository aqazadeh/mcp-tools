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
        "by id (med_... or the job_... of a finished job), never as URLs. If a call returns status "
        "'approval_required', nothing was created: tell the user the estimated credits and only call again with "
        "max_credits if they agree. After a timeout, retry with the same idempotency_key; never resubmit blindly."
    ),
    "jobs": (
        "Results: pass the job ids to jobs_wait until all_terminal is true. Failed and moderated jobs are "
        "refunded automatically; report error.message to the user instead of retrying in a loop."
    ),
    "billing": "Budget: balance shows credits and the server's limits, which tools cannot change.",
}


def build_instructions(families: Sequence[str]) -> str:
    notes = [_FAMILY_NOTES[name] for name in families if name in _FAMILY_NOTES]
    return "\n".join([_INTRO, *notes])
