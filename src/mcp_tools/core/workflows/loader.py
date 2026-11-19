"""Workflow bundles: instructions shipped as data.

A bundle is a folder with a `SKILL.md` (YAML front matter: name, description, version) and
optional reference files. The model reads it through tools and follows it; nothing in a
bundle is ever executed.

A bundle speaks to the model with the user's authority, so where bundles come from matters:
only the ones shipped with this package and folders the operator named in the configuration
are loaded (never the working directory), a user folder must belong to the user and not be
writable by anyone else, and user bundles live in their own `user/` namespace so they cannot
replace a built-in one. Everything is read once at start-up and served from memory; a file
is looked up by its manifest key, never by joining path strings from a request.
"""

import hashlib
import os
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from mcp_tools.core.yamlsafe import YamlError, load_yaml

MANIFEST = "SKILL.md"
ALLOWED_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml")
MAX_FILES = 64
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 4
USER_PREFIX = "user/"
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn"}  # control, format (bidi, zero-width, tags), unassigned
_ALLOWED_CONTROLS = {"\n", "\t", "\r"}


class WorkflowError(ValueError):
    """A bundle is malformed or comes from an unsafe place."""


@dataclass(frozen=True)
class WorkflowFile:
    path: str
    text: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    version: str
    instructions: str
    files: dict[str, WorkflowFile]
    digest: str
    builtin: bool

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "version": self.version}


def check_text(text: str, where: str) -> None:
    """Refuse invisible or direction-changing characters: they can hide instructions from a reviewer."""
    for char in text:
        if char in _ALLOWED_CONTROLS:
            continue
        if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            raise WorkflowError(f"{where}: contains a control or invisible character (U+{ord(char):04X})")


def _front_matter(text: str, where: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise WorkflowError(f"{where}: must start with YAML front matter (--- name / description / version ---)")
    try:
        _, header, body = text.split("---", 2)
        meta = load_yaml(header)
    except (ValueError, YamlError) as exc:
        raise WorkflowError(f"{where}: invalid front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise WorkflowError(f"{where}: front matter must be a mapping")
    return meta, body.strip()


def _walk(root: Traversable, prefix: str = "", depth: int = 0) -> Iterable[tuple[str, Traversable]]:
    if depth > MAX_DEPTH:
        raise WorkflowError(f"{prefix}: bundle folders are nested too deeply")
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        name = entry.name
        if name.startswith(".") or name == "__pycache__":
            continue
        if isinstance(entry, Path) and entry.is_symlink():
            raise WorkflowError(f"{prefix}{name}: symbolic links are not allowed in a bundle")
        if entry.is_dir():
            yield from _walk(entry, f"{prefix}{name}/", depth + 1)
        elif entry.is_file():
            yield f"{prefix}{name}", entry


def load_bundle(root: Traversable, *, folder_name: str, builtin: bool) -> Workflow:
    files: dict[str, WorkflowFile] = {}
    total = 0
    for relative, entry in _walk(root):
        if not relative.lower().endswith(ALLOWED_SUFFIXES):
            raise WorkflowError(f"{folder_name}/{relative}: only {', '.join(ALLOWED_SUFFIXES)} files are allowed")
        raw = entry.read_bytes()
        total += len(raw)
        if len(files) >= MAX_FILES or total > MAX_BUNDLE_BYTES:
            raise WorkflowError(f"{folder_name}: a bundle may hold {MAX_FILES} files and {MAX_BUNDLE_BYTES} bytes")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowError(f"{folder_name}/{relative}: not valid UTF-8") from exc
        check_text(text, f"{folder_name}/{relative}")
        files[relative] = WorkflowFile(relative, text, len(raw), hashlib.sha256(raw).hexdigest())
    manifest = files.pop(MANIFEST, None)
    if manifest is None:
        raise WorkflowError(f"{folder_name}: missing {MANIFEST}")
    meta, body = _front_matter(manifest.text, f"{folder_name}/{MANIFEST}")
    name, description = str(meta.get("name") or ""), " ".join(str(meta.get("description") or "").split())
    if name != folder_name:
        raise WorkflowError(f"{folder_name}/{MANIFEST}: name must equal the folder name, got {name!r}")
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in name) or not 1 <= len(name) <= 64:
        raise WorkflowError(f"{folder_name}: names use letters, digits, '-' and '_' (at most 64)")
    if not description or len(description) > 400:
        raise WorkflowError(f"{folder_name}/{MANIFEST}: description must be 1 to 400 characters")
    digest = hashlib.sha256(
        "\n".join([manifest.sha256, *(f"{path}:{f.sha256}" for path, f in sorted(files.items()))]).encode()
    ).hexdigest()[:16]
    return Workflow(
        name=name if builtin else USER_PREFIX + name,
        description=description,
        version=str(meta.get("version") or "1"),
        instructions=body,
        files=files,
        digest=digest,
        builtin=builtin,
    )


def check_user_directory(directory: Path) -> Path:
    resolved = directory.expanduser().resolve()
    try:
        info = resolved.stat()
    except OSError as exc:
        raise WorkflowError(f"workflow folder {directory} cannot be read: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise WorkflowError(f"workflow folder {directory} is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise WorkflowError(f"workflow folder {directory} belongs to another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise WorkflowError(f"workflow folder {directory} is writable by others; run: chmod go-w {resolved}")
    return resolved


def load_workflows(user_directories: Iterable[Path] = ()) -> dict[str, Workflow]:
    """Built-in bundles plus the operator's folders. Raises WorkflowError on any bad bundle."""
    found: dict[str, Workflow] = {}
    packaged = resources.files("mcp_tools") / "data" / "workflows"
    for entry in sorted(packaged.iterdir(), key=lambda e: e.name):
        if entry.is_dir() and not entry.name.startswith((".", "_")):
            bundle = load_bundle(entry, folder_name=entry.name, builtin=True)
            found[bundle.name] = bundle
    for directory in user_directories:
        root = check_user_directory(directory)
        for entry in sorted(root.iterdir(), key=lambda e: e.name):
            if entry.is_symlink() or not entry.is_dir() or entry.name.startswith("."):
                continue
            bundle = load_bundle(entry, folder_name=entry.name, builtin=False)
            if bundle.name in found:
                raise WorkflowError(f"workflow {bundle.name} is defined twice")
            found[bundle.name] = bundle
    return found
