"""Media family: bring files in, look at them, list them."""

import base64
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ContentBlock, ImageContent, ToolAnnotations
from pydantic import Field

from mcp_tools.core.pagination import page_request
from mcp_tools.server.envelope import ok, platform_of
from mcp_tools.server.params import Wire, choice, optional_choice
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
