"""Media family: bring files in, look at them, list them."""

import base64
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ContentBlock, ImageContent, ToolAnnotations
from pydantic import Field

from mcp_tools.core.errors import INVALID_PARAMS, UNSUPPORTED, ApiError
from mcp_tools.core.pagination import page_request
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import NextStep, Wire, choice, optional_choice
from mcp_tools.server.registry import ToolDef

KINDS = ("image", "video", "audio")


class MediaResult(Wire):
    kind: Literal["media"] = "media"
    schema_version: int = 1
    media: dict[str, Any]
    preview: str | None = Field(
        default=None, description="attached: an image follows; unavailable: none for this kind."
    )
    message: str | None = None


class MediaListResult(Wire):
    kind: Literal["media_list"] = "media_list"
    schema_version: int = 1
    items: list[dict[str, Any]]
    has_more: bool = False
    next_cursor: str | None = None


_USE = "Pass media_id as a medias value in a generate tool."


async def media_import_url(
    ctx: Context,
    url: Annotated[str, Field(description="Public https URL of one image, video or audio file.", max_length=2000)],
) -> Annotated[CallToolResult, MediaResult]:
    platform = platform_of(ctx)
    row = await platform.importer.from_url(url, principal=platform.principal)
    return ok(MediaResult(media=platform.jobs.media_view(row).to_dict(), message=_USE))


async def media_import_path(
    ctx: Context,
    path: Annotated[
        str, Field(description="Absolute path of one file inside a folder this server may read.", max_length=4096)
    ],
) -> Annotated[CallToolResult, MediaResult]:
    platform = platform_of(ctx)
    row = await platform.importer.from_path(path, principal=platform.principal)
    return ok(MediaResult(media=platform.jobs.media_view(row).to_dict(), message=_USE))


async def media_get(
    ctx: Context,
    media_id: Annotated[str, Field(description="A media id (med_...).", max_length=64)],
    include_preview: Annotated[
        bool, Field(description="Attach a small image so you can look at it (images only).")
    ] = False,
) -> Annotated[CallToolResult, MediaResult]:
    platform = platform_of(ctx)
    row = await platform.media.get(media_id, principal=platform.principal)
    view = platform.jobs.media_view(row).to_dict()
    if not include_preview:
        return ok(MediaResult(media=view))
    data = await platform.media.preview(row)
    if data is None:
        return ok(MediaResult(media=view, preview="unavailable", message="Previews exist for images only."))
    content: list[ContentBlock] = [
        ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/jpeg")
    ]
    return ok(MediaResult(media=view, preview="attached"), content=content)


async def media_list(
    ctx: Context,
    kind: Annotated[str, choice("Only this kind; omit for all.", KINDS)] = "",
    source: Annotated[
        str, choice("imported: files brought in; generated: job outputs. Omit for both.", ("imported", "generated"))
    ] = "",
    limit: Annotated[int, Field(description="Page size, 1-50 (default 20).")] = 0,
    cursor: Annotated[str, Field(description="next_cursor from the previous page; keep the same filters.")] = "",
) -> Annotated[CallToolResult, MediaListResult]:
    platform = platform_of(ctx)
    wanted_source = optional_choice("source", source, ("imported", "generated"))
    page = await platform.media.list(
        page_request(limit, cursor),
        principal=platform.principal,
        kind=optional_choice("kind", kind, KINDS),
        generated=None if wanted_source is None else wanted_source == "generated",
    )
    return ok(
        MediaListResult(
            items=[platform.jobs.media_view(row).to_dict() for row in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )
    )


UPLOAD_TTL_SECONDS = 15 * 60
MAX_UPLOAD_FILES = 8


class UploadFile(Wire):
    filename: str = Field(default="", description="Original file name, for display only.", max_length=255)


class UploadSlot(Wire):
    media_id: str
    upload_url: str
    method: str = "PUT"
    filename: str | None = None


class UploadResult(Wire):
    kind: Literal["upload"] = "upload"
    schema_version: int = 1
    uploads: list[UploadSlot]
    expires_in_seconds: int
    max_bytes: int
    next: NextStep
    message: str


class ConfirmResult(Wire):
    kind: Literal["upload_confirm"] = "upload_confirm"
    schema_version: int = 1
    items: list[dict[str, Any]]
    all_ready: bool
    message: str | None = None


async def media_upload(
    ctx: Context,
    files: Annotated[list[UploadFile], Field(description="1-8 files to upload; one upload link is returned for each.")],
) -> Annotated[CallToolResult, UploadResult]:
    platform = platform_of(ctx)
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise ApiError(INVALID_PARAMS, f"pass 1 to {MAX_UPLOAD_FILES} files", hint="Upload larger sets in groups.")
    max_bytes = platform.media.limits.largest
    slots = []
    for item in files:
        media_id, nonce, _expires = await platform.media.reserve_upload(
            principal=platform.principal, filename=item.filename or None, ttl_seconds=UPLOAD_TTL_SECONDS
        )
        url = platform.urls.upload_url(media_id, ttl_seconds=UPLOAD_TTL_SECONDS, max_bytes=max_bytes, nonce=nonce)
        if url is None:
            raise ApiError(UNSUPPORTED, "uploads are not available on this server", hint="Use media_import_url.")
        slots.append(UploadSlot(media_id=media_id, upload_url=url, filename=item.filename or None))
    return ok(
        UploadResult(
            uploads=slots,
            expires_in_seconds=UPLOAD_TTL_SECONDS,
            max_bytes=max_bytes,
            next=NextStep(tool="media_confirm", hint="After every PUT succeeded, pass the media_ids."),
            message="Send each file's raw bytes with one HTTP PUT to its upload_url (no auth header, no form "
            "encoding), e.g. curl -T <file> '<upload_url>'. Each link works once.",
        )
    )


async def media_confirm(
    ctx: Context,
    media_ids: Annotated[list[str], Field(description="1-8 media ids returned by media_upload.")],
) -> Annotated[CallToolResult, ConfirmResult]:
    platform = platform_of(ctx)
    if not media_ids or len(media_ids) > MAX_UPLOAD_FILES:
        raise ApiError(INVALID_PARAMS, f"pass 1 to {MAX_UPLOAD_FILES} media ids", hint="Confirm larger sets in groups.")
    ids = [str(m) for m in media_ids]
    statuses = await platform.media.confirm_uploads(ids, principal=platform.principal)
    rows = {row.id: row for row in await platform.media.get_many(ids, principal=platform.principal)}
    items: list[dict[str, Any]] = []
    for media_id in ids:
        status = statuses[media_id]
        item: dict[str, Any] = {"media_id": media_id, "status": status}
        if status == "ready" and media_id in rows:
            item = {**platform.jobs.media_view(rows[media_id]).to_dict(), "status": status}
        items.append(item)
    all_ready = all(item["status"] == "ready" for item in items)
    return ok(
        ConfirmResult(
            items=items,
            all_ready=all_ready,
            message=None if all_ready else "not_uploaded: the PUT has not succeeded yet; upload, then confirm again.",
        )
    )


MAX_PREVIEWS = 8


class PreviewsResult(Wire):
    kind: Literal["media_previews"] = "media_previews"
    schema_version: int = 1
    items: list[dict[str, Any]]


async def media_previews(
    ctx: Context,
    media_ids: Annotated[list[str], Field(description="1-8 media ids.")],
) -> Annotated[CallToolResult, PreviewsResult]:
    platform = platform_of(ctx)
    if not media_ids or len(media_ids) > MAX_PREVIEWS:
        raise ApiError(INVALID_PARAMS, f"pass 1 to {MAX_PREVIEWS} media ids", hint="Ask in smaller groups.")
    rows = await platform.media.get_many([str(m) for m in media_ids], principal=platform.principal)
    items: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {"media_id": row.id, "kind": row.kind}
        data = await platform.media.preview(row, max_side=320, max_bytes=36_000) if row.status == "ready" else None
        if data is not None:
            item["data_url"] = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        items.append(item)
    return ok(PreviewsResult(items=items))


_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def tools() -> list[ToolDef]:
    return [
        ToolDef(
            name="media_import_url",
            family="media",
            handler=media_import_url,
            title="Import media from URL",
            description=(
                "Download one image, video or audio file from a public https URL into the media store and "
                "return its media_id. Use it whenever the user gives a link: generate tools take ids, never "
                "URLs. The file type is detected from its content; web pages, private or local addresses and "
                "oversized files are refused. Costs no credits. Each call stores a new copy."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
            ),
            effects=frozenset({"write", "network"}),
        ),
        ToolDef(
            name="media_import_path",
            family="media",
            handler=media_import_path,
            title="Import media from a local file",
            description=(
                "Copy one image, video or audio file from this computer into the media store and return its "
                "media_id. Only folders the server operator allowed can be read (the error lists them; by "
                "default the inbox folder in the data directory). Use it when the user names a local file; "
                "never use it to explore the disk. Costs no credits."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
            ),
            effects=frozenset({"write", "filesystem"}),
            requires=frozenset({"local_paths"}),
        ),
        ToolDef(
            name="media_upload",
            family="media",
            handler=media_upload,
            title="Upload media",
            description=(
                "Get one-time upload links for files you can read yourself (e.g. in a sandbox). For each file "
                "PUT the raw bytes to its upload_url, then call media_confirm with the media_ids; only then can "
                "the ids be used as medias values. Links expire in 15 minutes. For a file that is already at a "
                "public URL use media_import_url instead. Costs no credits."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
            ),
            effects=frozenset({"write"}),
            requires=frozenset({"url_issuer"}),
        ),
        ToolDef(
            name="media_confirm",
            family="media",
            handler=media_confirm,
            title="Confirm uploads",
            description=(
                "Finish uploads started with media_upload: checks that each file arrived, detects its type "
                "from the content and marks it ready. Call it once after the PUTs succeeded. Items reported "
                "as not_uploaded have not received their file yet. Safe to repeat."
            ),
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
            ),
            effects=frozenset({"write"}),
            requires=frozenset({"url_issuer"}),
        ),
        ToolDef(
            name="media_get",
            family="media",
            handler=media_get,
            title="Get media",
            description=(
                "Read one media item: kind, type, size, dimensions or duration, and where the file is "
                "(local_path or a fresh short-lived url). Read-only. Set include_preview to look at an image "
                "yourself before judging or describing it; previews are small and exist for images only."
            ),
            annotations=_READ,
            visibility=("model", "app"),
        ),
        ToolDef(
            name="media_previews",
            family="media",
            handler=media_previews,
            title="Media previews",
            description="Small image previews for the results viewer. Not for the model: use media_get instead.",
            annotations=_READ,
            visibility=("app",),
            app_helper=True,
            requires=frozenset({"ui"}),
        ),
        ToolDef(
            name="media_list",
            family="media",
            handler=media_list,
            title="List media",
            description=(
                "List stored media, newest first: imported files and generation outputs. Read-only. Filter by "
                "kind or source. Use it to find the id of a file imported or generated earlier. Pages with "
                "limit and cursor."
            ),
            annotations=_READ,
        ),
    ]
