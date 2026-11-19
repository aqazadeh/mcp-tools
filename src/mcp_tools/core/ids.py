"""Typed, time-sortable identifiers: `<prefix>_<26-char lowercase ULID>`.

The prefix says what an id refers to (so `medias[].value` can be a media or a job id
without ambiguity) and the ULID part sorts by creation time (so keyset pagination works).
"""

import re
import secrets
from typing import Literal

Prefix = Literal["job", "grp", "med", "upl", "prj", "fld", "ast", "run"]
PREFIXES: tuple[str, ...] = ("job", "grp", "med", "upl", "prj", "fld", "ast", "run")

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
_ULID = f"[{_ALPHABET}]{{26}}"
_ID = re.compile(rf"^({'|'.join(PREFIXES)})_({_ULID})$")

ID_LENGTH = 30  # 3-char prefix + "_" + 26


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: Prefix, now_ms: int) -> str:
    """48-bit millisecond timestamp followed by 80 random bits."""
    timestamp = _encode(now_ms & ((1 << 48) - 1), 10)
    randomness = _encode(secrets.randbits(80), 16)
    return f"{prefix}_{timestamp}{randomness}"


def id_prefix(value: object) -> str | None:
    """The prefix of a well-formed id, or None."""
    if not isinstance(value, str):
        return None
    match = _ID.match(value)
    return match.group(1) if match else None


def is_id(value: object, prefix: Prefix) -> bool:
    return id_prefix(value) == prefix


def shard_of(identifier: str) -> str:
    """Two characters from the random tail (the leading characters are time and barely vary)."""
    return identifier[-2:]


def id_pattern(prefix: Prefix) -> str:
    """A regular expression for one id kind; used in JSON Schemas and URL routing."""
    return rf"^{prefix}_{_ULID}$"
