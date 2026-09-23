"""Checkpoint D optional rollup and shared purge against real MariaDB."""

from contextlib import contextmanager

import pytest

from conftest import T0, row
from pompa import history
from pompa.aggregation import (OptionalHistoryInconsistent, OptionalStats, Stats, combine_optional,
                               fold_optional_minutes)
from pompa.history_profile import HISTORY_PROFILES_BY_IDENTITY
from pompa.optional_minute import OptionalMinute
from pompa.optional_policy import resolve_series_id
from pompa.recorder import RecordedMinute, PurgeRefused, persist, purge_step, roll_next_hour
from pompa.storage import Session, StorageUnavailable
from pompa.timegrid import HOUR


def select(db, names, effective, base=None):
    with db.session() as s:
        head = s.lock_policy_head()
        assert base is None or head == base
        ids = [resolve_series_id(s, HISTORY_PROFILES_BY_IDENTITY[name], effective) for name in names]
        rev = s.insert_revision(head, effective, effective)
        s.insert_revision_members(rev, ids)
        s.update_policy_head(rev)
    return rev


def pair(ts, **values):
    return RecordedMinute(row(ts, outside_temp=1.0), OptionalMinute(ts, values))


def roll_all(db, end):
    while roll_next_hour(db, end) is not None:
        pass


def roll_rows(db, start=T0, end=T0 + HOUR):
    with db.session() as s:
        return s.read_optional_rollup(start, end)


def test_optional_stats_pure_fold_selected_unknown_zero_and_order():
    class Member:
        def __init__(self, sid):
            self.id = sid
    selected = {T0: [Member(7), Member(8)], T0 + 60: [Member(7)], T0 + 180: [Member(7)]}
    result = fold_optional_minutes([T0, T0 + 60, T0 + 180], selected,
                                   {T0: {"7": 0.0}, T0 + 180: {"7": 3.0}})
    assert result[7] == OptionalStats(3, 2, Stats(2, 3.0, 0.0, 3.0, 3.0))
    assert result[8] == OptionalStats(1, 0, None)
    assert combine_optional(OptionalStats(1, 1, Stats.of(1.0)),
                            OptionalStats(1, 1, Stats.of(2.0))).values.last == 2.0
    with pytest.raises(OptionalHistoryInconsistent):
        fold_optional_minutes([T0], {T0: []}, {T0: {"7": 2.0}})
    with pytest.raises(OptionalHistoryInconsistent):
        fold_optional_minutes([T0], {T0: [Member(7)]}, {T0: [7.0]})


def test_optional_rollup_ddl_and_checks(mariadb):
    db = mariadb
    db.ensure_schema()
    db.ensure_schema()
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'optional_rollup_1h'"
                    " ORDER BY ORDINAL_POSITION")
        assert cur.fetchall() == (
            ("hour_ts", "int", "NO"), ("series_id", "int", "NO"),
            ("selected_minutes", "smallint", "NO"), ("known_minutes", "smallint", "NO"),
            ("v_sum", "double", "YES"), ("v_min", "double", "YES"),
            ("v_max", "double", "YES"), ("v_last", "double", "YES"))
        cur.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
                    " AND TABLE_NAME = 'optional_rollup_1h'")
        assert cur.fetchone() == ("InnoDB",)
        cur.execute("SHOW CREATE TABLE optional_rollup_1h")
        ddl = cur.fetchone()[1]
        assert "PRIMARY KEY (`hour_ts`,`series_id`)" in ddl
        assert "FOREIGN KEY (`series_id`)" in ddl and "ON DELETE CASCADE" not in ddl
    select(db, ["TOP21"], T0)
    with db.session() as s:
        sid = s.list_optional_series()[0].id
    bad = [(0, 0, None, None, None, None), (1, 2, 3., 3., 3., 3.),
           (1, 0, 3., None, None, None), (1, 1, None, None, None, None)]
    for fields in bad:
        with pytest.raises(StorageUnavailable):
            with db.session() as s:
                s.replace_optional_rollup_hour(T0, [(sid, *fields)])
    with db.session() as s:
        s.replace_optional_rollup_hour(T0, [(sid, 1, 0, None, None, None, None)])
        assert s.read_optional_rollup(T0, T0 + HOUR)[0][2:] == (1, 0, None, None, None, None)


def test_hour_rollup_policy_boundary_gaps_zero_and_unknown(mariadb):
    db = mariadb
    db.ensure_schema()
    first = select(db, ["TOP21", "XTOP1"], T0 + 20 * 60)
    select(db, ["TOP21", "XTOP1"], T0 + 20 * 60, first)  # latest descendant wins tie
    persist(db, [pair(T0, TOP21=90), pair(T0 + 20 * 60, TOP21=0.0, XTOP1=100.0),
                 pair(T0 + 22 * 60), pair(T0 + 59 * 60, TOP21=-2.5, XTOP1=0.0)])
    assert roll_next_hour(db, T0 + HOUR) == T0
    rows = roll_rows(db)
    assert len(rows) == 2
    by_id = {sid: (selected, known, total, v_min, v_max, v_last)
             for _, sid, selected, known, total, v_min, v_max, v_last in rows}
    with db.session() as s:
        ids = {r.identity: r.id for r in s.list_optional_series()}
    assert by_id[ids["TOP21"]] == (3, 2, -2.5, -2.5, 0.0, -2.5)
    assert by_id[ids["XTOP1"]] == (3, 2, 100.0, 0.0, 100.0, 0.0)


def test_late_rewrite_replaces_complete_optional_hour(mariadb):
    db = mariadb
    db.ensure_schema()
    select(db, ["TOP21", "TOP50"], T0)
    persist(db, [pair(T0, TOP21=10, TOP50=4), pair(T0 + 60)])
    roll_next_hour(db, T0 + HOUR)
    with db.session() as s:
        ids = {r.identity: r.id for r in s.list_optional_series()}
    persist(db, [pair(T0, TOP21=20, TOP50=4)])
    by_id = {r[1]: r for r in roll_rows(db)}
    assert by_id[ids["TOP21"]][2:5] == (2, 1, 20.0)
    persist(db, [pair(T0)])
    by_id = {r[1]: r for r in roll_rows(db)}
    assert by_id[ids["TOP21"]][2:] == (2, 0, None, None, None, None)
    assert by_id[ids["TOP50"]][2:] == (2, 0, None, None, None, None)
    persist(db, [pair(T0 + 60, TOP21=7)])
    by_id = {r[1]: r for r in roll_rows(db)}
    assert by_id[ids["TOP21"]][2:5] == (2, 1, 7.0)
    assert by_id[ids["TOP50"]][2:4] == (2, 0)
    with db.session() as s:
        assert s.read_rollup(T0, T0 + HOUR, ["recorded"])[0][2] == 2


def test_optional_rollup_write_failure_rolls_back_canonical(mariadb):
    db = mariadb
    db.ensure_schema()
    select(db, ["TOP21"], T0)
    persist(db, [pair(T0, TOP21=2)])
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TRIGGER reject_optional_rollup BEFORE INSERT ON optional_rollup_1h"
                    " FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reject rollup'")
        conn.commit()
    try:
        with pytest.raises(StorageUnavailable):
            roll_next_hour(db, T0 + HOUR)
        with db.session() as s:
            assert s.rolled_until() is None
            assert s.read_optional_rollup(T0, T0 + HOUR) == []
            assert len(s.read_minutes(T0, T0 + 60)) == 1
    finally:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute("DROP TRIGGER reject_optional_rollup")
            conn.commit()
    assert roll_next_hour(db, T0 + HOUR) == T0
    assert roll_rows(db)[0][2:4] == (1, 1)


def _purge_ready(db, pairs, names=("TOP21",)):
    db.ensure_schema()
    if names:
        select(db, names, T0)
    persist(db, [*pairs, pair(T0 + 3 * HOUR)])
    roll_all(db, T0 + 4 * HOUR)


@pytest.mark.parametrize("facts,expected", [
    ({"TOP21": 2.0}, (2, 1, 2.0)),
    ({}, (2, 0, None)),
])
def test_shared_purge_preserves_selected_known_or_unknown(mariadb, facts, expected):
    db = mariadb
    _purge_ready(db, [pair(T0, **facts), pair(T0 + 60)])
    assert roll_rows(db)[0][2:5] == expected
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 2)[1] == 2
    with db.session() as s:
        assert s.read_minutes(T0, T0 + HOUR) == []
        assert s.read_optional_minutes(T0, T0 + HOUR) == []
        assert s.read_optional_rollup(T0, T0 + HOUR)[0][2:5] == expected
        assert s.list_optional_series() and s.read_policy_head() > 1
    entry = history.query(db, T0, T0 + HOUR, "1h", ["optional:TOP21@1"], T0 + HOUR)[
        "series"]["optional:TOP21@1"]
    assert entry["selected_minutes"] == [2]
    assert entry["known_minutes"] == [expected[1]]
    assert entry["avg"] == [expected[2] / expected[1] if expected[1] else None]


@pytest.mark.parametrize("damage", ["missing", "selected", "known", "sum", "min", "max", "last", "extra"])
def test_purge_refuses_optional_rollup_mismatch(mariadb, damage):
    db = mariadb
    _purge_ready(db, [pair(T0, TOP21=4.0)])
    with db.session() as s:
        current = s.read_optional_rollup(T0, T0 + HOUR)[0]
        sid = current[1]
        if damage == "missing":
            s.replace_optional_rollup_hour(T0, [])
        elif damage == "extra":
            other = resolve_series_id(s, HISTORY_PROFILES_BY_IDENTITY["TOP50"], T0)
            s.replace_optional_rollup_hour(T0, [(sid, *current[2:]),
                                                (other, 1, 0, None, None, None, None)])
        else:
            values = list(current[2:])
            index = {"selected": 0, "known": 1, "sum": 2, "min": 3, "max": 4, "last": 5}[damage]
            values[index] = 2 if damage == "selected" else (0 if damage == "known" else 8.0)
            if damage == "known":
                values[2:] = [None] * 4
            s.replace_optional_rollup_hour(T0, [(sid, *values)])
    with pytest.raises(PurgeRefused):
        purge_step(db, T0 + 100 * HOUR, 1, None, 2)
    with db.session() as s:
        assert len(s.read_minutes(T0, T0 + 60)) == 1
        assert bool(s.read_optional_minutes(T0, T0 + 60))


def test_purge_refuses_unselected_raw_key(mariadb):
    db = mariadb
    _purge_ready(db, [pair(T0, TOP21=4)])
    with db.session() as s:
        other = resolve_series_id(s, HISTORY_PROFILES_BY_IDENTITY["TOP50"], T0)
        s.replace_optional_minute(T0, {str(other): 5.0})
    with pytest.raises(PurgeRefused, match="unselected"):
        purge_step(db, T0 + 100 * HOUR, 1, None, 2)
    with db.session() as s:
        assert s.read_minutes(T0, T0 + 60)


@pytest.mark.parametrize("failure_at", ["optional", "canonical"])
def test_shared_purge_delete_failure_rolls_back_both_domains(mariadb, monkeypatch, failure_at):
    db = mariadb
    _purge_ready(db, [pair(T0, TOP21=4)])
    method = "delete_optional_minutes" if failure_at == "optional" else "delete_minutes_before"
    original = getattr(Session, method)
    def fail_after_delete(session, *args):
        original(session, *args)
        raise StorageUnavailable("lost connection after delete")
    monkeypatch.setattr(Session, method, fail_after_delete)
    with pytest.raises(StorageUnavailable):
        purge_step(db, T0 + 100 * HOUR, 1, None, 2)
    monkeypatch.setattr(Session, method, original)
    with db.session() as s:
        assert len(s.read_minutes(T0, T0 + 60)) == 1
        assert len(s.read_optional_minutes(T0, T0 + 60)) == 1
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 2)[1] == 1


def test_lost_ack_late_rewrite_converges_all_four_tables(mariadb):
    db = mariadb
    _purge_ready(db, [pair(T0, TOP21=4)])
    rewrite = pair(T0, TOP21=9)
    original = db.session
    @contextmanager
    def lost_ack():
        with original() as session:
            yield session
        raise StorageUnavailable("ack lost")
    db.session = lost_ack
    try:
        with pytest.raises(StorageUnavailable):
            persist(db, [rewrite])
    finally:
        db.session = original
    def facts():
        with db.session() as s:
            return (s.read_minutes(T0, T0 + 60), s.read_optional_minutes(T0, T0 + 60),
                    s.read_rollup(T0, T0 + HOUR, ["recorded", "outside_temp"]),
                    s.read_optional_rollup(T0, T0 + HOUR))
    before = facts()
    persist(db, [rewrite])
    assert facts() == before
