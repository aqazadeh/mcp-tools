"""App manifests: small tools that live behind three meta-tools instead of the tool list.

An app is a set of actions, each with a JSON Schema for its arguments. The model discovers
apps with a search, reads an action's schema, then invokes it. That keeps rarely used
capabilities out of `tools/list` (which every session pays for) while leaving them reachable.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp_tools.core.clock import Clock
from mcp_tools.core.principal import Principal

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AppContext:
    """What an action handler may touch. Passed in by the platform; apps never import it."""

    principal: Principal
    clock: Clock
    media: Any  # MediaService
    jobs: Any  # JobService
    max_credits: int | None = None
    idempotency_key: str | None = None
    load_run: Callable[[str], Awaitable[JsonDict]] | None = None


@dataclass(frozen=True)
class ActionResult:
    output: JsonDict
    status: str = "completed"  # completed | running | approval_required
    credits: int = 0
    group_id: str | None = None
    replayed: bool = False


Handler = Callable[[AppContext, JsonDict], Awaitable[ActionResult]]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    input_schema: JsonDict
    handler: Handler
    read_only: bool = False
    destructive: bool = False
    spends: bool = False

    def public(self) -> JsonDict:
        return {
            "action": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "annotations": {
                "read_only": self.read_only,
                "destructive": self.destructive,
                "spends_credits": self.spends,
            },
        }


@dataclass(frozen=True)
class AppSpec:
    id: str
    name: str
    description: str
    actions: tuple[ActionSpec, ...]
    tags: tuple[str, ...] = ()
    revision: str = field(init=False, default="")

    def __post_init__(self) -> None:
        manifest = json.dumps(
            {"id": self.id, "description": self.description, "actions": [a.public() for a in self.actions]},
            sort_keys=True,
        )
        object.__setattr__(self, "revision", hashlib.sha256(manifest.encode()).hexdigest()[:12])

    def action(self, name: str) -> ActionSpec | None:
        return next((a for a in self.actions if a.name == name), None)

    def summary(self) -> JsonDict:
        return {
            "app_id": self.id,
            "name": self.name,
            "description": self.description,
            "actions": [a.name for a in self.actions],
            "tags": list(self.tags),
        }
