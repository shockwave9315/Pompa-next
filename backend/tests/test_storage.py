"""sample_1m storage against real MariaDB (skipped unless POMPA_TEST_DB_HOST is set)."""

import pytest

from pompa.catalog import RECORDED_KEYS
from pompa.minute import MinuteRow
from pompa.storage import Storage, StorageUnavailable, create_table_sql

T0 = 1_800_000_000


def row(ts, **values):
    return MinuteRow(ts, {k: values.get(k) for k in RECORDED_KEYS})


def upsert(storage, rows):
    with storage.session() as s:
        s.upsert_minutes(rows)


def read(storage, start, end, keys):
    with storage.session() as s:
        return s.read_minutes(start, end, keys)


def columns(storage):
    with storage._connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_TYPE FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sample_1m' ORDER BY ORDINAL_POSITION"
        )
        return cur.fetchall()


def test_ddl_is_generated_from_catalog():
    sql = create_table_sql()
    assert "ts INT UNSIGNED NOT NULL PRIMARY KEY" in sql
    assert sql.endswith("ENGINE=InnoDB")
    for k in RECORDED_KEYS:
        assert f"  {k} DOUBLE NULL" in sql


def test_schema_bootstrap(mariadb):
    mariadb.ensure_schema()
    mariadb.ensure_schema()  # idempotent
    cols = columns(mariadb)
    assert cols[0] == ("ts", "int", "NO", "int(10) unsigned")
    assert [c[0] for c in cols[1:]] == list(RECORDED_KEYS)
    assert all(c[1] == "double" and c[2] == "YES" for c in cols[1:])
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT ENGINE FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sample_1m'")
        assert cur.fetchone() == ("InnoDB",)


def test_schema_bootstrap_is_additive(mariadb):
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE sample_1m (ts INT UNSIGNED NOT NULL PRIMARY KEY,"
                    " main_outlet_temp DOUBLE NULL) ENGINE=InnoDB")
        cur.execute("INSERT INTO sample_1m (ts, main_outlet_temp) VALUES (%s, 35)", (T0,))
        conn.commit()
    mariadb.ensure_schema()
    assert [c[0] for c in columns(mariadb)[1:]] == list(RECORDED_KEYS)
    assert read(mariadb, T0, T0 + 60, ["main_outlet_temp", "outside_temp"]) == [
        (T0, {"main_outlet_temp": 35.0, "outside_temp": None})
    ]


def test_upsert_is_idempotent_and_null_persists(mariadb):
    mariadb.ensure_schema()
    upsert(mariadb, [row(T0, main_outlet_temp=35.0, co_power_consumption=0.0)])
    upsert(mariadb, [row(T0, main_outlet_temp=35.0, co_power_consumption=0.0)])
    got = read(mariadb, T0, T0 + 60, list(RECORDED_KEYS))
    assert len(got) == 1
    ts, values = got[0]
    assert ts == T0
    assert values["main_outlet_temp"] == 35.0
    assert values["co_power_consumption"] == 0.0  # real zero stays zero
    assert values["outside_temp"] is None  # NULL stays NULL
    # A re-closed minute replaces the row (clock reversal cannot duplicate keys).
    upsert(mariadb, [row(T0, main_outlet_temp=36.5)])
    assert read(mariadb, T0, T0 + 60, ["main_outlet_temp", "co_power_consumption"]) == [
        (T0, {"main_outlet_temp": 36.5, "co_power_consumption": None})
    ]


def test_absence_remains_absence_and_reads_are_ordered_half_open(mariadb):
    mariadb.ensure_schema()
    upsert(mariadb, [row(T0 + 180, outside_temp=1.0), row(T0, outside_temp=0.0), row(T0 + 60, outside_temp=-2.5)])
    got = read(mariadb, T0, T0 + 180, ["outside_temp"])
    assert got == [(T0, {"outside_temp": 0.0}), (T0 + 60, {"outside_temp": -2.5})]
    assert read(mariadb, T0 + 120, T0 + 180, ["outside_temp"]) == []
    assert mariadb.facts() == (T0, T0 + 180, None)


def test_empty_bounds(mariadb):
    mariadb.ensure_schema()
    assert mariadb.facts() == (None, None, None)


def test_unaligned_ts_rejected(mariadb):
    mariadb.ensure_schema()
    with pytest.raises(ValueError):
        upsert(mariadb, [row(T0 + 1)])


def test_unknown_column_rejected(mariadb):
    with pytest.raises(ValueError):
        read(mariadb, T0, T0 + 60, ["ts; DROP TABLE sample_1m"])


def test_unreachable_database_is_reported_factually():
    storage = Storage("127.0.0.1", 1, "x", "x", "x", connect_timeout=1)
    with pytest.raises(StorageUnavailable):
        storage.facts()
    with pytest.raises(StorageUnavailable):
        storage.ensure_schema()


def test_missing_table_is_unavailable_not_crash(mariadb):
    with pytest.raises(StorageUnavailable):
        read(mariadb, T0, T0 + 60, ["outside_temp"])


def test_rollup_schema_is_the_approved_table(mariadb):
    mariadb.ensure_schema()
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY FROM information_schema.COLUMNS"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rollup_1h' ORDER BY ORDINAL_POSITION")
        assert cur.fetchall() == (
            ("hour_ts", "int(10) unsigned", "NO", "PRI"),
            ("series", "varchar(40)", "NO", "PRI"),
            ("n", "smallint(5) unsigned", "NO", ""),
            ("v_sum", "double", "NO", ""),
            ("v_min", "double", "NO", ""),
            ("v_max", "double", "NO", ""),
            ("v_last", "double", "NO", ""),
        )
        cur.execute("SELECT ENGINE FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rollup_1h'")
        assert cur.fetchone() == ("InnoDB",)
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
                    " ORDER BY TABLE_NAME")
        # Canonical history, policy tables, optional raw and optional rollup.
        assert cur.fetchall() == (
            ("optional_policy_head",),
            ("optional_policy_member",),
            ("optional_policy_revision",),
            ("optional_rollup_1h",),
            ("optional_sample_1m",),
            ("optional_series",),
            ("rollup_1h",),
            ("sample_1m",),
        )


def test_rollup_doubles_round_trip_exactly(mariadb):
    mariadb.ensure_schema()
    values = [("a", 60, 0.1 + 0.2, -1e-300, 1.7976931348623157e308, 35.123457 * 60)]
    with mariadb.session() as s:
        s.replace_rollup_hour(T0, values)
    with mariadb.session() as s:
        assert s.read_rollup(T0, T0 + 3600, ["a"]) == [(T0, *values[0])]
        assert s.rolled_until() == T0 + 3600


def test_rollup_row_needs_positive_n_and_aligned_hour(mariadb):
    mariadb.ensure_schema()
    with pytest.raises(ValueError), mariadb.session() as s:
        s.replace_rollup_hour(T0, [("a", 0, 0.0, 0.0, 0.0, 0.0)])
    with pytest.raises(ValueError), mariadb.session() as s:
        s.replace_rollup_hour(T0 + 60, [("a", 1, 0.0, 0.0, 0.0, 0.0)])
