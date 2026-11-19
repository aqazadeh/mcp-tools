"""Open a local file named by a tool caller, confined to the configured import roots.

The caller is a language model that may be steered by text it read somewhere, so a path is
untrusted. A file is readable only if its fully resolved location lies under an allowed
root, none of its components below that root is hidden, and it is a regular file within
the size limit. The descriptor is opened with O_NOFOLLOW and everything after that is read
from the descriptor, so swapping the path afterwards changes nothing.
"""

import os
import stat
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio

from mcp_tools.core.errors import INVALID_PARAMS, NOT_FOUND, QUOTA_EXCEEDED, ApiError

CHUNK = 256 * 1024


class RootError(ValueError):
    """An import root in the configuration is not acceptable."""


def validate_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    """Resolve the configured roots; refuse `/` and the home directory itself."""
    home = Path.home().resolve()
    out: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved == Path(resolved.anchor) or resolved == home:
            raise RootError(f"{root} is too broad to be an import root; name a dedicated folder")
        if resolved not in out:
            out.append(resolved)
    return tuple(out)


def _refuse(roots: Sequence[Path]) -> ApiError:
    # One answer for "missing", "outside the roots" and "hidden": a caller must not be able to
    # use this tool to find out which files exist elsewhere on the disk.
    return ApiError(
        NOT_FOUND,
        "no importable file at that path",
        hint="The file must exist inside one of the allowed folders; pass its full path.",
        allowed=[str(r) for r in roots],
    )


def _open_beneath(root: Path, parts: Sequence[str]) -> int:
    """Open root/parts without following a symlink in any component.

    The path was already resolved, so it holds no symlinks; if one shows up now, something
    swapped a directory between the check and the open, and the open fails.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    last_flags = os.O_RDONLY | nofollow | cloexec | getattr(os, "O_NONBLOCK", 0)
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        return os.open(root.joinpath(*parts), last_flags)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | cloexec)
    try:
        for part in parts[:-1]:
            deeper = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow | cloexec, dir_fd=directory)
            os.close(directory)
            directory = deeper
        return os.open(parts[-1], last_flags, dir_fd=directory)
    finally:
        os.close(directory)


def open_confined(path: str, roots: Sequence[Path], max_bytes: int) -> tuple[int, Path, int]:
    """Return (file descriptor, resolved path, size). Blocking; call it in a worker thread."""
    if not path or "\x00" in path:
        raise ApiError(INVALID_PARAMS, "path is empty or malformed", hint="Pass the absolute path of one file.")
    candidate = Path(path)  # no ~ expansion: that would turn the tool into a user-name probe
    if not candidate.is_absolute():
        raise ApiError(INVALID_PARAMS, "path must be absolute", hint="Pass the full path of the file.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _refuse(roots) from None
    root = next((r for r in roots if resolved.is_relative_to(r) and resolved != r), None)
    if root is None:
        raise _refuse(roots)
    parts = resolved.relative_to(root).parts
    if any(part.startswith(".") for part in parts):
        raise _refuse(roots)
    try:
        fd = _open_beneath(root, parts)
    except OSError:
        raise _refuse(roots) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ApiError(INVALID_PARAMS, "the path is not a regular file", hint="Pass the path of one media file.")
        if info.st_nlink > 1:  # a hard link may be a second name for a file that lives outside the roots
            raise _refuse(roots)
        if info.st_size > max_bytes:
            raise ApiError(
                QUOTA_EXCEEDED,
                f"the file is larger than the {max_bytes // (1024 * 1024)} MB limit",
                hint="Use a smaller file.",
            )
    except BaseException:
        os.close(fd)
        raise
    return fd, resolved, info.st_size


async def read_fd(fd: int, *, max_bytes: int) -> AsyncIterator[bytes]:
    """Yield the descriptor's content and close it. Stops with an error past `max_bytes`."""
    try:
        received = 0
        while True:
            chunk = await anyio.to_thread.run_sync(os.read, fd, CHUNK)
            if not chunk:
                return
            received += len(chunk)
            if received > max_bytes:  # the file grew after fstat
                raise ApiError(QUOTA_EXCEEDED, "the file is larger than the size limit", hint="Use a smaller file.")
            yield chunk
    finally:
        os.close(fd)
