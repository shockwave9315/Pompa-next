"""Recorder: event coordination, bounded write buffer, outage and recovery."""

import threading

from conftest import RUNNING, T0, FakeStorage
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder
from pompa.storage import StorageUnavailable


def make(start=T0, buffer_rows=60, storage=None, stale=600):
    ingest = Ingest(stale)
    storage = storage or FakeStorage()
    return Recorder(ingest, MinuteAccumulator(ingest, start), storage, buffer_rows), storage


def feed(rec, start, end, step=10):
    t = start
    while t < end:
        for topic, payload in RUNNING.items():
            rec.on_message(topic, payload, False, t)
        t += step


def test_rows_flow_to_storage():
    rec, db = make()
    rec.on_connect(T0)
    feed(rec, T0, T0 + 180)
    rec.tick(T0 + 180)
    assert sorted(db.rows) == [T0, T0 + 60, T0 + 120]
    assert db.rows[T0]["main_outlet_temp"] == 35.0
    assert (rec.rows_closed, rec.rows_written, rec.dropped_rows) == (3, 3, 0)
    assert rec.last_written_minute == T0 + 120
    assert rec.snapshot(T0 + 180)["recorder"]["buffered_rows"] == 0


def test_db_outage_buffers_then_recovery_flushes():
    rec, db = make()
    db.available = False
    rec.on_connect(T0)
    for m in range(3):
        feed(rec, T0 + 60 * m, T0 + 60 * (m + 1))
        rec.tick(T0 + 60 * (m + 1))
    assert db.rows == {}
    assert not rec.schema_ready
    assert db.schema_calls == 3  # schema bootstrap retried on every tick
    snap = rec.snapshot(T0 + 180)["recorder"]
    assert snap["buffered_rows"] == 3
    assert snap["db_last_error"] == "fake outage"
    db.available = True
    rec.tick(T0 + 181)
    assert sorted(db.rows) == [T0, T0 + 60, T0 + 120]
    assert rec.schema_ready and rec.db_last_error is None
    snap = rec.snapshot(T0 + 181)["recorder"]
    assert (snap["buffered_rows"], snap["dropped_rows"]) == (0, 0)
    calls = db.upsert_calls
    rec.tick(T0 + 182)  # nothing pending: no further write
    assert db.upsert_calls == calls and db.schema_calls == 4


class AckLostStorage(FakeStorage):
    """Writes succeed but the first acknowledgement is lost."""

    def __init__(self):
        super().__init__()
        self.lose_ack = True

    def upsert(self, rows):
        super().upsert(rows)
        if self.lose_ack:
            self.lose_ack = False
            raise StorageUnavailable("ack lost")


def test_repeated_flush_is_safe():
    rec, db = make(storage=AckLostStorage())
    rec.on_connect(T0)
    feed(rec, T0, T0 + 120)
    rec.tick(T0 + 120)
    assert len(rec._buffer) == 2 and sorted(db.rows) == [T0, T0 + 60]
    before = {ts: dict(v) for ts, v in db.rows.items()}
    rec.tick(T0 + 121)
    assert db.rows == before
    assert len(rec._buffer) == 0 and rec.rows_written == 2


def test_overflow_drops_oldest_and_counts():
    rec, db = make(buffer_rows=3)
    db.available = False
    rec.on_connect(T0)
    feed(rec, T0, T0 + 300)
    rec.tick(T0 + 300)
    assert rec.rows_closed == 5
    assert rec.dropped_rows == 2
    assert [r.ts for r in rec._buffer] == [T0 + 120, T0 + 180, T0 + 240]
    db.available = True
    rec.tick(T0 + 301)
    assert sorted(db.rows) == [T0 + 120, T0 + 180, T0 + 240]  # the loss stays a visible gap
    assert rec.snapshot(T0 + 301)["recorder"]["dropped_rows"] == 2


def test_event_before_cursor_is_clamped():
    rec, _ = make()
    rec.on_connect(T0)
    rec.tick(T0 + 100)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 50)
    assert rec.ingest.last_live_at == T0 + 100
    assert rec.accumulator.cursor == T0 + 100


def test_disconnect_at_boundary_closes_minute_first():
    rec, db = make()
    rec.on_connect(T0)
    feed(rec, T0, T0 + 60)
    rec.on_disconnect(T0 + 60)
    rec.tick(T0 + 300)
    assert sorted(db.rows) == [T0]


def test_disconnect_inside_minute_loses_that_minute():
    rec, db = make()
    rec.on_connect(T0)
    feed(rec, T0, T0 + 60)
    rec.on_disconnect(T0 + 59.9)
    rec.tick(T0 + 300)
    assert db.rows == {}


def test_retained_lwt_and_messages_after_reconnect_record_nothing():
    rec, db = make()
    rec.on_connect(T0)
    rec.on_lwt("Online", True, T0)
    for topic, payload in RUNNING.items():
        rec.on_message(topic, payload, True, T0 + 1)
    rec.tick(T0 + 900)
    assert db.rows == {}
    snap = rec.snapshot(T0 + 900)
    assert snap["mqtt"]["alive"] is False
    assert snap["mqtt"]["lwt"] == {"state": "Online", "retained": True,
                                   "received_at": "2027-01-15T08:00:00Z", "messages": 1}


def test_snapshot_facts():
    rec, _ = make(start=T0 + 30)
    rec.on_connect(T0 + 30)
    rec.on_message("main/Outside_Temp", "abc", False, T0 + 31)
    rec.on_message("main/DHW_Target_Temp", "48", False, T0 + 31)
    feed(rec, T0 + 40, T0 + 200)
    rec.tick(T0 + 200)
    snap = rec.snapshot(T0 + 200)
    mqtt, recorder = snap["mqtt"], snap["recorder"]
    assert mqtt["connected"] and mqtt["alive"] and mqtt["epoch"] == 1
    assert mqtt["alive_since"] == "2027-01-15T08:00:31Z"
    assert mqtt["last_live_message_at"] == "2027-01-15T08:03:10Z"
    assert mqtt["parse_rejects"] == 1
    assert mqtt["uncatalogued_topics"] == ["main/DHW_Target_Temp"]
    assert recorder["process_start"] == "2027-01-15T08:00:30Z"
    assert recorder["last_closed_minute"] == "2027-01-15T08:02:00Z"
    assert recorder["last_written_minute"] == "2027-01-15T08:02:00Z"
    assert recorder["rows_written"] == 2  # minutes 1 and 2; minute 0 predates process start
    xtop0 = next(s for s in snap["sources"] if s["id"] == "XTOP0")
    assert xtop0["seen_live"] and xtop0["historical_value"] == 900.0
    assert xtop0["max_live_gap_seconds"] == 10
    assert xtop0["topic"] == "extra/Heat_Power_Consumption_Extra"


def test_concurrent_events_and_ticks():
    rec, db = make()
    rec.on_connect(T0)
    errors = []

    def mqtt_thread():
        try:
            feed(rec, T0, T0 + 3600, step=5)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    def recorder_thread():
        try:
            for i in range(2000):
                rec.tick(T0 + i)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=mqtt_thread), threading.Thread(target=recorder_thread)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    rec.tick(T0 + 3600)
    assert errors == []
    assert all(ts % 60 == 0 for ts in db.rows)
    assert rec.dropped_rows == 0 and rec.rows_written == len(db.rows) > 0
    assert len(rec._buffer) == 0


class BlockingStorage(FakeStorage):
    """upsert() records its claimed rows, then blocks until released."""

    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail
        self.claimed = None
        self.started = threading.Event()
        self.release = threading.Event()

    def upsert(self, rows):
        self.claimed = [r.ts for r in rows]
        self.started.set()
        assert self.release.wait(5), "test never released the write"
        if self.fail:
            self.upsert_calls += 1
            raise StorageUnavailable("write failed")
        super().upsert(rows)


def full_buffer_then_blocked_flush(capacity, storage):
    """Buffer full after an outage; a recovery flush is blocked inside upsert()."""
    rec, _ = make(buffer_rows=capacity, storage=storage)
    storage.available = False
    rec.on_connect(T0)
    feed(rec, T0, T0 + 60 * capacity)
    rec.tick(T0 + 60 * capacity)
    assert [r.ts for r in rec._buffer] == [T0 + 60 * m for m in range(capacity)]
    assert rec.dropped_rows == 0
    storage.available = True
    flush = threading.Thread(target=rec.tick, args=(T0 + 60 * capacity + 1,))
    flush.start()
    assert storage.started.wait(5)
    return rec, flush


def minutes_ts(*offsets):
    return [T0 + 60 * m for m in offsets]


def test_row_persisted_by_in_flight_flush_is_not_counted_as_dropped():
    db = BlockingStorage()
    rec, flush = full_buffer_then_blocked_flush(3, db)
    assert db.claimed == minutes_ts(0, 1, 2)
    # MQTT keeps closing minutes while the write is blocked (no lock wait).
    feed(rec, T0 + 181, T0 + 300)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 300)
    snap = rec.snapshot(T0 + 300)["recorder"]
    assert (snap["buffered_rows"], snap["in_flight_rows"], snap["dropped_rows"]) == (5, 3, 0)
    db.release.set()
    flush.join(5)
    assert sorted(db.rows) == minutes_ts(0, 1, 2)
    assert [r.ts for r in rec._buffer] == minutes_ts(3, 4)  # FIFO preserved
    assert (rec.dropped_rows, rec.rows_written, rec._in_flight) == (0, 3, 0)
    rec.tick(T0 + 301)
    assert sorted(db.rows) == minutes_ts(0, 1, 2, 3, 4)
    assert rec.dropped_rows == 0 and len(rec._buffer) == 0


def test_failed_in_flight_flush_then_drops_oldest():
    db = BlockingStorage(fail=True)
    rec, flush = full_buffer_then_blocked_flush(3, db)
    feed(rec, T0 + 181, T0 + 240)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 240)  # closes minute 3
    assert rec.dropped_rows == 0  # outcome of minute 0 still unknown
    db.release.set()
    flush.join(5)
    assert db.rows == {}
    assert [r.ts for r in rec._buffer] == minutes_ts(1, 2, 3)  # minute 0 was the real loss
    assert rec.dropped_rows == 1 and rec._in_flight == 0


def test_row_doomed_whatever_the_outcome_is_dropped_immediately():
    db = BlockingStorage()
    rec, flush = full_buffer_then_blocked_flush(2, db)
    assert db.claimed == minutes_ts(0, 1)
    feed(rec, T0 + 121, T0 + 300)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 300)  # closes minutes 2, 3, 4
    # Three unclaimed rows exceed capacity 2: minute 2 is lost on either outcome.
    assert [r.ts for r in rec._buffer] == minutes_ts(0, 1, 3, 4)
    assert rec.dropped_rows == 1
    db.release.set()
    flush.join(5)
    assert sorted(db.rows) == minutes_ts(0, 1)
    assert [r.ts for r in rec._buffer] == minutes_ts(3, 4)
    assert rec.dropped_rows == 1
