"""What a caller sees of a job and its outputs."""

from dataclasses import dataclass, field
from typing import Any

from mcp_tools.core.media.service import MediaRow

# `submitting` is an internal state; callers see it as `queued`.
PUBLIC_STATUS = {"submitting": "queued"}
TERMINAL = frozenset({"completed", "failed", "canceled", "moderated", "not_found"})


@dataclass(frozen=True)
class MediaView:
    media_id: str
    kind: str
    mime: str
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    filename: str | None = None
    local_path: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def media_view(row: MediaRow, *, local_path: str | None = None, url: str | None = None) -> MediaView:
    return MediaView(
        media_id=row.id,
        kind=row.kind,
        mime=row.mime,
        size_bytes=row.size_bytes,
        width=row.width,
        height=row.height,
        duration_s=round(row.duration_ms / 1000, 3) if row.duration_ms is not None else None,
        filename=row.filename,
        local_path=local_path,
        url=url,
    )


@dataclass(frozen=True)
class JobView:
    job_id: str
    status: str
    model: str | None = None
    output_type: str | None = None
    group_id: str | None = None
    index: int | None = None
    progress: float | None = None
    credits: int | None = None
    error: dict[str, Any] | None = None
    results: list[MediaView] = field(default_factory=list)
    created_at: int | None = None
    prompt: str | None = None
    next_poll_at: int | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"job_id": self.job_id, "status": self.status}
        for name in ("model", "output_type", "prompt", "progress", "credits", "error"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.created_at is not None:
            out["created_at_ms"] = self.created_at
        if self.results:
            out["results"] = [r.to_dict() for r in self.results]
        return out


def _brief_prompt(params: Any) -> str | None:
    """Enough of the prompt to tell jobs apart in a list."""
    prompt = params.get("prompt") if isinstance(params, dict) else None
    if not isinstance(prompt, str) or not prompt:
        return None
    return prompt if len(prompt) <= 120 else prompt[:117] + "..."


def job_view(row: Any, results: list[MediaView]) -> JobView:
    raw_status = str(row["status"])
    status = PUBLIC_STATUS.get(raw_status, raw_status)
    error = None
    if row["status"] in ("failed", "moderated", "canceled") and row["error_code"]:
        error = {"code": row["error_code"], "message": row["error_message"] or row["error_code"]}
    return JobView(
        job_id=row["id"],
        status=status,
        model=row["model_id"],
        output_type=row["output_type"],
        group_id=row["group_id"],
        index=row["group_index"],
        progress=row["progress"] if row["status"] not in ("queued", "submitting") else None,
        credits=row["credits"],
        error=error,
        results=results,
        created_at=row["created_at"],
        prompt=_brief_prompt(row["params_json"]),
        next_poll_at=row["next_poll_at"],
    )
