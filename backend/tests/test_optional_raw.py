import pytest

from conftest import Api, T0, FakeStorage, row
from pompa.history_profile import HISTORY_PROFILES_BY_IDENTITY
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.optional_minute import OptionalMinute
from pompa.optional_policy import replace_selection, resolve_series_id
from pompa.recorder import RecordedMinute, Recorder, PurgeRefused, persist, purge_step, roll_next_hour
from pompa.storage import StorageUnavailable
from pompa.timegrid import HOUR


def select(storage, identities, effective, base=1):
    with storage.session() as s:
        assert s.lock_policy_head() == base
        ids = [resolve_series_id(s, HISTORY_PROFILES_BY_IDENTITY[name], effective) for name in identities]
        rev = s.insert_revision(base, effective, effective)
        s.insert_revision_members(rev, ids)
        s.update_policy_head(rev)
    return rev


def pair(ts, **values):
    return RecordedMinute(row(ts, outside_temp=1.0), OptionalMinute(ts, values))


def optional(storage, start=T0, end=T0 + 600):
    with storage.session() as s:
        return dict(s.read_optional_minutes(start, end))


@pytest.mark.parametrize("identity,value", [("TOP21", 0.0), ("XTOP1", 0.0)])
def test_selected_zero_and_unknown(any_storage, identity, value):
    db = any_storage
    select(db, [identity], T0)
    persist(db, [pair(T0, **{identity: value}), pair(T0 + 60)])
    rows = optional(db)
    assert len(rows) == 1 and list(rows[T0].values()) == [0.0]
    with db.session() as s:
        assert len(s.read_minutes(T0, T0 + 120)) == 2


def test_replace_complete_document_and_delete_on_unknown(any_storage):
    db = any_storage
    select(db, ["TOP21", "TOP50"], T0)
    persist(db, [pair(T0, TOP21=10.0, TOP50=20.0)])
    assert len(optional(db)[T0]) == 2
    persist(db, [pair(T0, TOP21=11.0)])
    assert list(optional(db)[T0].values()) == [11.0]
    persist(db, [pair(T0)])
    assert optional(db) == {}


def test_batch_crosses_revision_and_equal_boundary_descendant(any_storage):
    db = any_storage
    first = select(db, ["TOP21"], T0 + 60)
    second = select(db, ["TOP50"], T0 + 120, first)
    select(db, ["TOP21", "TOP50"], T0 + 120, second)
    persist(db, [pair(T0, TOP21=1.0, TOP50=2.0),
                 pair(T0 + 60, TOP21=3.0, TOP50=4.0),
                 pair(T0 + 120, TOP21=5.0, TOP50=6.0)])
    rows = optional(db)
    assert T0 not in rows
    assert len(rows[T0 + 60]) == 1 and list(rows[T0 + 60].values()) == [3.0]
    assert len(rows[T0 + 120]) == 2


def test_optional_write_failure_rolls_back_canonical(mariadb):
    db = mariadb
    db.ensure_schema()
    select(db, ["TOP21"], T0)
    with db._connection() as conn, conn.cursor() as cur:
        cur.execute("DROP TRIGGER IF EXISTS reject_optional_raw")
        cur.execute("CREATE TRIGGER reject_optional_raw BEFORE INSERT ON optional_sample_1m"
                    " FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'reject optional'")
        conn.commit()
    try:
        with pytest.raises(StorageUnavailable):
            persist(db, [pair(T0, TOP21=1.0)])
        with db.session() as s:
            assert s.read_minutes(T0, T0 + 60) == []
            assert s.read_optional_minutes(T0, T0 + 60) == []
    finally:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute("DROP TRIGGER IF EXISTS reject_optional_raw")
            conn.commit()


def test_fk_prevents_orphan(mariadb):
    db = mariadb
    db.ensure_schema()
    with pytest.raises(StorageUnavailable):
        with db.session() as s:
            s.replace_optional_minute(T0, {"1": 2.0})
    select(db, ["TOP21"], T0)
    persist(db, [pair(T0, TOP21=2.0)])
    with pytest.raises(StorageUnavailable):
        with db.session() as s:
            s.delete_minutes_before(T0 + 60)
    assert T0 in optional(db)


def test_ambiguous_commit_retry_preserves_pair(any_storage):
    db = any_storage
    select(db, ["TOP21"], T0)
    p = pair(T0, TOP21=12.0)
    if isinstance(db, FakeStorage):
        db.fail_commit, db.ack_lost = 1, True
        with pytest.raises(StorageUnavailable):
            persist(db, [p])
    else:
        original = db.session
        from contextlib import contextmanager

        @contextmanager
        def lost_ack():
            with original() as s:
                yield s
            raise StorageUnavailable("ack lost")

        db.session = lost_ack
        try:
            with pytest.raises(StorageUnavailable):
                persist(db, [p])
        finally:
            db.session = original
    persist(db, [p])
    assert list(optional(db)[T0].values()) == [12.0]


def test_purge_interlock_and_canonical_rollup(any_storage):
    db = any_storage
    select(db, ["TOP21"], T0)
    persist(db, [pair(T0, TOP21=2.0)])
    assert roll_next_hour(db, T0 + HOUR) == T0
    persist(db, [pair(T0 + 3 * HOUR)])
    assert roll_next_hour(db, T0 + 4 * HOUR) == T0 + 3 * HOUR
    with pytest.raises(PurgeRefused):
        purge_step(db, T0 + 100 * HOUR, 1, None, 24)
    assert optional(db)[T0]
    with db.session() as s:
        assert len(s.read_minutes(T0, T0 + 60)) == 1


def test_continuous_observation_and_future_selection():
    api = Api(start=T0)
    api.connect(T0).msg(T0, "main/Main_Outlet_Temp", "35")
    api.msg(T0 + 10, "main/Outside_Pipe_Temp", "21.25")
    result = replace_selection(api.recorder, api.storage, 1, ["TOP21"], lambda: T0 + 27)
    assert result.revision.effective_from_minute == T0 + 60
    api.tick(T0 + 120)
    assert T0 not in optional(api.storage)
    assert list(optional(api.storage)[T0 + 60].values()) == [21.25]


def test_retained_only_is_live_but_never_optional_history():
    api = Api(start=T0)
    select(api.storage, ["TOP21"], T0)
    api.connect(T0).msg(T0, "main/Main_Outlet_Temp", "35")
    api.msg(T0, "main/Outside_Pipe_Temp", "21", retained=True)
    assert api.ingest.physical_readings["TOP21"].payload.value == 21.0
    assert not api.ingest.optional_sources["main/Outside_Pipe_Temp"].seen_live
    api.tick(T0 + 60)
    assert T0 in api.storage.rows and optional(api.storage) == {}
    api.msg(T0 + 60, "main/Outside_Pipe_Temp", "22")
    api.tick(T0 + 120)
    assert list(optional(api.storage)[T0 + 60].values()) == [22.0]


def test_blocked_member_is_selected_but_unknown(any_storage, monkeypatch):
    db = any_storage
    select(db, ["TOP21"], T0)
    monkeypatch.setattr("pompa.recorder.HISTORY_PROFILES_BY_IDENTITY", {})
    persist(db, [pair(T0, TOP21=5.0)])
    assert optional(db) == {}
    with db.session() as s:
        assert len(s.read_minutes(T0, T0 + 60)) == 1
        assert len(s.lock_revision_members(s.lock_policy_head())) == 1


def test_delayed_protected_pair_uses_its_minute_policy():
    db = FakeStorage()
    select(db, ["TOP21"], T0)
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), db, 2)
    rec.on_connect(T0)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0)
    rec.on_message("main/Outside_Pipe_Temp", "8", False, T0)
    db.available = False
    rec.tick(T0 + 120)
    assert len(rec._protected) == 2
    protected = tuple(rec._protected)
    db.available = True
    result = replace_selection(rec, db, db.optional_head, [], lambda: T0 + 125)
    assert result.revision.effective_from_minute >= T0 + 180
    assert tuple(rec._protected) == protected
    rec.tick(T0 + 120)
    assert rec._protected == [] and rec.rows_written == 2
    assert len(optional(db)) == 2


def test_waiting_overflow_discards_whole_pair():
    db = FakeStorage()
    select(db, ["TOP21"], T0)
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), db, 1)
    rec.on_connect(T0)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0)
    rec.on_message("main/Outside_Pipe_Temp", "9", False, T0)
    db.available = False
    for n in (1, 2, 3):
        rec.tick(T0 + n * 60)
    assert rec.dropped_rows == 1
    assert [p.ts for p in rec._protected] == [T0]
    assert [p.ts for p in rec._waiting] == [T0 + 120]
    db.available = True
    rec.tick(T0 + 180)
    assert sorted(db.rows) == [T0, T0 + 120]
    assert sorted(optional(db)) == [T0, T0 + 120]


def test_rebuild_refusal_removes_whole_pair():
    db = FakeStorage()
    persist(db, [row(T0, outside_temp=1.0), row(T0 + 3 * HOUR, outside_temp=2.0)])
    assert roll_next_hour(db, T0 + HOUR) == T0
    assert roll_next_hour(db, T0 + 4 * HOUR) == T0 + 3 * HOUR
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 24)[1] == 1
    select(db, ["TOP21"], T0)
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0 + 4 * HOUR), db, 2)
    refused = pair(T0, TOP21=3.0)
    accepted = pair(T0 + 4 * HOUR, TOP21=4.0)
    rec._waiting.extend((refused, accepted))
    rec.tick(T0 + 4 * HOUR)
    assert rec.refused_rows == 1 and rec.rows_written == 1
    assert T0 not in optional(db)
    assert list(optional(db, T0 + 4 * HOUR, T0 + 5 * HOUR)[accepted.ts].values()) == [4.0]


def test_optional_events_and_expiry_do_not_change_canonical():
    baseline = Api(start=T0)
    changed = Api(start=T0)
    for api in (baseline, changed):
        api.connect(T0).msg(T0, "main/Main_Outlet_Temp", "30")
    for t, value in [(T0 + 7, "1"), (T0 + 17, "5"), (T0 + 43, "0")]:
        changed.msg(t, "main/Outside_Pipe_Temp", value)
    for api in (baseline, changed):
        api.msg(T0 + 55, "main/Main_Outlet_Temp", "40")
        api.msg(T0 + 70, "main/Main_Outlet_Temp", "41")
    changed.tick(T0 + 660)  # optional expiry occurs inside this walk
    baseline.tick(T0 + 660)
    assert T0 + 600 in baseline.storage.rows
    def bits(rows):
        return {ts: {key: None if value is None else value.hex() for key, value in values.items()}
                for ts, values in rows.items()}
    assert bits(baseline.storage.rows) == bits(changed.storage.rows)
