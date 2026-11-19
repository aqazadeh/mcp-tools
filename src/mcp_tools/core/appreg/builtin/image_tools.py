"""image_tools: local, free image operations (no provider, no credits)."""

import io
from pathlib import Path
from typing import Any

import anyio
from PIL import Image

from mcp_tools.core.appreg.spec import ActionResult, ActionSpec, AppContext, AppSpec, JsonDict
from mcp_tools.core.errors import INVALID_PARAMS, ApiError
from mcp_tools.core.media import probe

MAX_SIDE = 4096
_FORMATS = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
_IMAGE_ID = {"type": "string", "description": "A media id (med_...) of an image."}


async def _source(ctx: AppContext, media_id: str) -> Path:
    row = await ctx.media.get(media_id, principal=ctx.principal)
    path = ctx.media.local_path(row)
    if row.kind != "image" or row.status != "ready" or path is None:
        raise ApiError(INVALID_PARAMS, f"{media_id} is not a ready image", hint="Pass the media_id of an image.")
    return path


def _load(path: Path) -> Image.Image:
    try:
        with probe._open_image(path) as image:
            image.seek(0)
            return image.convert("RGBA")
    except probe.ProbeError as exc:
        raise ApiError(INVALID_PARAMS, "the image could not be read", hint="Use another image.") from exc


def _encode(image: Image.Image, fmt: str) -> bytes:
    buffer = io.BytesIO()
    if fmt == "jpeg":
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        flat.save(buffer, format="JPEG", quality=92, optimize=True)
    else:
        image.save(buffer, format=_FORMATS[fmt])
    return buffer.getvalue()


async def _store(ctx: AppContext, data: bytes, name: str) -> JsonDict:
    row = await ctx.media.ingest(_once(data), principal=ctx.principal, source="app", filename=name)
    return ctx.jobs.media_view(row).to_dict()


async def _once(data: bytes) -> Any:
    yield data


def _resize(path: Path, width: int | None, height: int | None, fit: str, fmt: str) -> bytes:
    image = _load(path)
    target_w = width or round(image.width * (height or image.height) / image.height)
    target_h = height or round(image.height * target_w / image.width)
    if fit == "stretch":
        out = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    else:
        scale = (max if fit == "cover" else min)(target_w / image.width, target_h / image.height)
        scaled = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS
        )
        if fit == "cover":
            left, top = (scaled.width - target_w) // 2, (scaled.height - target_h) // 2
            out = scaled.crop((left, top, left + target_w, top + target_h))
        else:
            out = scaled
    return _encode(out, fmt)


def _crop(path: Path, ratio: tuple[int, int], fmt: str) -> bytes:
    image = _load(path)
    wanted = ratio[0] / ratio[1]
    if image.width / image.height > wanted:
        new_w = max(1, round(image.height * wanted))
        left = (image.width - new_w) // 2
        box = (left, 0, left + new_w, image.height)
    else:
        new_h = max(1, round(image.width / wanted))
        top = (image.height - new_h) // 2
        box = (0, top, image.width, top + new_h)
    return _encode(image.crop(box), fmt)


def _sheet(paths: list[Path], columns: int, cell: int) -> bytes:
    rows = -(-len(paths) // columns)
    gap = max(4, cell // 40)
    sheet = Image.new("RGBA", (columns * cell + (columns + 1) * gap, rows * cell + (rows + 1) * gap), (24, 24, 24, 255))
    for index, path in enumerate(paths):
        tile = _load(path)
        tile.thumbnail((cell, cell), Image.Resampling.LANCZOS)
        x = gap + (index % columns) * (cell + gap) + (cell - tile.width) // 2
        y = gap + (index // columns) * (cell + gap) + (cell - tile.height) // 2
        sheet.paste(tile, (x, y), tile)
    return _encode(sheet, "png")


async def resize(ctx: AppContext, args: JsonDict) -> ActionResult:
    width, height = args.get("width"), args.get("height")
    if width is None and height is None:
        raise ApiError(INVALID_PARAMS, "pass width, height or both", hint="Sizes are in pixels.")
    fmt = args.get("format", "png")
    path = await _source(ctx, args["image_id"])
    data = await anyio.to_thread.run_sync(_resize, path, width, height, args.get("fit", "contain"), fmt)
    return ActionResult({"media": await _store(ctx, data, f"resized.{fmt}")})


async def crop(ctx: AppContext, args: JsonDict) -> ActionResult:
    try:
        w, h = (int(part) for part in str(args["aspect_ratio"]).split(":"))
        if w <= 0 or h <= 0:
            raise ValueError
    except ValueError:
        raise ApiError(INVALID_PARAMS, "aspect_ratio must look like 16:9", hint="Use W:H with whole numbers.") from None
    fmt = args.get("format", "png")
    data = await anyio.to_thread.run_sync(_crop, await _source(ctx, args["image_id"]), (w, h), fmt)
    return ActionResult({"media": await _store(ctx, data, f"cropped.{fmt}")})


async def convert(ctx: AppContext, args: JsonDict) -> ActionResult:
    fmt = args["format"]
    path = await _source(ctx, args["image_id"])
    data = await anyio.to_thread.run_sync(lambda: _encode(_load(path), fmt))
    return ActionResult({"media": await _store(ctx, data, f"converted.{fmt}")})


async def contact_sheet(ctx: AppContext, args: JsonDict) -> ActionResult:
    paths = [await _source(ctx, media_id) for media_id in args["image_ids"]]
    columns = min(args.get("columns") or min(4, len(paths)), len(paths))
    data = await anyio.to_thread.run_sync(_sheet, paths, columns, args.get("cell_size", 512))
    return ActionResult({"media": await _store(ctx, data, "contact-sheet.png"), "count": len(paths)})


_FORMAT = {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"}
_SIDE = {"type": "integer", "minimum": 1, "maximum": MAX_SIDE}


def _schema(properties: JsonDict, required: list[str]) -> JsonDict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


APP = AppSpec(
    id="image_tools",
    name="Image tools",
    description="Free, local image operations: resize, crop to an aspect ratio, convert format, contact sheet.",
    tags=("image", "resize", "crop", "convert", "format", "contact sheet", "grid", "collage", "free"),
    actions=(
        ActionSpec(
            "resize",
            "Resize an image to a width and/or height in pixels. fit: contain keeps the whole image, cover fills "
            "the box and crops the overflow, stretch ignores the proportions. Returns a new media item.",
            _schema(
                {
                    "image_id": _IMAGE_ID,
                    "width": _SIDE,
                    "height": _SIDE,
                    "fit": {"type": "string", "enum": ["contain", "cover", "stretch"], "default": "contain"},
                    "format": _FORMAT,
                },
                ["image_id"],
            ),
            resize,
        ),
        ActionSpec(
            "crop",
            "Centre-crop an image to an aspect ratio such as 1:1, 4:5 or 16:9. Returns a new media item.",
            _schema(
                {
                    "image_id": _IMAGE_ID,
                    "aspect_ratio": {"type": "string", "pattern": "^[0-9]{1,3}:[0-9]{1,3}$"},
                    "format": _FORMAT,
                },
                ["image_id", "aspect_ratio"],
            ),
            crop,
        ),
        ActionSpec(
            "convert",
            "Re-encode an image as png, jpeg or webp (jpeg flattens transparency onto white). Returns a new media "
            "item.",
            _schema(
                {"image_id": _IMAGE_ID, "format": {"type": "string", "enum": ["png", "jpeg", "webp"]}},
                ["image_id", "format"],
            ),
            convert,
        ),
        ActionSpec(
            "contact_sheet",
            "Lay 2 to 12 images out on one grid image, in the order given - useful to show a set of options or the "
            "shots of a storyboard at a glance. Returns a new media item.",
            _schema(
                {
                    "image_ids": {"type": "array", "items": _IMAGE_ID, "minItems": 2, "maxItems": 12},
                    "columns": {"type": "integer", "minimum": 1, "maximum": 6},
                    "cell_size": {"type": "integer", "minimum": 64, "maximum": 1024, "default": 512},
                },
                ["image_ids"],
            ),
            contact_sheet,
        ),
    ),
)
