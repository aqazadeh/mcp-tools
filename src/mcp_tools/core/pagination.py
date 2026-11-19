"""Opaque cursors and pages. One pagination style for every list tool."""

import base64
import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from mcp_tools.core.errors import INVALID_PARAMS, ApiError

T = TypeVar("T")

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


@dataclass(frozen=True)
class PageRequest:
    limit: int = DEFAULT_LIMIT
    cursor: dict[str, Any] | None = None


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    has_more: bool = False
    next_cursor: str | None = None


def clamp_limit(limit: int | None, *, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> int:
    if limit is None or limit <= 0:
        return default
    return min(limit, maximum)


def encode_cursor(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    """Decode a cursor produced by `encode_cursor`; empty means "first page"."""
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        state = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            INVALID_PARAMS, "cursor is not valid", hint="Pass next_cursor exactly as returned, or omit it."
        ) from exc
    if not isinstance(state, dict):
        raise ApiError(INVALID_PARAMS, "cursor is not valid", hint="Pass next_cursor exactly as returned.")
    return state


def page_request(limit: int | None, cursor: str | None) -> PageRequest:
    return PageRequest(limit=clamp_limit(limit), cursor=decode_cursor(cursor))
