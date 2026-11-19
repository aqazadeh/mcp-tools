"""media_import_path may only read regular, visible files under the allowed roots."""

import os
from pathlib import Path

import pytest

from mcp_tools.core.errors import ApiError
from mcp_tools.core.media.paths import RootError, open_confined, validate_roots

LIMIT = 1000


@pytest.fixture
def root(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "ok.png").write_bytes(b"data")
    (inbox / "sub").mkdir()
    (inbox / "sub" / "deep.png").write_bytes(b"deep")
    (inbox / ".hidden").mkdir()
    (inbox / ".hidden" / "x.png").write_bytes(b"x")
    (inbox / ".env").write_bytes(b"SECRET=1")
    (tmp_path / "outside.png").write_bytes(b"outside")
    return inbox.resolve()


def read(path: Path | str, root: Path, limit: int = LIMIT) -> bytes:
    fd, _resolved, size = open_confined(str(path), [root], limit)
    try:
        return os.read(fd, size + 1)
    finally:
        os.close(fd)


def test_files_under_the_root_are_readable(root: Path) -> None:
    assert read(root / "ok.png", root) == b"data"
    assert read(root / "sub" / "deep.png", root) == b"deep"
    assert read(root / "sub" / ".." / "ok.png", root) == b"data"


@pytest.mark.parametrize(
    "relative",
    ["../outside.png", "sub/../../outside.png", ".env", ".hidden/x.png", "", "missing.png", "sub", "."],
)
def test_everything_else_is_refused(root: Path, relative: str) -> None:
    with pytest.raises(ApiError):
        read(root / relative if relative else "", root)


def test_relative_paths_null_bytes_and_other_trees_are_refused(root: Path) -> None:
    for bad in ("ok.png", "inbox/ok.png", str(root / "ok.png") + "\x00", "/etc/passwd", "/proc/self/environ"):
        with pytest.raises(ApiError):
            read(bad, root)


def test_symlinks_cannot_leave_the_root(root: Path, tmp_path: Path) -> None:
    (root / "link.png").symlink_to(tmp_path / "outside.png")
    (root / "dirlink").symlink_to(tmp_path)
    (root / "inside.png").symlink_to(root / "ok.png")
    with pytest.raises(ApiError):
        read(root / "link.png", root)
    with pytest.raises(ApiError):
        read(root / "dirlink" / "outside.png", root)
    assert read(root / "inside.png", root) == b"data"  # resolves to a file that is inside


def test_a_root_that_is_itself_reached_through_a_symlink_still_confines(root: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    (resolved,) = validate_roots([alias])
    assert resolved == root
    assert read(alias / "ok.png", resolved) == b"data"


def test_size_limit_and_special_files(root: Path) -> None:
    with pytest.raises(ApiError) as too_big:
        read(root / "ok.png", root, limit=3)
    assert too_big.value.code == "quota_exceeded"
    os.mkfifo(root / "pipe")
    with pytest.raises(ApiError, match="regular file"):
        read(root / "pipe", root)


def test_errors_list_the_allowed_roots_but_not_what_exists_outside(root: Path, tmp_path: Path) -> None:
    with pytest.raises(ApiError) as outside:
        read(tmp_path / "outside.png", root)
    with pytest.raises(ApiError) as missing:
        read(tmp_path / "does-not-exist.png", root)
    assert outside.value.allowed == [str(root)]
    assert outside.value.message == missing.value.message and outside.value.code == missing.value.code


def test_overbroad_roots_are_rejected() -> None:
    for bad in (Path("/"), Path.home()):
        with pytest.raises(RootError):
            validate_roots([bad])


def test_hard_links_and_swapped_directories_are_refused(root: Path, tmp_path: Path) -> None:
    os.link(tmp_path / "outside.png", root / "alias.png")  # a second name for a file outside the root
    with pytest.raises(ApiError):
        read(root / "alias.png", root)

    from mcp_tools.core.media import paths

    # A directory replaced by a symlink between the check and the open must not be followed.
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "deep.png").write_bytes(b"stolen")
    (root / "sub").rename(root / "sub.real")
    (root / "sub").symlink_to(tmp_path / "elsewhere")
    with pytest.raises((NotADirectoryError, OSError), match=r"(?i)not a directory|symbolic|loop"):
        os.close(paths._open_beneath(root, ("sub", "deep.png")))


def test_missing_outside_and_hidden_look_the_same(root: Path, tmp_path: Path) -> None:
    errors = []
    for path in (tmp_path / "outside.png", tmp_path / "missing.png", root / ".env", root / "missing.png", "~root/x"):
        with pytest.raises(ApiError) as caught:
            read(path, root)
        errors.append((caught.value.code, caught.value.message) if str(path) != "~root/x" else None)
    assert len({e for e in errors if e}) == 1
