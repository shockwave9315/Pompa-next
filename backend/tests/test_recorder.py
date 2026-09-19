"""Recorder: event coordination, bounded write buffer, outage and recovery."""

import threading
from contextlib import contextmanager

from conftest import RUNNING, T0, FakeStorage
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder
from pompa.storage import StorageUnavailable


def make(start=T0, buffer_rows=60, storage=None, stale=600):
    ingest = Ingest(stale)
    storage = storage or FakeStorage()
    return Recorder(ingest, MinuteAccumulator(ingest, start), storage, buffer_rows), storage


def queued(rec):
    """(protected batch, waiting queue) as minute timestamps."""
    return [r.ts for r in rec._protected], [r.ts for r in rec._waiting]


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
    assert queued(rec) == ([], [])


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
    # Minute 0 was claimed by the first (failed) flush; 1 and 2 never submitted.
    assert (snap["protected_rows"], snap["waiting_rows"]) == (1, 2)
    assert snap["db_last_error"] == "fake outage"
    db.available = True
    rec.tick(T0 + 181)
    assert sorted(db.rows) == [T0, T0 + 60, T0 + 120]
    assert rec.schema_ready and rec.db_last_error is None
    snap = rec.snapshot(T0 + 181)["recorder"]
    assert (snap["protected_rows"], snap["waiting_rows"], snap["dropped_rows"]) == (0, 0, 0)
    calls = db.upsert_calls
    rec.tick(T0 + 182)  # nothing pending: no further write
    assert db.upsert_calls == calls and db.schema_calls == 4


class AckLostStorage(FakeStorage):
    """Writes succeed but the first acknowledgement is lost."""

    def __init__(self):
        super().__init__()
        self.fail_commit, self.ack_lost = 1, True


def test_repeated_flush_is_safe():
    rec, db = make(storage=AckLostStorage())
    rec.on_connect(T0)
    feed(rec, T0, T0 + 120)
    rec.tick(T0 + 120)
    assert queued(rec) == ([T0, T0 + 60], []) and sorted(db.rows) == [T0, T0 + 60]
    before = {ts: dict(v) for ts, v in db.rows.items()}
    rec.tick(T0 + 121)
    assert db.rows == before
    assert queued(rec) == ([], []) and rec.rows_written == 2


def test_overflow_drops_oldest_and_counts():
    rec, db = make(buffer_rows=3)
    db.available = False
    rec.on_connect(T0)
    feed(rec, T0, T0 + 300)
    rec.tick(T0 + 300)
    assert rec.rows_closed == 5
    assert rec.dropped_rows == 2
    # The outage tick claimed the three surviving rows; they are now a protected batch.
    assert queued(rec) == ([T0 + 120, T0 + 180, T0 + 240], [])
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
    assert queued(rec) == ([], [])


class BlockingStorage(FakeStorage):
    """Records each submitted minute batch; the first write blocks until released.

    ``outcome`` of that first write: "ok" commits, "fail" commits nothing and
    raises, "ack_lost" commits and then raises. Later writes succeed at once.
    """

    def __init__(self, outcome="ok"):
        super().__init__()
        self.outcome = outcome
        self.batches = []
        self.started = threading.Event()
        self.release = threading.Event()
        if outcome == "ack_lost":
            self.fail_commit, self.ack_lost = 1, True

    @contextmanager
    def session(self):
        with super().session() as tx:
            upsert = tx.upsert_minutes

            def blocking_upsert(rows):
                self.batches.append([r.ts for r in rows])
                if len(self.batches) == 1:
                    self.started.set()
                    assert self.release.wait(5), "test never released the write"
                    if self.outcome == "fail":
                        raise StorageUnavailable("write failed")
                upsert(rows)

            tx.upsert_minutes = blocking_upsert
            yield tx


def minutes_ts(*offsets):
    return [T0 + 60 * m for m in offsets]


def blocked_flush(capacity, storage):
    """Minutes 0..capacity-1 closed while the DB is down, then a flush of them blocks in upsert()."""
    rec, _ = make(buffer_rows=capacity, storage=storage)
    storage.available = False
    rec.on_connect(T0)
    feed(rec, T0, T0 + 60 * capacity)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 60 * capacity)  # closes the last minute
    assert queued(rec) == ([], minutes_ts(*range(capacity)))
    storage.available = True
    flush = threading.Thread(target=rec.tick, args=(T0 + 60 * capacity,))
    flush.start()
    assert storage.started.wait(5)
    assert storage.batches == [minutes_ts(*range(capacity))]
    return rec, flush


def close_minutes(rec, start_minute, end_minute):
    """Feed live data so that minutes [start_minute, end_minute) close (MQTT side, no DB wait)."""
    feed(rec, T0 + 60 * start_minute + 1, T0 + 60 * end_minute)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 60 * end_minute)


def finish(flush, db):
    db.release.set()
    flush.join(5)
    assert not flush.is_alive()


def test_successful_blocked_flush_counts_nothing_as_dropped():
    db = BlockingStorage("ok")
    rec, flush = blocked_flush(3, db)
    close_minutes(rec, 3, 5)  # minutes 3, 4 close while the write is blocked
    snap = rec.snapshot(T0 + 300)["recorder"]
    assert (snap["protected_rows"], snap["waiting_rows"], snap["flush_in_progress"]) == (3, 2, True)
    assert snap["dropped_rows"] == 0
    finish(flush, db)
    # The same tick then claims and writes the waiting rows, in FIFO order.
    assert db.batches == [minutes_ts(0, 1, 2), minutes_ts(3, 4)]
    assert sorted(db.rows) == minutes_ts(0, 1, 2, 3, 4)
    assert queued(rec) == ([], [])
    assert (rec.dropped_rows, rec.rows_written) == (0, 5)


def test_definite_failed_blocked_flush_keeps_batch_protected():
    db = BlockingStorage("fail")
    rec, flush = blocked_flush(3, db)
    close_minutes(rec, 3, 4)
    finish(flush, db)
    assert db.rows == {}
    assert queued(rec) == (minutes_ts(0, 1, 2), minutes_ts(3))
    assert rec.dropped_rows == 0
    rec.tick(T0 + 241)
    assert db.batches[1:] == [minutes_ts(0, 1, 2), minutes_ts(3)]
    assert sorted(db.rows) == minutes_ts(0, 1, 2, 3) and rec.dropped_rows == 0


def test_ack_lost_during_concurrent_close_never_counts_persisted_rows_as_dropped():
    db = BlockingStorage("ack_lost")
    rec, flush = blocked_flush(3, db)
    close_minutes(rec, 3, 4)  # minute 3 closes while the write is blocked
    finish(flush, db)
    # Storage committed 0, 1, 2 but reported failure.
    assert sorted(db.rows) == minutes_ts(0, 1, 2)
    assert rec.dropped_rows == 0
    assert queued(rec) == (minutes_ts(0, 1, 2), minutes_ts(3))  # protected for retry; 3 waiting
    snap = rec.snapshot(T0 + 241)["recorder"]
    assert (snap["protected_rows"], snap["waiting_rows"], snap["flush_in_progress"]) == (3, 1, False)
    rec.tick(T0 + 241)
    assert db.batches == [minutes_ts(0, 1, 2), minutes_ts(0, 1, 2), minutes_ts(3)]  # idempotent retry, then waiting
    assert sorted(db.rows) == minutes_ts(0, 1, 2, 3)
    assert queued(rec) == ([], [])
    assert (rec.dropped_rows, rec.rows_written) == (0, 4)


def test_ack_lost_with_waiting_overflow_drops_only_never_submitted_rows():
    db = BlockingStorage("ack_lost")
    rec, flush = blocked_flush(2, db)
    close_minutes(rec, 2, 5)  # minutes 2, 3, 4 wait; capacity 2 → never-submitted minute 2 dropped
    assert queued(rec) == (minutes_ts(0, 1), minutes_ts(3, 4))
    assert rec.dropped_rows == 1
    finish(flush, db)
    assert sorted(db.rows) == minutes_ts(0, 1)
    assert queued(rec) == (minutes_ts(0, 1), minutes_ts(3, 4))  # ambiguous batch still protected
    assert rec.dropped_rows == 1
    close_minutes(rec, 5, 6)  # minute 5: the oldest waiting (3, never submitted) is dropped, not 0 or 1
    assert queued(rec) == (minutes_ts(0, 1), minutes_ts(4, 5))
    assert rec.dropped_rows == 2
    rec.tick(T0 + 361)
    assert sorted(db.rows) == minutes_ts(0, 1, 4, 5)
    assert rec.dropped_rows == 2  # exactly minutes 2 and 3, neither ever submitted nor stored
    assert queued(rec) == ([], [])


def test_waiting_overflow_while_flush_blocked_drops_oldest_waiting():
    db = BlockingStorage("ok")
    rec, flush = blocked_flush(2, db)
    close_minutes(rec, 2, 5)  # three waiting rows exceed capacity 2: minute 2 lost on any outcome
    assert queued(rec) == (minutes_ts(0, 1), minutes_ts(3, 4))
    assert rec.dropped_rows == 1
    finish(flush, db)
    assert sorted(db.rows) == minutes_ts(0, 1, 3, 4)
    assert rec.dropped_rows == 1
