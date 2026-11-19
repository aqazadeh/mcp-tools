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
}


def build_instructions(families: Sequence[str]) -> str:
    notes = [_FAMILY_NOTES[name] for name in families if name in _FAMILY_NOTES]
    return "\n".join([_INTRO, *notes])
