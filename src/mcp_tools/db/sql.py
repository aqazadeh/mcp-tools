"""Small dialect helpers so repositories stay portable between SQLite and PostgreSQL."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncConnection


def insert_ignore(conn: AsyncConnection, table: Table, *, conflict: Sequence[str]) -> Any:
    """`INSERT ... ON CONFLICT (conflict) DO NOTHING` for the connection's dialect.

    Combine with `.returning(...)` and count returned rows; never read `rowcount` on a
    statement that uses RETURNING (SQLite reports 0 until the rows are fetched).
    """
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(table).on_conflict_do_nothing(index_elements=list(conflict))
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    return sqlite_insert(table).on_conflict_do_nothing(index_elements=list(conflict))
