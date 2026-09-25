"""Stage 4C-B: durable hour-local activity segments (docs/ARCHITECTURE.md §25.3.2).

``any_storage`` tests run identically on FakeStorage and, with ``POMPA_TEST_DB_HOST``, real
MariaDB; ``mariadb`` tests need the real server (schema, CHECKs, triggers, concurrency).
"""

import threading
import time
from contextlib import contextmanager
from datetime import date

import pytest

import conftest
from conftest import T0, persist_canonical, row
from pompa import activity as act
from pompa import storage as storage_module
from pompa.activity import (
    ENERGY_SERIES, Activity, ActivityRecordInvalid, Boundary, Gap, activity_events, build_segments,
    compressor_off_intervals, compressor_runs, decode_segment, decode_segments, defrosts, fold_energy,
    segment_record, summarize, timeline,
)
from pompa.aggregation import Stats, combine_maps, cop, energy_kwh, fold_minutes
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import (
    ACTIVITY_BACKFILL_HOURS_PER_STEP, PurgeRefused, RebuildRefused, Recorder, backfill_activity_step,
    purge_step, rebuild_hour, roll_next_hour,
)
from pompa.storage import StorageUnavailable
from pompa.timegrid import local_midnight

M, H, DAY = 60, 3600, 86400
END = 2**32 - 1
NOW = T0 + 400 * DAY  # far past retention: purge is limited only by rolled_until and the margin

# Minute shapes with dyadic power values, so every sum is exact in binary floating point.
CO = dict(compressor_freq=40.0, defrosting_state=0.0, heatpump_state=1.0, three_way_valve=0.0,
          co_power_consumption=900.5, co_power_production=3600.25, dhw_power_consumption=0.0,
          dhw_power_production=0.0)
DHW = {**CO, "three_way_valve": 1.0, "co_power_consumption": 0.0, "co_power_production": 0.0,
       "dhw_power_consumption": 1500.75, "dhw_power_production": 4500.5}
OFF = dict(compressor_freq=0.0, defrosting_state=0.0, heatpump_state=0.0, three_way_valve=0.0,
           co_power_consumption=0.0, co_power_production=0.0, dhw_power_consumption=0.0,
           dhw_power_production=0.0)
IDLE = {**OFF, "heatpump_state": 1.0, "co_power_consumption": 18.0}
UNKNOWN = {**CO, "compressor_freq": None, "dhw_power_production": None}


def defrost(fraction):
    return {**CO, "defrosting_state": fraction, "co_power_production": 0.0}


GAP = None


def rows_of(start, shapes):
    return [row(start + i * M, **shape) for i, shape in enumerate(shapes) if shape is not None]


def pairs(rows):
    return [(r.ts, r.values) for r in rows]


def roll_all(storage):
    while roll_next_hour(storage, END) is not None:
        pass


def put(storage, start, shapes, *, roll=True):
    rows = rows_of(start, shapes)
    persist_canonical(storage, rows)
    if roll:
        roll_all(storage)
    return rows


def raw(storage, start=0, end=END):
    with storage.session() as s:
        return s.read_minutes(start, end)


def records(storage, start=0, end=END):
    with storage.session() as s:
        return s.read_activity_segments(start, end)


def durable(storage, start=0, end=END):
    return decode_segments(records(storage, start, end))


def rollup_facts(storage, start=0, end=END):
    with storage.session() as s:
        return (s.read_rollup(start, end, ["recorded", *ENERGY_SERIES]), s.read_optional_rollup(start, end))


def delete_activity(storage, start=0, end=END):
    """Pre-4C-B state: rolled hours without any activity materialization."""
    if isinstance(storage, conftest.FakeStorage):
        for t in [t for t in storage.activity if start <= t < end]:
            del storage.activity[t]
        return
    with storage._connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM activity_segment_1h WHERE start_ts >= %s AND start_ts < %s", (start, end))
        conn.commit()


def purge_all(storage, now=NOW, max_hours=24):
    total, more = 0, True
    while more:
        _, deleted, more = purge_step(storage, now, 365, None, max_hours)
        total += deleted
    return total


# Two UTC hours covering every durable fact: observed start/stop and restart, CO→DHW, fractional
# defrost, an unknown minute, an internal gap, idle/off and a compressor run across the hour.
SCENARIO = ([OFF] * 5 + [CO] * 5 + [DHW] * 3 + [OFF] * 2 + [CO] * 5
            + [defrost(0.583333), defrost(1.0), defrost(1.0), defrost(0.166667)]
            + [CO] * 3 + [UNKNOWN] + [CO] * 2 + [GAP] * 2 + [OFF] * 4 + [IDLE] * 21 + [CO] * 3
            + [CO] * 3 + [OFF] * 57)
assert len(SCENARIO) == 120
TAIL = [OFF] * 180  # three more hours so purge may delete the scenario (two-hour margin)


def scenario(storage):
    put(storage, T0, SCENARIO + TAIL)


# ------------------------------------------------------------------ schema (MariaDB)

def test_activity_schema_is_the_approved_table(mariadb):
    mariadb.ensure_schema()
    mariadb.ensure_schema()  # idempotent
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, CHARACTER_SET_NAME,"
                    " COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
                    " AND TABLE_NAME = 'activity_segment_1h' ORDER BY ORDINAL_POSITION")
        columns = cur.fetchall()
        assert [c[:4] for c in columns] == [
            ("start_ts", "int(10) unsigned", "NO", "PRI"),
            ("minutes", "tinyint(3) unsigned", "NO", ""),
            ("rule_version", "smallint(5) unsigned", "NO", ""),
            ("activity", "varchar(16)", "NO", ""),
            ("compressor", "varchar(8)", "NO", ""),
            ("defrost_fraction", "double", "YES", ""),
            ("energy_json", "longtext", "NO", ""),
        ]
        assert columns[3][4:] == columns[4][4:] == ("ascii", "ascii_bin")
        cur.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
                    " AND TABLE_NAME = 'activity_segment_1h'")
        assert cur.fetchone() == ("InnoDB",)
        cur.execute("SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'activity_segment_1h'")
        assert cur.fetchall() == (("PRIMARY", "start_ts"),)  # no speculative index
        cur.execute("SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'activity_segment_1h'"
                    " ORDER BY CONSTRAINT_NAME")
        assert {name for (name,) in cur.fetchall()} == {
            "PRIMARY", "ck_activity_segment_bounds", "ck_activity_segment_defrost", "energy_json"}
        cur.execute("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS"
                    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND (TABLE_NAME = 'activity_segment_1h'"
                    " OR REFERENCED_TABLE_NAME = 'activity_segment_1h')")
        assert cur.fetchone() == (0,)  # no FK: segments must outlive raw purge


def test_schema_upgrade_is_additive_and_leaves_existing_data(mariadb):
    mariadb.ensure_schema()
    scenario(mariadb)
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE activity_segment_1h")  # the database as it was before Stage 4C-B
        conn.commit()
    before = raw(mariadb), rollup_facts(mariadb)
    mariadb.ensure_schema()
    mariadb.ensure_schema()
    assert (raw(mariadb), rollup_facts(mariadb)) == before
    assert records(mariadb) == []


@pytest.mark.parametrize("bad", [
    (T0 + 30, 1, 1, "co", "on", 0.0, "{}"),  # unaligned start
    (T0 + 59 * M, 2, 1, "co", "on", 0.0, "{}"),  # crosses the UTC hour
    (T0, 0, 1, "co", "on", 0.0, "{}"),  # no minutes
    (T0, 61, 1, "co", "on", 0.0, "{}"),
    (T0, 1, 1, "defrost", "on", 1.5, "{}"),  # fraction outside [0, 1]
    (T0, 1, 1, "co", "on", 0.0, "not json"),
])
def test_database_checks_reject_structurally_invalid_rows(mariadb, bad):
    mariadb.ensure_schema()
    with pytest.raises(StorageUnavailable):
        with mariadb.session() as s:
            s._cur.execute("INSERT INTO activity_segment_1h VALUES (%s, %s, %s, %s, %s, %s, %s)", bad)
    assert records(mariadb) == []


# ------------------------------------------------------------------ records and round trip

def test_record_encoding_is_literal_and_deterministic():
    """The exact persisted bytes of a version-1 segment; shortest round-trip float text."""
    rows = rows_of(T0, [{**defrost(f), "co_power_consumption": 0.1 + 0.2, "co_power_production": 0.0,
                          "dhw_power_consumption": None} for f in (0.583333, 1.0, 1.0)])
    first, second = build_segments(pairs(rows))
    x = "0.30000000000000004"
    assert segment_record(first) == (
        T0, 1, 1, "defrost", "on", 0.583333,
        f'{{"co_power_consumption":[1,{x},{x},{x},{x}],"co_power_production":[1,0.0,0.0,0.0,0.0],'
        '"dhw_power_production":[1,0.0,0.0,0.0,0.0],'
        f'"pair_co_in":[1,{x},{x},{x},{x}],"pair_co_out":[1,0.0,0.0,0.0,0.0]}}')
    assert segment_record(second)[:6] == (T0 + M, 2, 1, "defrost", "on", 1.0)
    assert decode_segment(segment_record(second)) == second


EDGE_SHAPES = [
    {**UNKNOWN, "defrosting_state": None},  # NULL fraction
    defrost(0.583333), defrost(1.0), defrost(1.0), defrost(0.000001),  # fractional, full, tiny
    OFF, OFF,  # zeros everywhere, still paired 0/0
    {**CO, "co_power_consumption": 0.1, "co_power_production": 0.2},
    {**CO, "co_power_consumption": 1.7976931348623157e308 / 2, "co_power_production": 5e-324},
    {**DHW, "dhw_power_production": None},  # unpaired DHW
    {**CO, "dhw_power_consumption": None, "dhw_power_production": None},
    {**IDLE, "heatpump_state": 0.5},
]


def test_segments_round_trip_exactly(any_storage):
    rows = put(any_storage, T0, EDGE_SHAPES)
    expected = build_segments(pairs(rows))
    assert durable(any_storage) == expected
    fractions = [s.defrost_fraction for s in durable(any_storage)]
    assert fractions[:4] == [None, 0.583333, 1.0, 0.000001]
    zero = durable(any_storage)[4]
    assert zero.energy["pair_total_in"] == Stats(2, 0.0, 0.0, 0.0, 0.0)
    assert all(r[2] == act.ACTIVITY_RULE_VERSION == 1 for r in records(any_storage))


def _energy_json(**series):
    import json
    return json.dumps({k: list(v) for k, v in series.items()})


GOOD = (T0, 2, 1, "co", "on", 0.0, _energy_json(co_power_consumption=[2, 10.0, 4.0, 6.0, 6.0]))


@pytest.mark.parametrize("field, value", [
    (0, T0 + 1), (0, T0 + 59 * M), (0, -60), (0, 1.0 * T0), (1, 0), (1, 61), (1, True),
    (2, 2), (2, 0), (2, None),
    (3, "heating"), (3, "CO"), (4, "running"), (3, "off"), (3, "idle"),
    (5, None), (5, 0.5), (5, 1.5), (5, -0.0000001), (5, float("nan")), (5, "0"),
    (6, "not json"), (6, "[]"), (6, None), (6, _energy_json(recorded=[2, 2.0, 1.0, 1.0, 1.0])),
    (6, _energy_json(co_power_consumption=[0, 0.0, 0.0, 0.0, 0.0])),
    (6, _energy_json(co_power_consumption=[3, 10.0, 4.0, 6.0, 6.0])),
    (6, _energy_json(co_power_consumption=[True, 10.0, 4.0, 6.0, 6.0])),
    (6, _energy_json(co_power_consumption=[2.0, 10.0, 4.0, 6.0, 6.0])),
    (6, _energy_json(co_power_consumption=[2, 10.0, 4.0, 6.0])),
    (6, '{"co_power_consumption":[2,1e999,4.0,6.0,6.0]}'),
    (6, '{"co_power_consumption":[2,NaN,4.0,6.0,6.0]}'),
    (6, _energy_json(co_power_consumption=[2, 10.0, 6.0, 4.0, 5.0])),
    (6, _energy_json(co_power_consumption=[2, 10.0, 4.0, 6.0, 7.0])),
    (6, _energy_json(co_power_consumption=[2, -10.0, -4.0, 6.0, 6.0])),
    (6, _energy_json(co_power_consumption=[2, 10.0, 4.0, 6.0, "6"])),
    (6, _energy_json(co_power_consumption=[2, 10.0, 4.0, 6.0, 6.0],
                     co_power_production=[2, 10.0, 4.0, 6.0, 6.0], pair_co_in=[2, 10.0, 4.0, 6.0, 6.0])),
    (6, _energy_json(co_power_consumption=[2, 10.0, 4.0, 6.0, 6.0],
                     co_power_production=[2, 10.0, 4.0, 6.0, 6.0], pair_co_in=[2, 10.0, 4.0, 6.0, 6.0],
                     pair_co_out=[1, 5.0, 5.0, 5.0, 5.0])),
    (6, _energy_json(co_power_consumption=[1, 5.0, 5.0, 5.0, 5.0],
                     co_power_production=[2, 10.0, 4.0, 6.0, 6.0], pair_co_in=[2, 10.0, 4.0, 6.0, 6.0],
                     pair_co_out=[2, 10.0, 4.0, 6.0, 6.0])),
    (6, _energy_json(pair_total_in=[1, 5.0, 5.0, 5.0, 5.0], pair_total_out=[1, 5.0, 5.0, 5.0, 5.0])),
])
def test_malformed_records_fail_closed(field, value):
    assert decode_segment(GOOD).minutes == 2
    bad = list(GOOD)
    bad[field] = value
    with pytest.raises(ActivityRecordInvalid):
        decode_segment(tuple(bad))


@pytest.mark.parametrize("activity, compressor, fraction, valid", [
    ("defrost", "on", 0.25, True), ("defrost", "off", 1.0, True), ("defrost", "unknown", 0.25, True),
    ("unknown", "on", 0.0, True), ("unknown", "on", None, True), ("unknown", "off", None, True),
    ("unknown", "unknown", 0.0, True), ("off", "off", 0.0, True), ("idle", "off", 0.0, True),
    ("transition", "on", 0.0, True),
    ("defrost", "on", 0.0, False), ("idle", "off", None, False), ("co", "unknown", 0.0, False),
    ("idle", "unknown", 0.0, False), ("unknown", "on", 0.5, False),
])
def test_version_1_activity_compressor_fraction_combinations(activity, compressor, fraction, valid):
    record = (T0, 2, 1, activity, compressor, fraction, "{}")
    if valid:
        assert decode_segment(record).activity.value == activity
    else:
        with pytest.raises(ActivityRecordInvalid):
            decode_segment(record)


def test_overlapping_or_unordered_records_fail_closed():
    a = (T0, 2, 1, "co", "on", 0.0, "{}")
    b = (T0 + M, 1, 1, "dhw", "on", 0.0, "{}")
    with pytest.raises(ActivityRecordInvalid):
        decode_segments([a, b])
    with pytest.raises(ActivityRecordInvalid):
        decode_segments([b, a])


def test_clipped_piece_is_never_persisted():
    [segment] = build_segments(pairs(rows_of(T0, [CO] * 3)))
    with pytest.raises(ValueError):
        segment_record(segment.clip(T0, T0 + M))


# ------------------------------------------------------------------ rebuild and forward roll

def test_roll_materializes_activity_with_the_rollups(any_storage):
    rows = put(any_storage, T0, SCENARIO, roll=False)
    assert roll_next_hour(any_storage, T0 + H) == T0
    assert durable(any_storage) == build_segments(pairs([r for r in rows if r.ts < T0 + H]))
    assert roll_next_hour(any_storage, T0 + H) is None  # hour 1 not closed: no segments yet
    assert records(any_storage, T0 + H, T0 + 2 * H) == []
    assert roll_next_hour(any_storage, T0 + 2 * H) == T0 + H
    assert durable(any_storage) == build_segments(pairs(rows))
    assert len(records(any_storage, T0 + H, T0 + 2 * H)) == 2  # hour-local pieces of the run


def test_repeated_rebuild_is_identical_and_replaces_stale_rows(any_storage):
    scenario(any_storage)
    before = records(any_storage), rollup_facts(any_storage)
    with any_storage.session() as s:
        s.replace_activity_hour(T0, [*s.read_activity_segments(T0, T0 + H)[:-1],
                                     (T0 + 59 * M, 1, 1, "dhw", "on", 0.0, "{}")])
    with any_storage.session() as s:
        rebuild_hour(s, T0)
        rebuild_hour(s, T0)
    assert (records(any_storage), rollup_facts(any_storage)) == before


def test_empty_hour_is_never_a_fabricated_record(any_storage):
    put(any_storage, T0, [CO] * 3)
    put(any_storage, T0 + 2 * H, [CO] * 3)  # hour 1 never recorded
    assert records(any_storage, T0 + H, T0 + 2 * H) == []
    tl = timeline(durable(any_storage), T0, T0 + 3 * H, T0 + 3 * H)
    assert Gap(T0 + 3 * M, T0 + 2 * H) in tl.items
    runs = compressor_runs(tl)
    assert [(r.start_boundary, r.end_boundary) for r in runs] == [
        (Boundary.OUTSIDE_EVIDENCE, Boundary.GAP), (Boundary.GAP, Boundary.GAP)]


# ------------------------------------------------------------------ late writes

def test_late_write_changing_segmentation_rebuilds_activity_atomically(any_storage):
    put(any_storage, T0, [CO, CO, CO, OFF, OFF])
    put(any_storage, T0 + H, [OFF])  # hour 0 is rolled with raw still present
    assert [(s.activity, s.minutes) for s in durable(any_storage, T0, T0 + H)] == [
        (Activity.CO, 3), (Activity.OFF, 2)]
    persist_canonical(any_storage, rows_of(T0 + 2 * M, [DHW]))
    assert [(s.activity, s.minutes) for s in durable(any_storage, T0, T0 + H)] == [
        (Activity.CO, 2), (Activity.DHW, 1), (Activity.OFF, 2)]
    with any_storage.session() as s:
        assert s.read_minutes(T0 + 2 * M, T0 + 3 * M)[0][1]["three_way_valve"] == 1.0
        dhw = {r[1]: r[2:] for r in s.read_rollup(T0, T0 + H, ["dhw_power_production", "pair_dhw_out"])}
    assert dhw["dhw_power_production"][0] == 5 and dhw["dhw_power_production"][1] == 4500.5
    assert durable(any_storage, T0, T0 + H) == build_segments(raw(any_storage, T0, T0 + H))


def test_late_write_changing_energy_only(any_storage):
    put(any_storage, T0, [CO, CO, CO])
    put(any_storage, T0 + H, [OFF])
    persist_canonical(any_storage, rows_of(T0 + M, [{**CO, "co_power_production": 1000.0}]))
    [segment] = durable(any_storage, T0, T0 + H)
    assert segment.minutes == 3
    assert segment.energy["co_power_production"] == Stats(3, 8200.5, 1000.0, 3600.25, 3600.25)
    assert segment.energy["pair_co_out"] == segment.energy["co_power_production"]


def test_late_write_materializes_a_pre_4c_rolled_hour(any_storage):
    put(any_storage, T0, [CO, CO, OFF])
    put(any_storage, T0 + H, [OFF])
    delete_activity(any_storage)
    persist_canonical(any_storage, rows_of(T0 + 3 * M, [OFF]))
    assert durable(any_storage, T0, T0 + H) == build_segments(raw(any_storage, T0, T0 + H))
    assert records(any_storage, T0 + H, T0 + 2 * H) == []  # untouched hour stays for backfill


def _lose_ack(storage, monkeypatch):
    original = storage.session

    @contextmanager
    def lost_ack():
        with original() as session:
            yield session
        raise StorageUnavailable("ack lost")

    monkeypatch.setattr(storage, "session", lost_ack)


def test_lost_ack_retry_leaves_no_duplicate_segments(any_storage, monkeypatch):
    put(any_storage, T0, [CO, CO, CO, OFF, OFF])
    put(any_storage, T0 + H, [OFF])
    late = rows_of(T0 + 2 * M, [DHW])
    _lose_ack(any_storage, monkeypatch)
    with pytest.raises(StorageUnavailable):
        persist_canonical(any_storage, late)
    monkeypatch.undo()
    committed = records(any_storage), rollup_facts(any_storage), raw(any_storage)
    persist_canonical(any_storage, late)
    persist_canonical(any_storage, late)
    assert (records(any_storage), rollup_facts(any_storage), raw(any_storage)) == committed
    assert [r[0] - T0 for r in records(any_storage, T0, T0 + H)] == [0, 120, 180]


def _fail_after_activity(monkeypatch):
    """Every storage writes the activity hour, then the connection dies before commit."""
    for cls in (storage_module.Session, conftest.FakeSession):
        original = cls.replace_activity_hour

        def failing(session, hour_ts, recs, _original=original):
            _original(session, hour_ts, recs)
            raise StorageUnavailable("connection lost after the activity write")

        monkeypatch.setattr(cls, "replace_activity_hour", failing)


def test_failure_after_activity_write_rolls_back_every_representation(any_storage, monkeypatch):
    put(any_storage, T0, [CO, CO, CO, OFF, OFF])
    put(any_storage, T0 + H, [OFF])
    put(any_storage, T0 + 2 * H, [CO], roll=False)
    before = records(any_storage), rollup_facts(any_storage), raw(any_storage)
    _fail_after_activity(monkeypatch)
    with pytest.raises(StorageUnavailable):
        persist_canonical(any_storage, rows_of(T0 + 2 * M, [DHW]))
    with pytest.raises(StorageUnavailable):
        roll_next_hour(any_storage, END)  # hour 1 would roll next: nothing of it commits either
    monkeypatch.undo()
    assert (records(any_storage), rollup_facts(any_storage), raw(any_storage)) == before


def test_activity_insert_rejected_by_database_rolls_back_rollups(mariadb):
    mariadb.ensure_schema()
    put(mariadb, T0, [CO, CO, CO])
    put(mariadb, T0 + H, [OFF])
    before = records(mariadb), rollup_facts(mariadb), raw(mariadb)
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TRIGGER reject_activity BEFORE INSERT ON activity_segment_1h"
                    " FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reject activity'")
        conn.commit()
    try:
        with pytest.raises(StorageUnavailable):
            persist_canonical(mariadb, rows_of(T0 + M, [DHW]))
        persist_canonical(mariadb, rows_of(T0 + 2 * H, [OFF]))  # unrolled hour: no activity write
        with pytest.raises(StorageUnavailable):
            roll_next_hour(mariadb, END)
    finally:
        with mariadb._connection() as conn, conn.cursor() as cur:
            cur.execute("DROP TRIGGER reject_activity")
            conn.commit()
    assert (records(mariadb), rollup_facts(mariadb)) == before[:2]
    assert raw(mariadb)[:-1] == before[2]


def test_late_write_into_purged_hour_is_refused_not_rebuilt_from_segments(any_storage):
    scenario(any_storage)
    purge_all(any_storage)
    kept = records(any_storage, T0, T0 + 2 * H)
    assert kept
    with pytest.raises(RebuildRefused):
        persist_canonical(any_storage, rows_of(T0 + 30 * M, [CO]))
    assert records(any_storage, T0, T0 + 2 * H) == kept
    assert raw(any_storage, T0, T0 + 2 * H) == []


# ------------------------------------------------------------------ backfill of pre-4C-B hours

def _pre_4c(storage, hours):
    """``hours`` rolled hours without segments, and one unrolled minute after them."""
    put(storage, T0, ([CO] * 30 + [OFF] * 30) * hours)
    delete_activity(storage)
    put(storage, T0 + hours * H, [OFF], roll=False)
    with storage.session() as s:
        return s.rolled_until()


def test_backfill_materializes_rolled_raw_hours_bounded_and_idempotent(any_storage):
    rolled_until = _pre_4c(any_storage, 5)
    before = rollup_facts(any_storage)
    scan, hours, done = backfill_activity_step(any_storage, 0, 2)
    assert (hours, done, scan) == ([T0, T0 + H], False, T0 + 2 * H)
    assert [r[0] for r in records(any_storage)] == [T0, T0 + 30 * M, T0 + H, T0 + H + 30 * M]
    scan, hours, done = backfill_activity_step(any_storage, scan, 2)
    assert (hours, done) == ([T0 + 2 * H, T0 + 3 * H], False)
    scan, hours, done = backfill_activity_step(any_storage, scan, 2)
    assert (hours, done) == ([T0 + 4 * H], True)
    assert backfill_activity_step(any_storage, 0, 24)[1:] == ([], True)  # nothing left, from zero
    assert durable(any_storage) == build_segments(raw(any_storage, 0, rolled_until))
    with any_storage.session() as s:
        assert s.rolled_until() == rolled_until == T0 + 5 * H
    assert rollup_facts(any_storage) == before
    assert records(any_storage, T0 + 5 * H, END) == []  # the unrolled hour waits for its roll


def test_backfill_skips_long_empty_stretches_in_bounded_scans(any_storage):
    put(any_storage, T0, [CO])
    put(any_storage, T0 + 30 * DAY, [CO])
    put(any_storage, T0 + 30 * DAY + H, [OFF])
    delete_activity(any_storage)
    scan, hours, done = backfill_activity_step(any_storage, 0, 24)
    assert (hours, done) == ([T0], False)  # one bounded scan window
    scan, hours, done = backfill_activity_step(any_storage, scan, 24)
    assert (hours, done) == ([T0 + 30 * DAY, T0 + 30 * DAY + H], True)  # jumped to the next raw minute


def test_backfill_never_fabricates_hours_purged_before_4c(any_storage):
    put(any_storage, T0, [CO] * 3)
    put(any_storage, T0 + H, [CO] * 3 + [OFF])
    delete_activity(any_storage)
    with any_storage.session() as s:  # the hour was purged by a pre-4C-B process
        s.delete_minutes_before(T0 + H)
        assert s.first_purged_hour(T0, T0 + H) == T0
    assert backfill_activity_step(any_storage, 0, 24)[1:] == ([T0 + H], True)
    assert records(any_storage, T0, T0 + H) == []
    with any_storage.session() as s:
        assert s.first_purged_hour(T0, T0 + 2 * H) == T0  # canonical purge fact unchanged
        assert s.read_rollup(T0, T0 + H, ["recorded"])[0][2] == 3
    tl = timeline(durable(any_storage), T0, T0 + 2 * H, T0 + 2 * H)
    assert tl.items[0] == Gap(T0, T0 + H)  # unavailable evidence, never invented segments


def test_backfill_ignores_raw_hours_without_a_rollup(any_storage):
    put(any_storage, T0, [CO] * 3)
    for hour in (2, 3, 4):
        put(any_storage, T0 + hour * H, [OFF])
    with any_storage.session() as s:
        s.upsert_minutes(rows_of(T0 + H, [CO]))  # a raw hour below rolled_until without its rollup
    delete_activity(any_storage)
    assert backfill_activity_step(any_storage, 0, 24)[1] == [T0, T0 + 2 * H, T0 + 3 * H, T0 + 4 * H]
    assert records(any_storage, T0 + H, T0 + 2 * H) == []
    with pytest.raises(PurgeRefused):  # still reported as the canonical anomaly it is
        purge_step(any_storage, NOW, 365, None, 24)


def _recorder(storage, start):
    ingest = Ingest(600)
    return Recorder(ingest, MinuteAccumulator(ingest, start), storage, 60)


def test_recorder_backfills_before_it_purges(any_storage):
    hours = ACTIVITY_BACKFILL_HOURS_PER_STEP + 6
    _pre_4c(any_storage, hours)
    minutes_before = len(raw(any_storage))
    rec = _recorder(any_storage, NOW)
    def backfilled():
        return len({r[0] - r[0] % H for r in records(any_storage, T0, T0 + hours * H)})

    rec.tick(NOW + 1)  # also rolls the trailing hour through the ordinary forward path
    assert backfilled() == ACTIVITY_BACKFILL_HOURS_PER_STEP
    assert len(raw(any_storage)) == minutes_before  # purge waited for the backfill
    assert rec.snapshot(lambda: NOW)[1]["recorder"]["purge"]["last_run_at"] is None
    rec.tick(NOW + 2)
    assert backfilled() == hours
    assert len(raw(any_storage)) < minutes_before  # now purge proves and deletes
    assert rec.purge_error is None and rec.activity_backfill_error is None
    assert len(durable(any_storage)) == 2 * hours + 1  # durable truth survived the purge


def test_purge_cannot_outrun_backfill_even_when_called_directly(any_storage):
    _pre_4c(any_storage, 5)
    minutes_before = len(raw(any_storage))
    with pytest.raises(PurgeRefused, match="activity segments of hour"):
        purge_step(any_storage, NOW, 365, None, 24)
    assert len(raw(any_storage)) == minutes_before


# ------------------------------------------------------------------ purge proof

def _corrupt(storage, kind):
    pristine = records(storage, T0, T0 + H)
    with storage.session() as s:
        recs = [list(r) for r in s.read_activity_segments(T0, T0 + H)]
        if kind == "missing hour":
            recs = []
        elif kind == "deleted segment":
            del recs[3]
        elif kind == "split segment":  # same minutes and totals, different segmentation
            start, minutes = recs[1][0], recs[1][1]
            recs[1:2] = [[start, 1, *recs[1][2:6], "{}"], [start + M, minutes - 1, *recs[1][2:6], "{}"]]
        elif kind == "extra segment":
            recs.append([T0 + 31 * M, 1, 1, "unknown", "unknown", 0.0, "{}"])  # inside the gap
        elif kind == "shifted start":
            recs[1][0] += M
            recs[1][1] -= 1
        elif kind == "activity":
            recs[1][3] = "transition"
        elif kind == "compressor":
            recs[0][3:5] = ["unknown", "unknown"]
        elif kind == "fraction":
            index = next(i for i, r in enumerate(recs) if r[5] == 0.583333)
            recs[index][5] = 0.583334
        elif kind == "version":
            recs[0][2] = 2
        elif kind == "energy count":
            recs[1][6] = recs[1][6].replace('"co_power_consumption":[5,', '"co_power_consumption":[4,')
        elif kind == "energy value":
            recs[1][6] = recs[1][6].replace("4502.5", "4502.0")
        elif kind == "energy series":
            recs[1][6] = recs[1][6].replace(',"pair_total_in"', ',"pair_total_out_x"')
        s.replace_activity_hour(T0, [tuple(r) for r in recs])
    assert records(storage, T0, T0 + H) != pristine


@pytest.mark.parametrize("kind", ["missing hour", "deleted segment", "split segment", "extra segment",
                                  "shifted start", "activity", "compressor", "fraction", "version",
                                  "energy count", "energy value", "energy series"])
def test_purge_refuses_every_activity_mismatch(any_storage, kind):
    scenario(any_storage)
    pristine = records(any_storage, T0, T0 + H)
    minutes_before = raw(any_storage)
    _corrupt(any_storage, kind)
    with pytest.raises(PurgeRefused):
        purge_step(any_storage, NOW, 365, None, 24)
    assert raw(any_storage) == minutes_before
    with any_storage.session() as s:  # the repair is still possible from raw
        rebuild_hour(s, T0)
    assert records(any_storage, T0, T0 + H) == pristine
    assert purge_all(any_storage) == len([x for x in SCENARIO + TAIL[:60] if x is not None])


def test_successful_purge_keeps_durable_activity_and_rollups(any_storage):
    scenario(any_storage)
    kept = records(any_storage, T0, T0 + 3 * H), rollup_facts(any_storage, T0, T0 + 3 * H)
    assert purge_all(any_storage) == 118 + 60
    assert raw(any_storage, T0, T0 + 3 * H) == []
    assert (records(any_storage, T0, T0 + 3 * H), rollup_facts(any_storage, T0, T0 + 3 * H)) == kept
    with any_storage.session() as s:
        assert s.first_purged_hour(T0, T0 + 3 * H) == T0


# ------------------------------------------------------------------ durable truth after purge

def _facts(tl):
    s = summarize(tl, tl.start, tl.end)
    return {
        "events": [(e.state, e.start, e.minutes, e.start_boundary, e.end_boundary) for e in activity_events(tl)],
        "runs": [(r.start, r.minutes, r.start_boundary, r.end_boundary, r.energy) for r in compressor_runs(tl)],
        "offs": [(i.start, i.minutes, i.start_observed, i.end_observed) for i in compressor_off_intervals(tl)],
        "defrosts": [(d.start, d.minutes, d.observed_defrost_seconds, d.start_boundary, d.end_boundary)
                     for d in defrosts(tl)],
        "gaps": [i for i in tl.items if isinstance(i, Gap)],
        "summary": s,
    }


def test_durable_segments_reproduce_every_fact_after_purge(any_storage):
    scenario(any_storage)
    window = (T0, T0 + 2 * H, T0 + 5 * H)
    before_raw = timeline(build_segments(raw(any_storage, T0, T0 + 2 * H)), *window)
    before_durable = timeline(durable(any_storage, T0, T0 + 2 * H), *window)
    assert before_durable == before_raw
    facts = _facts(before_raw)
    purge_all(any_storage)
    assert raw(any_storage, T0, T0 + 2 * H) == []
    after = timeline(durable(any_storage, T0, T0 + 2 * H), *window)
    assert after == before_raw
    assert _facts(after) == facts
    # The concrete facts the scenario was built to keep.
    runs = compressor_runs(after)
    assert [((r.start - T0) // M, r.minutes, r.start_observed, r.end_observed) for r in runs] == [
        (5, 8, True, True), (15, 12, True, False), (28, 2, False, False), (57, 6, True, True)]
    assert len(runs[3].segments) == 2  # 09:57 → 10:03 UTC: two hour-local rows, one run
    assert facts["gaps"] == [Gap(T0 + 30 * M, T0 + 32 * M)]
    [d] = defrosts(after)
    assert ((d.start - T0) // M, d.minutes, d.start_observed, d.end_observed) == (20, 4, True, True)
    assert d.observed_defrost_seconds == pytest.approx(165.0, abs=1e-3)
    assert [s.defrost_fraction for s in d.segments] == [0.583333, 1.0, 0.166667]
    s = facts["summary"]
    assert (s.observed_starts, s.observed_stops, s.gap_minutes, s.activity_minutes["unknown"]) == (3, 2, 2, 1)
    assert s.exact_off_interval_minutes == (2,)


def test_durable_energy_and_paired_cop_match_raw_and_history(any_storage):
    scenario(any_storage)
    rows = raw(any_storage, T0, T0 + 2 * H)
    with any_storage.session() as s:
        history = {}
        for h, series, n, *stats in s.read_rollup(T0, T0 + 2 * H, ENERGY_SERIES):
            history = combine_maps(history, {series: Stats(n, *stats)})
    purge_all(any_storage)
    stored = durable(any_storage, T0, T0 + 2 * H)
    assert fold_energy(stored) == fold_energy(build_segments(rows))
    # Dyadic power values make every association exact, so durable equals the canonical rollup.
    assert fold_energy(stored) == {k: history[k] for k in ENERGY_SERIES if k in history}
    tl = timeline(stored, T0, T0 + 2 * H, T0 + 5 * H)
    raw_tl = timeline(build_segments(rows), T0, T0 + 2 * H, T0 + 5 * H)
    for run, raw_run in zip(compressor_runs(tl), compressor_runs(raw_tl), strict=True):
        own = [r for r in rows if run.start <= r[0] < run.end]
        assert run.energy == raw_run.energy == fold_minutes(own, ENERGY_SERIES)
        for name, (i, o) in (("co", ("pair_co_in", "pair_co_out")), ("dhw", ("pair_dhw_in", "pair_dhw_out")),
                             ("total", ("pair_total_in", "pair_total_out"))):
            assert cop(run.energy.get(i), run.energy.get(o)) == cop(raw_run.energy.get(i), raw_run.energy.get(o))
        for k in ENERGY_SERIES[:4]:
            assert energy_kwh(run.energy.get(k)) == energy_kwh(raw_run.energy.get(k))
    cross_hour = compressor_runs(tl)[3]
    assert cop(cross_hour.energy["pair_co_in"], cross_hour.energy["pair_co_out"]) == {
        "cop": 3600.25 / 900.5, "paired_minutes": 6, "input_kwh": 6 * 900.5 / 60000,
        "output_kwh": 6 * 3600.25 / 60000}


def test_run_across_warsaw_midnight_is_stitched_from_hour_local_rows(any_storage):
    midnight = local_midnight(date(2027, 1, 16))
    start = midnight - 5 * M
    put(any_storage, start, [OFF, OFF, CO, CO, CO, CO, CO, OFF] + [OFF] * (3 * 60))
    purge_all(any_storage, now=midnight + 400 * DAY)
    assert raw(any_storage, start, midnight + H) == []
    tl = timeline(durable(any_storage, start, midnight + H), start, midnight + H, midnight + 5 * H)
    [run] = compressor_runs(tl)
    assert (run.start, run.end, run.start_observed, run.end_observed, len(run.segments)) == (
        midnight - 3 * M, midnight + 2 * M, True, True, 2)
    day1 = summarize(tl, start, midnight)
    day2 = summarize(tl, midnight, midnight + H)
    assert (day1.observed_starts, day1.observed_stops, day2.observed_starts, day2.observed_stops) == (1, 0, 0, 1)
    assert day1.complete_run_minutes == (5,)


# ------------------------------------------------------------------ concurrency (MariaDB)

WAIT = 5.0


def _pause_in_thread(monkeypatch, method, thread_name):
    """Pause ``Session.<method>`` in one named thread until released; returns (paused, release)."""
    paused, release = threading.Event(), threading.Event()
    original = getattr(storage_module.Session, method)

    def wrapper(session, *args, **kwargs):
        if threading.current_thread().name == thread_name:
            paused.set()
            assert release.wait(WAIT)
        return original(session, *args, **kwargs)

    monkeypatch.setattr(storage_module.Session, method, wrapper)
    return paused, release


def _run(name, fn, result):
    def target():
        try:
            result[name] = fn()
        except Exception as e:  # noqa: BLE001 - the test inspects it
            result[name] = e
    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread


def test_backfill_waits_for_a_purge_that_holds_the_proof(mariadb, monkeypatch):
    """Purge refuses unmaterialized hours; the blocked backfill then materializes them."""
    mariadb.ensure_schema()
    _pre_4c(mariadb, 4)
    minutes_before = len(raw(mariadb))
    paused, release = _pause_in_thread(monkeypatch, "read_activity_segments", "purge")
    result = {}
    purge = _run("purge", lambda: purge_step(mariadb, NOW, 365, None, 24), result)
    assert paused.wait(WAIT)
    backfill = _run("backfill", lambda: backfill_activity_step(mariadb, 0, 24), result)
    time.sleep(0.4)
    assert "backfill" not in result  # serialized behind the purge's policy-head lock
    release.set()
    purge.join(WAIT)
    backfill.join(WAIT)
    assert isinstance(result["purge"], PurgeRefused)
    assert result["backfill"][1:] == ([T0, T0 + H, T0 + 2 * H, T0 + 3 * H], True)
    assert len(raw(mariadb)) == minutes_before
    monkeypatch.undo()
    assert purge_all(mariadb) == 2 * 60
    assert len(durable(mariadb)) == 8


def test_late_write_racing_purge_never_commits_a_mixed_hour(mariadb, monkeypatch):
    """A late minute for a hour being purged is serialized after it and refused."""
    mariadb.ensure_schema()
    scenario(mariadb)
    kept = records(mariadb, T0, T0 + H), rollup_facts(mariadb, T0, T0 + H)
    paused, release = _pause_in_thread(monkeypatch, "read_activity_segments", "purge")
    result = {}
    purge = _run("purge", lambda: purge_step(mariadb, NOW, 365, None, 24), result)
    assert paused.wait(WAIT)
    late = _run("late", lambda: persist_canonical(mariadb, rows_of(T0 + 2 * M, [DHW])), result)
    time.sleep(0.4)
    assert "late" not in result
    release.set()
    purge.join(WAIT)
    late.join(WAIT)
    assert result["purge"][1] > 0
    assert isinstance(result["late"], RebuildRefused), repr(result["late"])
    assert raw(mariadb, T0, T0 + H) == []
    assert (records(mariadb, T0, T0 + H), rollup_facts(mariadb, T0, T0 + H)) == kept


def test_late_write_before_purge_is_in_the_proof(mariadb, monkeypatch):
    """The other order: the late write commits first, purge then proves its rebuilt segments."""
    mariadb.ensure_schema()
    scenario(mariadb)
    paused, release = _pause_in_thread(monkeypatch, "replace_activity_hour", "late")
    result = {}
    late = _run("late", lambda: persist_canonical(mariadb, rows_of(T0 + 2 * M, [DHW])), result)
    assert paused.wait(WAIT)
    purge = _run("purge", lambda: purge_step(mariadb, NOW, 365, None, 24), result)
    time.sleep(0.4)
    assert "purge" not in result
    release.set()
    late.join(WAIT)
    purge.join(WAIT)
    assert result["late"] is None and result["purge"][1] > 0
    [dhw] = [s for s in durable(mariadb, T0, T0 + H) if s.start == T0 + 2 * M]
    assert dhw.activity is Activity.DHW


def test_backfill_snapshot_older_than_a_purge_rebuilds_nothing_purged(mariadb, monkeypatch):
    """Backfill's snapshot predates its head lock; a purge committed meanwhile still wins.

    Its scan starts at the current oldest raw minute, so the purged prefix is never revisited.
    """
    mariadb.ensure_schema()
    _pre_4c(mariadb, 4)
    paused, release = _pause_in_thread(monkeypatch, "lock_policy_head", "backfill")
    result = {}
    backfill = _run("backfill", lambda: backfill_activity_step(mariadb, 0, 24), result)
    assert paused.wait(WAIT)  # its transaction snapshot is already taken
    with mariadb.session() as s:
        rebuild_hour(s, T0)
        rebuild_hour(s, T0 + H)
    assert purge_all(mariadb) == 2 * 60  # hours 0 and 1, proven and deleted
    kept = records(mariadb, T0, T0 + 2 * H), rollup_facts(mariadb, T0, T0 + 2 * H)
    release.set()
    backfill.join(WAIT)
    assert result["backfill"][1:] == ([T0 + 2 * H, T0 + 3 * H], True)
    assert (records(mariadb, T0, T0 + 2 * H), rollup_facts(mariadb, T0, T0 + 2 * H)) == kept
    assert len(durable(mariadb)) == 8
