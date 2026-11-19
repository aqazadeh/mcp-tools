"""The app registry: search, describe, invoke."""

from collections.abc import Iterable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry
from sqlalchemy import select

from mcp_tools.core.appreg.spec import ActionResult, ActionSpec, AppContext, AppSpec, JsonDict
from mcp_tools.core.clock import Clock
from mcp_tools.core.errors import CONFIRMATION_REQUIRED, CONFLICT, INVALID_PARAMS, NOT_FOUND, ApiError
from mcp_tools.core.ids import is_id, new_id
from mcp_tools.core.principal import Principal
from mcp_tools.db.schema import app_runs
from mcp_tools.db.store import Store

_NO_REMOTE_REFS: Registry[Any] = Registry()  # an empty registry: a schema can never pull in a remote $ref


def _tokens(text: str) -> list[str]:
    return [word for word in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(word) > 1]


class AppRegistry:
    def __init__(self, apps: Iterable[AppSpec], *, store: Store, clock: Clock) -> None:
        self._apps: dict[str, AppSpec] = {}
        for app in apps:
            if app.id in self._apps:
                raise ValueError(f"duplicate app id {app.id!r}")
            for action in app.actions:
                Draft202012Validator.check_schema(action.input_schema)
            self._apps[app.id] = app
        self._store = store
        self._clock = clock

    # -- discovery ---------------------------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[JsonDict]:
        words = _tokens(query)
        scored = []
        for app in self._apps.values():
            haystack = _tokens(" ".join([app.id, app.name, app.description, *app.tags, *(a.name for a in app.actions)]))
            action_text = _tokens(" ".join(a.description for a in app.actions))
            score = sum(3 for w in words if w in haystack) + sum(1 for w in words if w in action_text)
            if score or not words:
                scored.append((-score, app.id, app))
        return [app.summary() for _, _, app in sorted(scored, key=lambda entry: entry[:2])[:limit]]

    def _app(self, app_id: str) -> AppSpec:
        app = self._apps.get(app_id)
        if app is None:
            raise ApiError(
                NOT_FOUND,
                f"no app with id {app_id!r}",
                hint="Find apps with apps_search.",
                recovery_tool="apps_search",
                allowed=sorted(self._apps),
            )
        return app

    def _action(self, app: AppSpec, name: str) -> ActionSpec:
        action = app.action(name)
        if action is None:
            raise ApiError(
                NOT_FOUND,
                f"app {app.id} has no action {name!r}",
                hint="Read the app's actions with apps_describe.",
                recovery_tool="apps_describe",
                allowed=[a.name for a in app.actions],
            )
        return action

    def describe(self, app_id: str, action: str | None = None) -> JsonDict:
        """The manifest: every action's summary, or one action in full with its input schema."""
        app = self._app(app_id)
        if action:
            actions = [self._action(app, action).public()]
        else:
            actions = [
                {"action": a.name, "description": a.description, "annotations": a.public()["annotations"]}
                for a in app.actions
            ]
        return {**app.summary(), "manifest_revision": app.revision, "actions": actions}

    # -- invoke ------------------------------------------------------------------------------
    async def invoke(
        self,
        app_id: str,
        action_name: str,
        *,
        manifest_revision: str,
        arguments: JsonDict,
        context: AppContext,
        confirm_destructive: bool = False,
    ) -> tuple[str, ActionResult]:
        app = self._app(app_id)
        action = self._action(app, action_name)
        if manifest_revision != app.revision:
            # The caller built its arguments from a schema that is no longer the current one.
            raise ApiError(
                CONFLICT,
                f"the manifest of app {app.id} changed (current revision {app.revision})",
                hint="Call apps_describe again and rebuild the arguments from the current schema.",
                recovery_tool="apps_describe",
                details={"manifest_revision": app.revision},
            )
        if not isinstance(arguments, dict):
            raise ApiError(INVALID_PARAMS, "arguments must be an object", hint="Pass a JSON object.")
        validator = Draft202012Validator(action.input_schema, registry=_NO_REMOTE_REFS)
        problem = best_match(validator.iter_errors(arguments))
        if problem is not None:
            where = ".".join(str(part) for part in problem.absolute_path) or "arguments"
            raise ApiError(
                INVALID_PARAMS,
                f"{where}: {problem.message[:200]}",
                hint="Fix the arguments to match the action's input_schema from apps_describe.",
                recovery_tool="apps_describe",
            )
        if action.destructive and confirm_destructive is not True:
            raise ApiError(
                CONFIRMATION_REQUIRED,
                f"{app.id}.{action.name} is destructive and needs confirm_destructive: true",
                hint="Ask the user first. Only if they agree, call again with confirm_destructive set to true.",
            )
        result = await action.handler(context, arguments)
        if result.status == "approval_required" or action.read_only:
            return "", result
        run_id = await self._record(app, action, arguments, result, context.principal)
        return run_id, result

    async def _record(
        self, app: AppSpec, action: ActionSpec, arguments: JsonDict, result: ActionResult, principal: Principal
    ) -> str:
        now = self._clock.now_ms()
        async with self._store.write_tx() as conn:
            if result.replayed and result.group_id:
                existing = (
                    await conn.execute(
                        select(app_runs.c.id).where(
                            app_runs.c.group_id == result.group_id, app_runs.c.workspace_id == principal.workspace_id
                        )
                    )
                ).scalar()
                if existing:
                    return str(existing)
            run_id = new_id("run", now)
            await conn.execute(
                app_runs.insert().values(
                    id=run_id,
                    workspace_id=principal.workspace_id,
                    app_id=app.id,
                    action=action.name,
                    manifest_revision=app.revision,
                    status=result.status,
                    input_json=arguments,
                    output_json=result.output,
                    credits=result.credits,
                    group_id=result.group_id,
                    created_at=now,
                    finished_at=now if result.status == "completed" else None,
                )
            )
        return run_id

    async def run(self, run_id: str, *, principal: Principal) -> JsonDict:
        row = None
        if is_id(run_id, "run"):
            async with self._store.read_tx() as conn:
                row = (
                    (
                        await conn.execute(
                            select(app_runs).where(
                                app_runs.c.id == run_id, app_runs.c.workspace_id == principal.workspace_id
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
        if row is None:
            raise ApiError(NOT_FOUND, f"no app run with id {run_id!r}", hint="Use the run_id returned by apps_invoke.")
        return dict(row)
