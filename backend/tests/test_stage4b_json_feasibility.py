"""Stage 4B checkpoint A: MariaDB feasibility proof for the ``optional_sample_1m`` JSON candidate.

This is semantic feasibility only, against a test-only table
(``stage4b_json_feasibility``): no production Stage 4B table exists yet. It
proves the round-trip semantics the architecture freeze (``docs/ARCHITECTURE.md``
S25.2) depends on for the frozen ``optional_sample_1m`` candidate, never a claim
about MariaDB's physical on-disk byte layout. MariaDB's ``JSON`` type is a
``LONGTEXT`` alias, so a value stored through the deterministic Python encoding
below is not reformatted by the server; the round trip is exact, not merely
close. Skipped unless ``POMPA_TEST_DB_HOST`` is set (see ``conftest.mariadb``).
"""

from __future__ import annotations

import json
import math

import pytest

from pompa.storage import StorageUnavailable

TABLE = "stage4b_json_feasibility"

DDL = (
    f"CREATE TABLE {TABLE} (\n"
    "  ts INT UNSIGNED NOT NULL PRIMARY KEY,\n"
    "  values_json JSON NOT NULL CHECK (JSON_VALID(values_json))\n"
    ") ENGINE=InnoDB"
)


def dumps(values: dict) -> str:
    """The candidate's deterministic encoding (``docs/ARCHITECTURE.md`` S25.2)."""
    return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)


@pytest.fixture
def table(mariadb):
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(DDL)
        conn.commit()
    yield mariadb
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.commit()


def _upsert(storage, ts: int, values: dict) -> None:
    with storage._connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE} (ts, values_json) VALUES (%s, %s)"
            " ON DUPLICATE KEY UPDATE values_json = VALUES(values_json)",
            (ts, dumps(values)),
        )
        conn.commit()


def _read(storage, ts: int) -> dict | None:
    with storage._connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT values_json FROM {TABLE} WHERE ts = %s", (ts,))
        row = cur.fetchone()
        return None if row is None else json.loads(row[0])


# ---------------------------------------------------------------- numeric round trip

ROUND_TRIP_VALUES = {
    "zero": 0.0,
    "negative_zero": -0.0,
    "tenth": 0.1,
    "one_third": 1.0 / 3.0,
    "six_decimals": 21.123456,
    "tiny": 1e-12,
    "huge": 1e12,
    "realistic_negative_telemetry": -18.4,  # a plausible Europe/Warsaw winter outside_temp-like reading
}


def test_numeric_round_trip_each_value_isolated(table):
    """Each candidate value survives Python float -> deterministic JSON -> MariaDB -> Python float."""
    for i, (name, value) in enumerate(ROUND_TRIP_VALUES.items()):
        ts = 1_000 + i
        _upsert(table, ts, {"1": value})
        got = _read(table, ts)
        assert got is not None, name
        assert list(got.keys()) == ["1"], name
        assert got["1"] == value, (name, value, got["1"])


def test_numeric_round_trip_combined_document(table):
    """The realistic multi-series shape from the architecture example round-trips as one object."""
    ts = 2_000
    values = {"17": 21.25, "22": 0.0, "31": 46.8}
    _upsert(table, ts, values)
    got = _read(table, ts)
    assert got == values


def test_nan_and_infinity_are_rejected_before_persistence():
    """``allow_nan=False`` refuses non-finite values in Python; nothing reaches MariaDB."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            dumps({"1": bad})


# ---------------------------------------------------------------- storage semantics


def test_invalid_json_is_rejected_by_the_database(table):
    """A malformed document is refused by the CHECK constraint, not silently stored.

    ``Storage._connection`` wraps every ``pymysql.MySQLError`` as
    ``StorageUnavailable`` (unchanged Stage 1-3 behaviour); the constraint
    violation surfaces through that same wrapper here.
    """
    with pytest.raises(StorageUnavailable):
        with table._connection() as conn, conn.cursor() as cur:
            cur.execute(f"INSERT INTO {TABLE} (ts, values_json) VALUES (%s, %s)", (3_000, "{not valid json"))
            conn.commit()
    assert _read(table, 3_000) is None


def test_upsert_replaces_the_complete_values_object(table):
    """An idempotent upsert replaces the whole document; it never merges old and new keys."""
    ts = 4_000
    _upsert(table, ts, {"1": 10.0, "2": 20.0})
    assert _read(table, ts) == {"1": 10.0, "2": 20.0}
    _upsert(table, ts, {"3": 30.0})  # a later minute selecting a different series set
    got = _read(table, ts)
    assert got == {"3": 30.0}
    assert "1" not in got and "2" not in got


def test_delete_leaves_no_stale_value(table):
    ts = 5_000
    _upsert(table, ts, {"1": 1.0})
    assert _read(table, ts) is not None
    with table._connection() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE} WHERE ts = %s", (ts,))
        conn.commit()
    assert _read(table, ts) is None


def test_missing_key_is_distinguishable_from_key_with_numeric_zero(table):
    """A selected-but-unknown series (absent key) never reads the same as a real zero."""
    ts = 6_000
    _upsert(table, ts, {"5": 0.0, "6": 1.0})
    got = _read(table, ts)
    assert got["5"] == 0.0
    assert math.copysign(1.0, got["5"]) in (1.0, -1.0)  # a real, present numeric zero
    assert "7" not in got  # a different series never selected/known for this minute: no key at all
