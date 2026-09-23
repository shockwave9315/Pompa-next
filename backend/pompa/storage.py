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
from typing import NamedTuple

import pymysql

from .catalog import RECORDED_KEYS
from .minute import MINUTE, MinuteRow
from .timegrid import HOUR, ceil_hour, floor_hour

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

# Stage 4B checkpoint B: production policy tables (docs/ARCHITECTURE.md §25.2.1). No
# optional_sample_1m/optional_rollup_1h yet (checkpoints C/D). Series/revisions/members are
# immutable after insert: only OPTIONAL_POLICY_HEAD.revision_id is ever UPDATEd.
OPTIONAL_SERIES = "optional_series"
OPTIONAL_POLICY_REVISION = "optional_policy_revision"
OPTIONAL_POLICY_MEMBER = "optional_policy_member"
OPTIONAL_POLICY_HEAD = "optional_policy_head"

OPTIONAL_SERIES_DDL = (
    f"CREATE TABLE IF NOT EXISTS {OPTIONAL_SERIES} (\n"
    "  id              INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,\n"
    # Protocol identity/topic text compares exactly, independent of the database's default
    # collation (which may be case/accent-insensitive): explicit binary collation, not the
    # server default. Labels stay under the default utf8mb4 collation; they are display text,
    # never compared or looked up by value.
    "  identity        VARCHAR(16)  CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,\n"
    "  expected_topic  VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,\n"
    "  profile_version INT UNSIGNED NOT NULL,\n"
    "  label           VARCHAR(255) NOT NULL,\n"
    "  unit            VARCHAR(32)  NULL,\n"
    "  kind            VARCHAR(8)   NOT NULL,\n"
    "  semantic_type   VARCHAR(32)  NOT NULL,\n"
    "  sentinels_json  JSON         NOT NULL CHECK (JSON_VALID(sentinels_json)),\n"
    "  min_value       DOUBLE       NULL,\n"
    "  max_value       DOUBLE       NULL,\n"
    "  energy          TINYINT(1)   NOT NULL,\n"
    "  created_at      INT UNSIGNED NOT NULL,\n"
    "  UNIQUE KEY uq_optional_series_meaning (identity, expected_topic, profile_version)\n"
    ") ENGINE=InnoDB"
)

# base_revision_id is UNIQUE: at most one child per base revision, enforcing the linear chain and
# the "stale-base concurrent writers cannot both create competing accepted heads" requirement at
# the database level, in addition to the application-level policy-head lock (§25.2.1).
OPTIONAL_POLICY_REVISION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {OPTIONAL_POLICY_REVISION} (\n"
    "  id                    INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,\n"
    "  base_revision_id      INT UNSIGNED NULL,\n"
    "  effective_from_minute INT UNSIGNED NOT NULL CHECK (effective_from_minute % 60 = 0),\n"
    "  created_at            INT UNSIGNED NOT NULL,\n"
    "  UNIQUE KEY uq_optional_policy_revision_one_child_per_base (base_revision_id),\n"
    f"  CONSTRAINT fk_optional_policy_revision_base FOREIGN KEY (base_revision_id)"
    f" REFERENCES {OPTIONAL_POLICY_REVISION} (id)\n"
    ") ENGINE=InnoDB"
)

OPTIONAL_POLICY_MEMBER_DDL = (
    f"CREATE TABLE IF NOT EXISTS {OPTIONAL_POLICY_MEMBER} (\n"
    "  revision_id INT UNSIGNED NOT NULL,\n"
    "  series_id   INT UNSIGNED NOT NULL,\n"
    "  PRIMARY KEY (revision_id, series_id),\n"
    f"  CONSTRAINT fk_optional_policy_member_revision FOREIGN KEY (revision_id)"
    f" REFERENCES {OPTIONAL_POLICY_REVISION} (id),\n"
    f"  CONSTRAINT fk_optional_policy_member_series FOREIGN KEY (series_id)"
    f" REFERENCES {OPTIONAL_SERIES} (id)\n"
    ") ENGINE=InnoDB"
)

# Singleton: id is always 1. This one row is the entire database serialization point required by
# docs/ARCHITECTURE.md §25.2.1 (SELECT ... FOR UPDATE against it); it is the only mutable row in
# the whole policy schema.
OPTIONAL_POLICY_HEAD_DDL = (
    f"CREATE TABLE IF NOT EXISTS {OPTIONAL_POLICY_HEAD} (\n"
    "  id          TINYINT UNSIGNED NOT NULL PRIMARY KEY,\n"
    "  revision_id INT UNSIGNED NOT NULL,\n"
    f"  CONSTRAINT fk_optional_policy_head_revision FOREIGN KEY (revision_id)"
    f" REFERENCES {OPTIONAL_POLICY_REVISION} (id)\n"
    ") ENGINE=InnoDB"
)

GENESIS_REVISION_ID = 1
POLICY_HEAD_ID = 1


class SeriesRow(NamedTuple):
    """One immutable ``optional_series`` snapshot, exactly as stored (§25.2.1 "self-describing")."""

    id: int
    identity: str
    expected_topic: str
    profile_version: int
    label: str
    unit: str | None
    kind: str
    semantic_type: str
    sentinels_json: str
    min_value: float | None
    max_value: float | None
    energy: bool


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

    def first_purged_hour(self, start: int, end: int) -> int | None:
        """Lowest UTC hour overlapping ``[start, end)`` whose raw minutes were purged.

        ``purged(H)`` is a rollup row for ``H`` and no ``sample_1m`` row inside
        it. That is exact, not a guess: ``rebuild_hour`` never writes a rollup
        for an hour with no minutes, so the row proves the hour once held raw
        data, and purge refuses to delete an hour whose rollup it cannot prove
        complete, so a deleted hour always leaves that row behind. An hour with
        neither raw minutes nor a rollup row was never recorded and is not
        reported here.

        Whole overlapped hours are examined, so a partial edge at ``08:30``
        still sees the purged hour starting at ``08:00``.
        """
        lo, hi = floor_hour(start), ceil_hour(end)
        # Anti-join, not a correlated NOT EXISTS: both sides are one indexed range scan of the
        # requested span, so the cost follows the span and never the size of the tables.
        self._cur.execute(
            f"SELECT MIN(r.hour_ts) FROM (SELECT DISTINCT hour_ts FROM {ROLLUP}"
            f" WHERE hour_ts >= %s AND hour_ts < %s) AS r"
            f" LEFT JOIN (SELECT DISTINCT ts - ts MOD {HOUR} AS h FROM {TABLE}"
            f" WHERE ts >= %s AND ts < %s) AS m ON m.h = r.hour_ts"
            " WHERE m.h IS NULL",
            (lo, hi, lo, hi))
        (value,) = self._cur.fetchone()
        return None if value is None else int(value)

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

    # ------------------------------------------------------------- optional history policy

    def read_policy_head(self) -> int:
        """A plain (non-locking) read: for reporting only, never for a PUT's serialization point."""
        self._cur.execute(f"SELECT revision_id FROM {OPTIONAL_POLICY_HEAD} WHERE id = %s",
                          (POLICY_HEAD_ID,))
        (revision_id,) = self._cur.fetchone()
        return int(revision_id)

    def lock_policy_head(self) -> int:
        """The one database serialization point (§25.2.1): a locking/current read, not a plain one.

        Every real-committed-truth-observing transaction — a policy PUT and a
        minute-persistence transaction alike — must call this, never an
        ordinary ``SELECT``, which would stay bound to this transaction's own
        consistent snapshot even after this row's lock is granted (proved in
        ``test_stage4b_policy_concurrency.py``).
        """
        self._cur.execute(f"SELECT revision_id FROM {OPTIONAL_POLICY_HEAD} WHERE id = %s FOR UPDATE",
                          (POLICY_HEAD_ID,))
        (revision_id,) = self._cur.fetchone()
        return int(revision_id)

    def lock_latest_minute_ts(self) -> int | None:
        """The current/locking read of the canonical minute frontier (§25.2.1 policy PUT step).

        An indexed current read of the single newest row, not ``MAX(ts)``,
        whose locking semantics under ``FOR UPDATE`` are not the single-row
        record lock this needs.
        """
        self._cur.execute(f"SELECT ts FROM {TABLE} ORDER BY ts DESC LIMIT 1 FOR UPDATE")
        row = self._cur.fetchone()
        return None if row is None else int(row[0])

    def _revision(self, revision_id: int, *, locking: bool) -> tuple[int, int | None, int, int] | None:
        suffix = " FOR UPDATE" if locking else ""
        self._cur.execute(
            f"SELECT id, base_revision_id, effective_from_minute, created_at"
            f" FROM {OPTIONAL_POLICY_REVISION} WHERE id = %s{suffix}", (revision_id,))
        row = self._cur.fetchone()
        if row is None:
            return None
        rid, base, effective_from, created_at = row
        return (int(rid), None if base is None else int(base), int(effective_from), int(created_at))

    def read_revision(self, revision_id: int) -> tuple[int, int | None, int, int] | None:
        """``(id, base_revision_id, effective_from_minute, created_at)`` or ``None``.

        A plain read, bound to this transaction's own snapshot: correct for
        reporting (``read_selection``), where everything is read consistently
        as of one point in time. Never use this to read a revision whose
        currentness was just proved by a locking read in the same
        transaction (e.g. the head, right after ``lock_policy_head``) —
        use ``lock_revision`` there instead, or the snapshot can still show
        this revision as absent even though the lock already proved it
        committed (docs/ARCHITECTURE.md §25.2.1).
        """
        return self._revision(revision_id, locking=False)

    def lock_revision(self, revision_id: int) -> tuple[int, int | None, int, int] | None:
        """The locking/current-read counterpart of ``read_revision``: use this to read a
        revision immediately after a locking read (e.g. ``lock_policy_head``) proved it current."""
        return self._revision(revision_id, locking=True)

    @staticmethod
    def _decode_series_row(row: tuple) -> SeriesRow:
        sid, identity, topic, version, label, unit, kind, semantic, sentinels_json, lo, hi, energy = row
        return SeriesRow(int(sid), identity, topic, int(version), label, unit, kind, semantic,
                         sentinels_json, None if lo is None else float(lo),
                         None if hi is None else float(hi), bool(energy))

    def _revision_members(self, revision_id: int, *, locking: bool) -> list[SeriesRow]:
        suffix = " FOR UPDATE" if locking else ""
        self._cur.execute(
            f"SELECT s.id, s.identity, s.expected_topic, s.profile_version, s.label, s.unit,"
            f" s.kind, s.semantic_type, s.sentinels_json, s.min_value, s.max_value, s.energy"
            f" FROM {OPTIONAL_POLICY_MEMBER} m JOIN {OPTIONAL_SERIES} s ON s.id = m.series_id"
            f" WHERE m.revision_id = %s ORDER BY s.id{suffix}", (revision_id,))
        return [self._decode_series_row(row) for row in self._cur.fetchall()]

    def read_revision_members(self, revision_id: int) -> list[SeriesRow]:
        """Every immutable series snapshot selected by one revision, in stable (series_id) order.

        Same snapshot-vs-locking caveat as ``read_revision``/``lock_revision``.
        """
        return self._revision_members(revision_id, locking=False)

    def lock_revision_members(self, revision_id: int) -> list[SeriesRow]:
        """The locking/current-read counterpart of ``read_revision_members``."""
        return self._revision_members(revision_id, locking=True)

    def get_or_create_series(self, identity: str, expected_topic: str, profile_version: int,
                             label: str, unit: str | None, kind: str, semantic_type: str,
                             sentinels_json: str, min_value: float | None, max_value: float | None,
                             energy: bool, created_at: int) -> int:
        """Get-or-create by the immutable historical meaning ``(identity, expected_topic,
        profile_version)``. Race-safe under the UNIQUE constraint: a concurrent insert of the
        same meaning either wins this ``INSERT`` or is resolved by the
        ``ON DUPLICATE KEY UPDATE`` clause, which never changes any column, only recovers the
        existing row's id via ``LAST_INSERT_ID(id)``.
        """
        self._cur.execute(
            f"INSERT INTO {OPTIONAL_SERIES}"
            " (identity, expected_topic, profile_version, label, unit, kind, semantic_type,"
            "  sentinels_json, min_value, max_value, energy, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
            (identity, expected_topic, profile_version, label, unit, kind, semantic_type,
             sentinels_json, min_value, max_value, int(energy), created_at))
        self._cur.execute("SELECT LAST_INSERT_ID()")
        (series_id,) = self._cur.fetchone()
        return int(series_id)

    def lock_series_by_identity_version(self, identity: str, profile_version: int) -> SeriesRow | None:
        """Current/locking read of any existing series for ``(identity, profile_version)``,
        regardless of ``expected_topic`` (§25.2.8): one identity/version pair is exactly one
        semantic lineage, so this is always checked *before* a series is created or reused --
        never a plain read, and never scoped to one already-guessed ``expected_topic`` -- so a
        topic change under an unbumped ``profile_version`` is caught here, not missed because it
        looks like a brand-new, non-conflicting ``(identity, expected_topic, profile_version)``
        tuple. More than one matching row is corrupted state, never silently resolved.
        """
        self._cur.execute(
            f"SELECT id, identity, expected_topic, profile_version, label, unit, kind,"
            f" semantic_type, sentinels_json, min_value, max_value, energy"
            f" FROM {OPTIONAL_SERIES} WHERE identity = %s AND profile_version = %s FOR UPDATE",
            (identity, profile_version))
        rows = self._cur.fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise RuntimeError(
                f"corrupted optional_series state: {len(rows)} rows for identity={identity!r}"
                f" profile_version={profile_version}")
        return self._decode_series_row(rows[0])

    def insert_revision(self, base_revision_id: int | None, effective_from_minute: int,
                        created_at: int) -> int:
        if effective_from_minute % MINUTE:
            raise ValueError(f"unaligned effective_from_minute {effective_from_minute}")
        self._cur.execute(
            f"INSERT INTO {OPTIONAL_POLICY_REVISION} (base_revision_id, effective_from_minute, created_at)"
            " VALUES (%s, %s, %s)", (base_revision_id, effective_from_minute, created_at))
        return int(self._cur.lastrowid)

    def insert_revision_members(self, revision_id: int, series_ids: Iterable[int]) -> None:
        params = [(revision_id, sid) for sid in series_ids]
        if params:
            self._cur.executemany(
                f"INSERT INTO {OPTIONAL_POLICY_MEMBER} (revision_id, series_id) VALUES (%s, %s)", params)

    def update_policy_head(self, revision_id: int) -> None:
        self._cur.execute(f"UPDATE {OPTIONAL_POLICY_HEAD} SET revision_id = %s WHERE id = %s",
                          (revision_id, POLICY_HEAD_ID))


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
        """Create the tables or add missing recorded columns. Never drops anything.

        Also creates the Stage 4B checkpoint B optional-history policy tables
        (§25.2.1) and, idempotently, their one immutable genesis revision: an
        empty selection, effective from the beginning of time, with the
        singleton head already pointing at it. Canonical ``sample_1m``/
        ``rollup_1h`` schema and data are never touched by this addition.
        """
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
            cur.execute(OPTIONAL_SERIES_DDL)
            cur.execute(OPTIONAL_POLICY_REVISION_DDL)
            cur.execute(OPTIONAL_POLICY_MEMBER_DDL)
            cur.execute(OPTIONAL_POLICY_HEAD_DDL)
            cur.execute(
                f"INSERT IGNORE INTO {OPTIONAL_POLICY_REVISION}"
                " (id, base_revision_id, effective_from_minute, created_at)"
                " VALUES (%s, NULL, 0, UNIX_TIMESTAMP())", (GENESIS_REVISION_ID,))
            cur.execute(
                f"INSERT IGNORE INTO {OPTIONAL_POLICY_HEAD} (id, revision_id) VALUES (%s, %s)",
                (POLICY_HEAD_ID, GENESIS_REVISION_ID))
            conn.commit()

    def facts(self) -> tuple[int | None, int | None, int | None]:
        """Oldest stored minute, newest stored minute and ``rolled_until`` from one snapshot."""
        with self.session() as s:
            return (*s.minute_bounds(), s.rolled_until())
