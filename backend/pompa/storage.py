"""MariaDB storage: DDL and parameterized queries for ``sample_1m`` and ``rollup_1h``.

No domain calculations live here; rollup contents are computed by
``aggregation`` and passed in as plain tuples.

A connection is opened per session. The recorder thread and API request
threads therefore never share a PyMySQL connection, and a restarted database
needs no reconnect bookkeeping. A session is one REPEATABLE READ transaction:
its reads share one snapshot and its writes commit together or not at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager

import pymysql

from .catalog import RECORDED_KEYS
from .minute import MINUTE, MinuteRow
from .timegrid import HOUR

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
assert all(_IDENT.match(k) for k in RECORDED_KEYS)

TABLE = "sample_1m"
ROLLUP = "rollup_1h"

# (series, n, v_sum, v_min, v_max, v_last)
RollupValues = tuple[str, int, float, float, float, float]

ROLLUP_DDL = (
    f"CREATE TABLE IF NOT EXISTS {ROLLUP} (\n"
    "  hour_ts  INT UNSIGNED      NOT NULL,\n"
    "  series   VARCHAR(40)       NOT NULL,\n"
    "  n        SMALLINT UNSIGNED NOT NULL,\n"
    "  v_sum    DOUBLE            NOT NULL,\n"
    "  v_min    DOUBLE            NOT NULL,\n"
    "  v_max    DOUBLE            NOT NULL,\n"
    "  v_last   DOUBLE            NOT NULL,\n"
    "  PRIMARY KEY (hour_ts, series)\n"
    ") ENGINE=InnoDB"
)


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


def _check_hour(ts: int) -> None:
    if ts % HOUR:
        raise ValueError(f"unaligned hour ts {ts}")


class Session:
    """Queries inside one transaction (see ``Storage.session``)."""

    def __init__(self, cur):
        self._cur = cur

    # ---------------------------------------------------------------- sample_1m

    def upsert_minutes(self, rows: Iterable[MinuteRow]) -> None:
        """Idempotent write keyed by ``ts``."""
        params = []
        for row in rows:
            if row.ts % MINUTE:
                raise ValueError(f"unaligned minute ts {row.ts}")
            params.append((row.ts, *(row.values[k] for k in RECORDED_KEYS)))
        if params:
            self._cur.executemany(_UPSERT_SQL, params)

    def read_minutes(self, start: int, end: int,
                     keys: Sequence[str] = RECORDED_KEYS) -> list[tuple[int, dict[str, float | None]]]:
        """Rows with ``start <= ts < end`` in ascending ``ts``."""
        for k in keys:
            if k not in RECORDED_KEYS:
                raise ValueError(f"unknown column {k!r}")
        cols = "".join(f", {k}" for k in keys)
        self._cur.execute(f"SELECT ts{cols} FROM {TABLE} WHERE ts >= %s AND ts < %s ORDER BY ts", (start, end))
        return [(int(r[0]), dict(zip(keys, r[1:]))) for r in self._cur.fetchall()]

    def first_minute_at_or_after(self, ts: int) -> int | None:
        self._cur.execute(f"SELECT MIN(ts) FROM {TABLE} WHERE ts >= %s", (max(ts, 0),))
        (value,) = self._cur.fetchone()
        return None if value is None else int(value)

    def minute_bounds(self) -> tuple[int | None, int | None]:
        """Oldest and newest stored minute."""
        self._cur.execute(f"SELECT MIN(ts), MAX(ts) FROM {TABLE}")
        lo, hi = self._cur.fetchone()
        return (None if lo is None else int(lo), None if hi is None else int(hi))

    def delete_minutes_before(self, cutoff: int) -> int:
        self._cur.execute(f"DELETE FROM {TABLE} WHERE ts < %s", (cutoff,))
        return self._cur.rowcount

    # ---------------------------------------------------------------- rollup_1h

    def rolled_until(self) -> int | None:
        """``MAX(hour_ts) + 1 h``; the rollup is built contiguously in ascending hours."""
        self._cur.execute(f"SELECT MAX(hour_ts) FROM {ROLLUP}")
        (value,) = self._cur.fetchone()
        return None if value is None else int(value) + HOUR

    def replace_rollup_hour(self, hour_ts: int, values: Iterable[RollupValues]) -> None:
        """Replace every series row of one hour (within this session's transaction)."""
        _check_hour(hour_ts)
        params = [(hour_ts, *v) for v in values]
        if any(p[2] <= 0 for p in params):
            raise ValueError("a rollup row needs n > 0")
        self._cur.execute(f"DELETE FROM {ROLLUP} WHERE hour_ts = %s", (hour_ts,))
        if params:
            self._cur.executemany(
                f"INSERT INTO {ROLLUP} (hour_ts, series, n, v_sum, v_min, v_max, v_last)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)", params)

    def read_rollup(self, start: int, end: int,
                    series: Sequence[str]) -> list[tuple[int, str, int, float, float, float, float]]:
        """Rows with ``start <= hour_ts < end`` for the given series, ascending ``hour_ts``."""
        if not series:
            return []
        marks = ", ".join(["%s"] * len(series))
        self._cur.execute(
            f"SELECT hour_ts, series, n, v_sum, v_min, v_max, v_last FROM {ROLLUP}"
            f" WHERE hour_ts >= %s AND hour_ts < %s AND series IN ({marks}) ORDER BY hour_ts, series",
            (start, end, *series))
        return [(int(h), s, int(n), float(a), float(b), float(c), float(d))
                for h, s, n, a, b, c, d in self._cur.fetchall()]

    def hour_counts(self, start: int, end: int, series: str) -> list[tuple[int, int, int | None]]:
        """Per stored UTC hour in ``[start, end)``: ``(hour_ts, raw minute count, rollup n of series)``."""
        _check_hour(start)
        _check_hour(end)
        self._cur.execute(
            f"SELECT r.h, r.c, u.n FROM (SELECT ts - ts MOD {HOUR} AS h, COUNT(*) AS c FROM {TABLE}"
            f" WHERE ts >= %s AND ts < %s GROUP BY h) AS r"
            f" LEFT JOIN {ROLLUP} AS u ON u.hour_ts = r.h AND u.series = %s ORDER BY r.h",
            (start, end, series))
        return [(int(h), int(c), None if n is None else int(n)) for h, c, n in self._cur.fetchall()]


class Storage:
    def __init__(self, host: str, port: int, user: str, password: str, database: str,
                 connect_timeout: int = 3, io_timeout: int = 10):
        self._params = dict(
            host=host, port=port, user=user, password=password, database=database,
            connect_timeout=connect_timeout, read_timeout=io_timeout, write_timeout=io_timeout,
            charset="utf8mb4", autocommit=False,
            init_command="SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
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

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One transaction: committed when the block succeeds, rolled back otherwise.

        A ``StorageUnavailable`` raised after the commit was sent (lost
        acknowledgement) leaves its outcome unknown: callers must only use
        writes that are safe to repeat.
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            try:
                yield Session(cur)
            except BaseException:
                try:
                    conn.rollback()
                except pymysql.MySQLError:
                    pass  # the server discards an uncommitted transaction with the connection
                raise
            conn.commit()

    def ensure_schema(self) -> None:
        """Create the tables or add missing recorded columns. Never drops anything."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(create_table_sql())
            cur.execute(ROLLUP_DDL)
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

    def facts(self) -> tuple[int | None, int | None, int | None]:
        """Oldest stored minute, newest stored minute and ``rolled_until`` from one snapshot."""
        with self.session() as s:
            return (*s.minute_bounds(), s.rolled_until())
