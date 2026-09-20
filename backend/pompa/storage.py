"""MariaDB storage for ``sample_1m``: DDL and parameterized queries, no domain logic.

A connection is opened per operation. The recorder thread and API request
threads therefore never share a PyMySQL connection, and a restarted database
needs no reconnect bookkeeping.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager

import pymysql

from .catalog import RECORDED_KEYS
from .minute import MINUTE, MinuteRow

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
assert all(_IDENT.match(k) for k in RECORDED_KEYS)

TABLE = "sample_1m"


class StorageUnavailable(Exception):
    """The database could not be reached or rejected the operation."""


def create_table_sql() -> str:
    columns = ",\n".join(f"  {k} DOUBLE NULL" for k in RECORDED_KEYS)
    return (
        f"CREATE TABLE IF NOT EXISTS {TABLE} (\n"
        f"  ts INT UNSIGNED NOT NULL PRIMARY KEY,\n{columns}\n) ENGINE=InnoDB"
    )


def _upsert_sql() -> str:
    cols = ", ".join(("ts",) + RECORDED_KEYS)
    placeholders = ", ".join(["%s"] * (1 + len(RECORDED_KEYS)))
    updates = ", ".join(f"{k} = VALUES({k})" for k in RECORDED_KEYS)
    return f"INSERT INTO {TABLE} ({cols}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"


_UPSERT_SQL = _upsert_sql()


class Storage:
    def __init__(self, host: str, port: int, user: str, password: str, database: str,
                 connect_timeout: int = 3, io_timeout: int = 10):
        self._params = dict(
            host=host, port=port, user=user, password=password, database=database,
            connect_timeout=connect_timeout, read_timeout=io_timeout, write_timeout=io_timeout,
            charset="utf8mb4", autocommit=False,
        )

    @contextmanager
    def _connection(self) -> Iterator[pymysql.connections.Connection]:
        try:
            conn = pymysql.connect(**self._params)
        except pymysql.MySQLError as e:
            raise StorageUnavailable(str(e)) from e
        try:
            yield conn
        except pymysql.MySQLError as e:
            raise StorageUnavailable(str(e)) from e
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """Create ``sample_1m`` or add missing recorded columns. Never drops anything."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(create_table_sql())
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (TABLE,),
            )
            existing = {name for (name,) in cur.fetchall()}
            for key in RECORDED_KEYS:
                if key not in existing:
                    cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {key} DOUBLE NULL")
            conn.commit()

    def upsert(self, rows: Iterable[MinuteRow]) -> None:
        """Idempotent write keyed by ``ts``; one transaction."""
        params = []
        for row in rows:
            if row.ts % MINUTE:
                raise ValueError(f"unaligned minute ts {row.ts}")
            params.append((row.ts, *(row.values[k] for k in RECORDED_KEYS)))
        if not params:
            return
        with self._connection() as conn, conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, params)
            conn.commit()

    def read(self, start: int, end: int, keys: Sequence[str]) -> list[tuple[int, dict[str, float | None]]]:
        """Rows with ``start <= ts < end`` in ascending ``ts``."""
        for k in keys:
            if k not in RECORDED_KEYS:
                raise ValueError(f"unknown column {k!r}")
        cols = "".join(f", {k}" for k in keys)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT ts{cols} FROM {TABLE} WHERE ts >= %s AND ts < %s ORDER BY ts", (start, end))
            return [(int(r[0]), dict(zip(keys, r[1:]))) for r in cur.fetchall()]

    def bounds(self) -> tuple[int | None, int | None]:
        """Oldest and newest stored minute."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT MIN(ts), MAX(ts) FROM {TABLE}")
            lo, hi = cur.fetchone()
            return (None if lo is None else int(lo), None if hi is None else int(hi))
