"""Blob store, sniffing, probing and the media service."""

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from PIL import Image

from mcp_tools.blobs.base import BlobTooLarge
from mcp_tools.blobs.local import LocalBlobStore
from mcp_tools.core.clock import FakeClock
from mcp_tools.core.errors import ApiError
from mcp_tools.core.media import probe as probing
from mcp_tools.core.media.service import MediaLimits, MediaService, sanitize_filename
from mcp_tools.core.media.sniff import sniff
from mcp_tools.core.pagination import PageRequest, decode_cursor
from mcp_tools.core.principal import LOCAL, Principal
from mcp_tools.db.store import Store
from tests.helpers import media_samples as samples

pytestmark = pytest.mark.anyio


async def _stream(data: bytes, size: int = 7) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


@pytest.fixture
def blobs(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


@pytest.fixture
async def service(tmp_path: Path, blobs: LocalBlobStore) -> AsyncIterator[MediaService]:
    store = Store(tmp_path / "db.sqlite")
    await store.open()
    yield MediaService(store, blobs, FakeClock(), limits=MediaLimits(image_bytes=200_000))
    await store.close()


# -- blob store -------------------------------------------------------------------------------
async def test_blob_put_read_stat_delete(blobs: LocalBlobStore) -> None:
    data = bytes(range(256)) * 40
    stat = await blobs.put("media/ab/med_x.png", _stream(data, 999), max_bytes=len(data))
    assert stat.size == len(data) and len(stat.sha256) == 64
    assert await blobs.read_bytes("media/ab/med_x.png") == data
    assert b"".join([c async for c in blobs.open("media/ab/med_x.png", start=10, end=20)]) == data[10:20]
    assert (await blobs.stat("media/ab/med_x.png")) == stat
    path = blobs.local_path("media/ab/med_x.png")
    assert path is not None and path.suffix == ".png" and path.is_file()
    await blobs.delete("media/ab/med_x.png")
    assert await blobs.stat("media/ab/med_x.png") is None
    await blobs.delete("media/ab/med_x.png")  # deleting twice is fine


async def test_blob_size_cap_leaves_nothing_behind(blobs: LocalBlobStore) -> None:
    with pytest.raises(BlobTooLarge):
        await blobs.put("media/ab/med_big.png", _stream(b"x" * 1000), max_bytes=500)
    assert await blobs.stat("media/ab/med_big.png") is None
    assert list((blobs.root / "tmp").iterdir()) == []


@pytest.mark.parametrize(
    "key",
    [
        "../escape.png",
        "/abs/file.png",
        "media/../x.png",
        "media/ab/NAME.png",
        "noext",
        "tmp/x.part",
        "a/b/.hidden",
    ],
)
async def test_blob_keys_are_validated(blobs: LocalBlobStore, key: str) -> None:
    with pytest.raises(ValueError, match="invalid blob key"):
        blobs.local_path(key)


# -- sniffing ---------------------------------------------------------------------------------
def test_sniff_accepts_only_allowlisted_media() -> None:
    assert (s := sniff(samples.png_bytes())) and (s.kind, s.mime, s.ext) == ("image", "image/png", "png")
    assert (s := sniff(samples.jpeg_bytes())) and s.ext == "jpg"
    assert (s := sniff(samples.wav_bytes())) and (s.kind, s.ext) == ("audio", "wav")
    assert (s := sniff(samples.mp4_header_bytes())) and (s.kind, s.ext) == ("video", "mp4")
    assert (s := sniff(b"ID3\x04\x00" + b"\x00" * 20)) and s.ext == "mp3"
    assert (s := sniff(b"\x1a\x45\xdf\xa3" + b"\x01" * 20 + b"webm" + b"\x00" * 20)) and s.ext == "webm"
    for refused in (
        samples.SVG,
        samples.HTML,
        samples.PDF,
        samples.ZIP,
        b"",
        b"plain text",
        b"\x1a\x45\xdf\xa3matroska",
    ):
        assert sniff(refused) is None


# -- probing ----------------------------------------------------------------------------------
def test_probe_reads_dimensions_and_duration(tmp_path: Path) -> None:
    png = tmp_path / "a.png"
    png.write_bytes(samples.png_bytes(40, 30))
    wav = tmp_path / "a.wav"
    wav.write_bytes(samples.wav_bytes(seconds=0.5))
    assert probing.probe(png, "image", "png") == probing.Probe(width=40, height=30)
    assert probing.probe(wav, "audio", "wav").duration_ms == 500


def test_probe_rejects_fake_and_oversized_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 10)
    with pytest.raises(probing.ProbeError):
        probing.probe_image(fake)
    real = tmp_path / "real.png"
    real.write_bytes(samples.png_bytes(64, 64))
    monkeypatch.setattr(probing, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(probing.ProbeError, match="megapixels"):
        probing.probe_image(real)


def test_preview_is_a_small_jpeg(tmp_path: Path) -> None:
    source = tmp_path / "big.png"
    buffer = io.BytesIO()
    Image.effect_noise((1600, 1200), 80).convert("RGBA").save(buffer, format="PNG")
    source.write_bytes(buffer.getvalue())
    preview = probing.make_preview(source)
    assert preview[:3] == b"\xff\xd8\xff" and len(preview) <= probing.PREVIEW_MAX_BYTES
    with Image.open(io.BytesIO(preview)) as image:
        assert max(image.size) <= probing.PREVIEW_MAX_SIDE


# -- media service ----------------------------------------------------------------------------
async def test_ingest_stores_sniffed_type_and_metadata(service: MediaService) -> None:
    row = await service.ingest(
        _stream(samples.png_bytes(48, 32)),
        principal=LOCAL,
        source="upload",
        filename="../../evil\u202egnp.exe",
    )
    assert (row.kind, row.mime, row.status, row.source) == ("image", "image/png", "ready", "upload")
    assert (row.width, row.height, row.size_bytes) == (48, 32, len(samples.png_bytes(48, 32)))
    assert row.blob_key is not None and row.blob_key.endswith(f"{row.id}.png")  # extension from the sniff
    assert row.filename == "evilgnp.exe"  # path and bidi override stripped; display only
    path = service.local_path(row)
    assert path is not None and path.read_bytes() == samples.png_bytes(48, 32)
    assert (await service.get(row.id, principal=LOCAL)) == row


async def test_ingest_rejects_non_media_and_wrong_kind(service: MediaService, blobs: LocalBlobStore) -> None:
    for refused in (samples.SVG, samples.HTML, samples.PDF, b""):
        with pytest.raises(ApiError) as excinfo:
            await service.ingest(_stream(refused), principal=LOCAL, source="upload")
        assert excinfo.value.code == "invalid_params"
    with pytest.raises(ApiError, match="expected image"):
        await service.ingest(
            _stream(samples.wav_bytes()), principal=LOCAL, source="upload", accept=frozenset({"image"})
        )
    # a PNG signature followed by garbage passes the sniff but fails decoding: nothing is kept
    with pytest.raises(ApiError):
        await service.ingest(_stream(b"\x89PNG\r\n\x1a\n" + b"junk" * 50), principal=LOCAL, source="upload")
    assert (
        not [p for p in (blobs.root / "media").rglob("*") if p.is_file()]
        if (blobs.root / "media").exists()
        else True
    )


async def test_size_limit_applies_per_kind(service: MediaService) -> None:
    buffer = io.BytesIO()
    Image.effect_noise((900, 900), 90).convert("RGB").save(buffer, format="PNG")
    assert buffer.tell() > 200_000
    with pytest.raises(ApiError) as excinfo:
        await service.ingest(_stream(buffer.getvalue(), 65536), principal=LOCAL, source="upload")
    assert excinfo.value.code == "quota_exceeded"


async def test_media_is_scoped_to_the_workspace_and_listed_newest_first(service: MediaService) -> None:
    clock: FakeClock = service._clock  # type: ignore[assignment]
    rows = []
    for index in range(3):
        clock.advance(1)
        rows.append(
            await service.ingest(_stream(samples.png_bytes(8 + index, 8)), principal=LOCAL, source="upload")
        )
    await service.ingest(_stream(samples.wav_bytes()), principal=LOCAL, source="import_url")

    images = await service.list(PageRequest(limit=2), principal=LOCAL, kind="image")
    assert [r.id for r in images.items] == [rows[2].id, rows[1].id] and images.has_more
    rest = await service.list(
        PageRequest(limit=2, cursor=decode_cursor(images.next_cursor)), principal=LOCAL, kind="image"
    )
    assert [r.id for r in rest.items] == [rows[0].id] and not rest.has_more
    assert len((await service.list(PageRequest(), principal=LOCAL)).items) == 4

    stranger = Principal(workspace_id="ws_other")
    assert await service.get_many([rows[0].id], principal=stranger) == []
    with pytest.raises(ApiError) as excinfo:
        await service.get(rows[0].id, principal=stranger)
    assert excinfo.value.code == "not_found"
    assert await service.get_many(["job_notamediaid", "https://x.test/a.png"], principal=LOCAL) == []


async def test_job_output_is_registered_exactly_once(service: MediaService) -> None:
    first = await service.store_bytes(samples.png_bytes())
    second = await service.store_bytes(samples.png_bytes())
    async with service._store.write_tx() as conn:
        assert await service.insert(
            conn, first, principal=LOCAL, source="job_output", job_id="job_a", output_index=0
        )
        assert not await service.insert(
            conn, second, principal=LOCAL, source="job_output", job_id="job_a", output_index=0
        )
    by_job = await service.for_jobs(["job_a", "job_b"], principal=LOCAL)
    assert [m.id for m in by_job["job_a"]] == [first.media_id] and "job_b" not in by_job
    await service.discard_key(second.key)
    discarded = service._blobs.local_path(second.key)
    assert discarded is not None and not discarded.exists()


async def test_preview_only_for_images(service: MediaService) -> None:
    image = await service.ingest(_stream(samples.png_bytes(300, 200)), principal=LOCAL, source="upload")
    audio = await service.ingest(_stream(samples.wav_bytes()), principal=LOCAL, source="upload")
    preview = await service.preview(image)
    assert preview is not None and preview[:3] == b"\xff\xd8\xff"
    assert await service.preview(audio) is None


def test_filename_sanitising() -> None:
    assert sanitize_filename(None) is None and sanitize_filename("") is None
    assert sanitize_filename("C:\\Users\\me\\photo.png") == "photo.png"
    assert sanitize_filename("a\x00b\x1fc.png") == "abc.png"
    assert len(sanitize_filename("x" * 500) or "") == 128
