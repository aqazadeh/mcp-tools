"""The data directory: database, blobs, inbox and secret files.

Everything in it is private to the current user. The directory is created 0700, files are
created under a 0o077 umask, and the server refuses to start on a directory that someone
else owns or can write to.
"""

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


class DataDirError(RuntimeError):
    """The data directory is unsafe or unusable."""


@dataclass(frozen=True)
class DataDir:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "mcp-tools.sqlite"

    @property
    def blobs(self) -> Path:
        return self.root / "blobs"

    @property
    def inbox(self) -> Path:
        """The only default root `media_import_path` may read from."""
        return self.root / "inbox"

    @property
    def signing_key_file(self) -> Path:
        return self.root / "signing.key"

    @property
    def api_key_file(self) -> Path:
        return self.root / "api-key.sha256"


def _check_private(path: Path) -> None:
    info = path.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise DataDirError(f"{path} is owned by another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise DataDirError(f"{path} is writable by group or others; run: chmod 700 {path}")


def prepare_data_dir(root: Path) -> DataDir:
    """Create the directory tree with private permissions and verify it is safe to use."""
    os.umask(0o077)
    root = root.expanduser()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise DataDirError(f"cannot create data directory {root}: {exc}") from exc
    root = root.resolve()
    _check_private(root)
    data = DataDir(root)
    for sub in (data.blobs, data.inbox):
        sub.mkdir(mode=0o700, exist_ok=True)
        _check_private(sub)
    return data


def read_or_create_secret(path: Path, *, nbytes: int = 32) -> bytes:
    """Load a secret file, creating it atomically (0600, O_EXCL) on first use."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _check_private(path)
        secret = path.read_bytes()
        if len(secret) < 16:
            raise DataDirError(f"{path} is too short to be a secret; delete it to regenerate") from None
        return secret
    secret = secrets.token_bytes(nbytes)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    return secret


def write_private(path: Path, data: bytes) -> None:
    """Replace a file atomically with 0600 content."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
