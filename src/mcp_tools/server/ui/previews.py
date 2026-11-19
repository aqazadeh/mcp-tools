"""Small image previews for the results widget.

The widget can ask for previews itself through the app-only `media_previews` tool, but some
hosts render the widget without forwarding its own tool calls. Results of the tools that open
the widget therefore carry the first previews in `_meta`, which the host hands to the widget
with the result and does not show to the model.
"""

import base64
from collections.abc import Iterable, Mapping
from typing import Any

from mcp.server.apps import client_supports_apps
from mcp.server.mcpserver import Context

from mcp_tools.core.platform import Platform

META_KEY = "mcp-tools/previews"
PREVIEW_MAX_SIDE = 320
PREVIEW_MAX_BYTES = 36_000
# Previews riding along with one result: enough for a generate call (count <= 4) and the first
# row of a history page, while keeping the response far below the envelope's size budget.
MAX_INLINE_PREVIEWS = 6


def shows_widget(platform: Platform, ctx: Context) -> bool:
    """True when the client renders the results widget, i.e. the user already sees the results."""
    try:
        return platform.settings.ui_enabled and client_supports_apps(ctx)
    except Exception:
        return False


async def preview_data_urls(platform: Platform, media_ids: Iterable[str]) -> dict[str, str | None]:
    """JPEG data URLs for ready images, keyed by media id; None where no preview can be made."""
    rows = await platform.media.get_many(list(media_ids), principal=platform.principal)
    out: dict[str, str | None] = {}
    for row in rows:
        data = None
        if row.status == "ready":
            data = await platform.media.preview(row, max_side=PREVIEW_MAX_SIDE, max_bytes=PREVIEW_MAX_BYTES)
        out[row.id] = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii") if data else None
    return out


def _image_ids(jobs: Iterable[Mapping[str, Any]]) -> list[str]:
    ids: list[str] = []
    for job in jobs:
        for media in job.get("results") or ():
            media_id = media.get("media_id") if isinstance(media, Mapping) else None
            if media.get("kind") == "image" and isinstance(media_id, str) and media_id not in ids:
                ids.append(media_id)
    return ids


async def inline_previews(platform: Platform, ctx: Context, jobs: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """`_meta` for a widget-opening result: previews of its first images, or None.

    Never fails: previews are a convenience, and the result they ride on may describe jobs
    that are already paid for.
    """
    if not shows_widget(platform, ctx):
        return None
    try:
        ids = _image_ids(jobs)[:MAX_INLINE_PREVIEWS]
        if not ids:
            return None
        urls = {k: v for k, v in (await preview_data_urls(platform, ids)).items() if v}
    except Exception:
        return None
    return {META_KEY: urls} if urls else None
