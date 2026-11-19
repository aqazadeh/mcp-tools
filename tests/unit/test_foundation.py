"""ids, clock, data directory and log redaction."""

import logging
import os
import stat
from pathlib import Path

import pytest

from mcp_tools.core.clock import FakeClock, SystemClock, day_of
from mcp_tools.core.datadir import DataDirError, prepare_data_dir, read_or_create_secret
from mcp_tools.core.ids import PREFIXES, id_pattern, id_prefix, is_id, new_id, shard_of
from mcp_tools.core.logsafe import RedactingFilter, redact, register_secret


def test_ids_are_typed_sortable_and_well_formed() -> None:
    older = new_id("job", 1_000)
    newer = new_id("job", 2_000)
    assert older < newer  # lexicographic order follows time
    assert len(older) == 30 and older.startswith("job_")
    assert id_prefix(older) == "job" and is_id(older, "job") and not is_id(older, "med")
    assert len({new_id("med", 5) for _ in range(200)}) == 200
    import re

    assert re.match(id_pattern("job"), older)
    for bad in (
        "job_",
        "job_UPPERCASE0000000000000000",
        "xyz_" + older[4:],
        older + "0",
        None,
        7,
        "https://x.test/a",
    ):
        assert id_prefix(bad) is None
    assert set(PREFIXES) >= {"job", "med", "prj", "fld", "ast", "run"}


def test_shard_comes_from_the_random_tail() -> None:
    ids = [new_id("med", 1_800_000_000_000) for _ in range(300)]
    assert len({i[4:6] for i in ids}) == 1  # the leading characters are time: useless as a shard
    assert len({shard_of(i) for i in ids}) > 50


def test_clocks() -> None:
    clock = FakeClock(start_ms=1_000)
    clock.advance(1.5)
    assert clock.now_ms() == 2_500 and clock.monotonic() == 2.5
    assert abs(SystemClock().now_ms() / 1000 - __import__("time").time()) < 5
    assert day_of(0) == "1970-01-01" and day_of(86_400_000) == "1970-01-02"


def test_data_dir_is_created_private(tmp_path: Path) -> None:
    data = prepare_data_dir(tmp_path / "nested" / "data")
    for path in (data.root, data.blobs, data.inbox):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert data.database.parent == data.root
    # a second call on the same directory is fine
    assert prepare_data_dir(data.root).root == data.root


def test_group_or_world_writable_data_dir_is_refused(tmp_path: Path) -> None:
    unsafe = tmp_path / "shared"
    unsafe.mkdir()
    os.chmod(unsafe, 0o777)
    with pytest.raises(DataDirError, match="writable by group or others"):
        prepare_data_dir(unsafe)


def test_secret_file_is_created_once_with_private_mode(tmp_path: Path) -> None:
    data = prepare_data_dir(tmp_path / "data")
    first = read_or_create_secret(data.signing_key_file)
    second = read_or_create_secret(data.signing_key_file)
    assert first == second and len(first) == 32
    assert stat.S_IMODE(data.signing_key_file.stat().st_mode) == 0o600
    data.signing_key_file.write_bytes(b"short")
    with pytest.raises(DataDirError, match="too short"):
        read_or_create_secret(data.signing_key_file)


def test_redaction_masks_tokens_queries_and_registered_secrets() -> None:
    register_secret("sk-live-1234567890abcdef")
    text = (
        "GET https://files.example.test/out.png?X-Sig=abc123&exp=9 "
        "Authorization: Bearer abcdefghijklmnop key sk-live-1234567890abcdef api_key=zzzzzzzz"
    )
    cleaned = redact(text)
    for leaked in ("abc123", "abcdefghijklmnop", "sk-live-1234567890abcdef", "zzzzzzzz"):
        assert leaked not in cleaned
    assert "https://files.example.test/out.png?***" in cleaned


def test_redacting_filter_rewrites_log_records() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", ("supersecretvalue",), None)
    assert RedactingFilter().filter(record) is True
    assert "supersecretvalue" not in record.getMessage()
    plain = logging.LogRecord("x", logging.INFO, __file__, 1, "nothing to hide %d", (3,), None)
    RedactingFilter().filter(plain)
    assert plain.getMessage() == "nothing to hide 3"
