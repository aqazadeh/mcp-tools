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
    return ApiError(
        INVALID_PARAMS,
        "the path is outside the folders this server may read",
        hint="Ask the user to put the file in one of the allowed folders, then pass its full path.",
        allowed=[str(r) for r in roots],
    )


def open_confined(path: str, roots: Sequence[Path], max_bytes: int) -> tuple[int, Path, int]:
    """Return (file descriptor, resolved path, size). Blocking; call it in a worker thread."""
    if not path or "\x00" in path:
        raise ApiError(INVALID_PARAMS, "path is empty or malformed", hint="Pass the absolute path of one file.")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ApiError(INVALID_PARAMS, "path must be absolute", hint="Pass the full path of the file.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        # Do not reveal whether something exists outside the roots.
        raise ApiError(
            NOT_FOUND, "no readable file at that path", hint="Check the path.", allowed=[str(r) for r in roots]
        ) from None
    root = next((r for r in roots if resolved.is_relative_to(r) and resolved != r), None)
    if root is None:
        raise _refuse(roots)
    if any(part.startswith(".") for part in resolved.relative_to(root).parts):
        raise _refuse(roots)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError:
        raise ApiError(NOT_FOUND, "no readable file at that path", hint="Check the path and its permissions.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ApiError(INVALID_PARAMS, "the path is not a regular file", hint="Pass the path of one media file.")
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
