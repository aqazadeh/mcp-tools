"""Idempotency keys for tools that spend or create.

`begin` and `finish` run inside the same write transaction as the work they protect, so a
key row never exists without its response: either everything committed (a retry replays the
stored response) or nothing did (a retry runs normally). The first statement is an atomic
INSERT ... ON CONFLICT DO NOTHING, which stays correct without SQLite's global write lock.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_tools.core.clock import Clock
from mcp_tools.core.errors import IDEMPOTENCY_KEY_REUSED, INVALID_PARAMS, ApiError
from mcp_tools.db.schema import idempotency_keys
from mcp_tools.db.sql import insert_ignore

REPLAY_WINDOW_MS = 24 * 60 * 60 * 1000
MAX_KEY_LENGTH = 128


def request_hash(payload: Any) -> str:
    """A stable digest of the normalised request; the same key must come with the same hash."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_key(key: str) -> str:
    if not key or len(key) > MAX_KEY_LENGTH or not key.isprintable() or " " in key:
        raise ApiError(
            INVALID_PARAMS,
            "idempotency_key must be 1-128 printable characters without spaces",
            hint="Use a fresh UUID per logical request and reuse it only when retrying that same request.",
        )
    return key


class IdempotencyStore:
    def __init__(self, clock: Clock, *, window_ms: int = REPLAY_WINDOW_MS) -> None:
        self._clock = clock
        self._window_ms = window_ms

    async def begin(
        self, conn: AsyncConnection, workspace_id: str, tool: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        """Claim the key. Returns the stored response to replay, or None to proceed."""
        now = self._clock.now_ms()
        where = (
            idempotency_keys.c.workspace_id == workspace_id,
            idempotency_keys.c.tool == tool,
            idempotency_keys.c.key == key,
        )
        await conn.execute(delete(idempotency_keys).where(*where, idempotency_keys.c.expires_at <= now))
        claimed = (
            await conn.execute(
                insert_ignore(conn, idempotency_keys, conflict=["workspace_id", "tool", "key"])
                .values(
                    workspace_id=workspace_id,
                    tool=tool,
                    key=key,
                    request_hash=digest,
                    response_json=None,
                    created_at=now,
                    expires_at=now + self._window_ms,
                )
                .returning(idempotency_keys.c.key)
            )
        ).first()
        if claimed is not None:
            return None
        row = (
            await conn.execute(
                select(idempotency_keys.c.request_hash, idempotency_keys.c.response_json).where(*where)
            )
        ).first()
        if row is None or row[0] != digest:
            raise ApiError(
                IDEMPOTENCY_KEY_REUSED,
                "this idempotency_key was already used with a different request",
                hint="Use a new key for a new request; reuse a key only to retry the identical request.",
            )
        return json.loads(row[1]) if row[1] else None

    async def finish(
        self, conn: AsyncConnection, workspace_id: str, tool: str, key: str, response: dict[str, Any]
    ) -> None:
        await conn.execute(
            update(idempotency_keys)
            .where(
                idempotency_keys.c.workspace_id == workspace_id,
                idempotency_keys.c.tool == tool,
                idempotency_keys.c.key == key,
            )
            .values(response_json=json.dumps(response, separators=(",", ":"), ensure_ascii=False))
        )

    async def sweep(self, conn: AsyncConnection) -> int:
        result = await conn.execute(
            delete(idempotency_keys).where(idempotency_keys.c.expires_at <= self._clock.now_ms())
        )
        return result.rowcount or 0
