"""storyboard: a set of consistent stills from a shot list, as one long-running app action.

`create` starts one image job per shot and returns at once; `status` (an action like any
other) reports progress and results; `cancel` stops what has not finished. The jobs are
ordinary generation jobs: same budget gates, same refunds, visible in show_generations.
"""

from mcp_tools.core.appreg.spec import ActionResult, ActionSpec, AppContext, AppSpec, JsonDict
from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.core.jobs.service import GenerationRequest

TOOL = "apps_invoke.storyboard"


async def create(ctx: AppContext, args: JsonDict) -> ActionResult:
    style = " ".join(str(args.get("style") or "").split())
    requests = []
    for index, shot in enumerate(args["shots"]):
        prompt = " ".join(str(shot["prompt"]).split())
        params: JsonDict = {"model": args["model"], "prompt": f"{style}. {prompt}" if style else prompt}
        for name in ("aspect_ratio", "folder_id"):
            if args.get(name):
                params[name] = args[name]
        requests.append(GenerationRequest(index=index, params=params))
    outcome = await ctx.jobs.create(
        requests,
        principal=ctx.principal,
        tool=TOOL,
        output_type="image",
        max_credits=ctx.max_credits,
        idempotency_key=ctx.idempotency_key,
        strict=True,
    )
    if outcome.status == "approval_required":
        return ActionResult(
            {"estimated": outcome.credits_total, "limit": outcome.limit, "reason": outcome.reason},
            status="approval_required",
        )
    job_ids = [job.job_id for job in outcome.jobs]
    return ActionResult(
        {"shots": len(job_ids), "job_ids": job_ids, "next": "Call action 'status' with the run_id until done is true."},
        status="running",
        credits=outcome.credits_total,
        group_id=outcome.group_id,
        replayed=outcome.replayed,
    )


async def _run_jobs(ctx: AppContext, run_id: str) -> list[str]:
    assert ctx.load_run is not None
    run = await ctx.load_run(run_id)
    if run["app_id"] != "storyboard" or run["action"] != "create":
        raise ApiError(INVALID_PARAMS, f"{run_id} is not a storyboard run", hint="Use a run_id from storyboard create.")
    return list((run["output_json"] or {}).get("job_ids") or [])


async def status(ctx: AppContext, args: JsonDict) -> ActionResult:
    views = await ctx.jobs.get(await _run_jobs(ctx, args["run_id"]), principal=ctx.principal)
    shots = []
    for index, view in enumerate(views):
        shot: JsonDict = {"shot": index + 1, "job_id": view.job_id, "status": view.status}
        if view.results:
            shot["media_id"] = view.results[0].media_id
            shot["result"] = view.results[0].to_dict()
        if view.error:
            shot["error"] = view.error
        shots.append(shot)
    done = all(view.terminal for view in views)
    ready = [s["media_id"] for s in shots if "media_id" in s]
    output: JsonDict = {"done": done, "completed": len(ready), "total": len(views), "shots": shots}
    if done and len(ready) >= 2:
        output["next"] = "Show the stills in order; image_tools.contact_sheet makes one overview image of them."
    elif not done:
        output["next"] = "Not finished: call status again in a few seconds."
    return ActionResult(output, status="completed" if done else "running")


async def cancel(ctx: AppContext, args: JsonDict) -> ActionResult:
    refunded, stopped = 0, 0
    for job_id in await _run_jobs(ctx, args["run_id"]):
        _view, credits, was_active = await ctx.jobs.cancel(job_id, principal=ctx.principal)
        refunded += credits
        stopped += int(was_active)
    return ActionResult({"run_id": args["run_id"], "cancel_requested_for": stopped, "refunded_so_far": refunded})


_RUN = {
    "type": "object",
    "properties": {"run_id": {"type": "string", "description": "The run_id returned by create."}},
    "required": ["run_id"],
    "additionalProperties": False,
}

APP = AppSpec(
    id="storyboard",
    name="Storyboard",
    description="Generate one still per shot of a shot list in a shared style, as a single tracked run.",
    tags=("storyboard", "shots", "stills", "sequence", "video planning", "comic", "frames"),
    actions=(
        ActionSpec(
            "create",
            "Start a storyboard: one image job per shot (2-8), each prompt prefixed with the shared `style`. "
            "Spends credits like generate_batch; with status approval_required nothing was created. Returns a "
            "run_id immediately; follow it with the status action.",
            {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "An image model id from models_explore."},
                    "style": {
                        "type": "string",
                        "maxLength": 600,
                        "description": "What stays the same in every shot: subject, palette, lighting, look.",
                    },
                    "shots": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 2000}},
                            "required": ["prompt"],
                            "additionalProperties": False,
                        },
                    },
                    "aspect_ratio": {"type": "string"},
                    "folder_id": {"type": "string", "description": "Optional project folder (fld_...)."},
                },
                "required": ["model", "shots"],
                "additionalProperties": False,
            },
            create,
            spends=True,
        ),
        ActionSpec(
            "status", "Progress and results of a storyboard run, shot by shot. Read-only.", _RUN, status, read_only=True
        ),
        ActionSpec(
            "cancel",
            "Stop the unfinished shots of a run; they are refunded. Finished shots are kept.",
            _RUN,
            cancel,
            destructive=True,
        ),
    ),
)
