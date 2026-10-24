"""Connection management. ``Database.tx()`` yields a connection inside a transaction that
commits on success and rolls back on error."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

__all__ = ["Conn", "Database"]

Conn = psycopg.Connection[DictRow]


class Database:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.dsn = dsn
        self._pool: ConnectionPool[Conn] = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
            connection_class=psycopg.Connection[DictRow],  # pyright: ignore[reportArgumentType]
        )

    @contextmanager
    def tx(self) -> Iterator[Conn]:
        with self._pool.connection() as conn, conn.transaction():
            yield conn

    @contextmanager
    def connection(self) -> Iterator[Conn]:
        """A pooled connection without an implicit outer transaction block (autocommit off:
        call ``conn.commit()`` yourself)."""
        with self._pool.connection() as conn:
            yield conn

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def connect(dsn: str) -> Conn:
        """A single standalone connection (scripts, migrations)."""
        return psycopg.Connection[DictRow].connect(dsn, row_factory=dict_row)
