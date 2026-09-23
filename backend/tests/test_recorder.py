"""Recorder: event coordination, bounded write buffer, outage and recovery."""

import threading
from contextlib import contextmanager

from conftest import RUNNING, T0, FakeStorage
from conftest import row as conf_row
from pompa.ingest import Ingest
from pompa.minute import MINUTE, MinuteAccumulator, floor_minute
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
    snap = rec.snapshot(lambda: T0 + 180)[1]["recorder"]
    # Minute 0 was claimed by the first (failed) flush; 1 and 2 never submitted.
    assert (snap["protected_rows"], snap["waiting_rows"]) == (1, 2)
    assert snap["db_last_error"] == "fake outage"
    db.available = True
    rec.tick(T0 + 181)
    assert sorted(db.rows) == [T0, T0 + 60, T0 + 120]
    assert rec.schema_ready and rec.db_last_error is None
    snap = rec.snapshot(lambda: T0 + 181)[1]["recorder"]
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
    assert rec.snapshot(lambda: T0 + 301)[1]["recorder"]["dropped_rows"] == 2


def test_event_before_cursor_clamps_sequencing_but_not_freshness():
    """The accumulator's cursor clamps for safe sequencing; a source's freshness timestamp must not.

    Clamping ``ingest.last_live_at`` to the cursor as well would inflate how long this evidence
    counts as fresh by exactly the gap between its raw receipt and the cursor — the P2
    clock-discontinuity bug. The cursor still floors accumulation; freshness uses the raw receipt.
    """
    rec, _ = make()
    rec.on_connect(T0)
    rec.tick(T0 + 100)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 50)
    assert rec.accumulator.cursor == T0 + 100  # sequencing floor: never regresses
    assert rec.ingest.last_live_at == T0 + 50  # freshness: the true, raw receipt time


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
    snap = rec.snapshot(lambda: T0 + 900)[1]
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
    snap = rec.snapshot(lambda: T0 + 200)[1]
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
    snap = rec.snapshot(lambda: T0 + 300)[1]["recorder"]
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
    snap = rec.snapshot(lambda: T0 + 241)[1]["recorder"]
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


# ------------------------------------------------------------ definite refusal (unwritable rows)


H = 3600


def purged_rolled_hour(db, hour_ts):
    """A rolled hour whose raw evidence is gone: exactly what purge leaves behind."""
    from pompa.recorder import persist, roll_next_hour

    from conftest import minutes
    persist(db, minutes(hour_ts, 60))
    while roll_next_hour(db, 2**32 - 1) is not None:
        pass
    with db.session() as s:
        s.delete_minutes_before(hour_ts + H)


def test_refused_row_is_dropped_instead_of_blocking_the_queue():
    """A row that can never be written must not hold the protected batch or the minutes behind it."""
    db = FakeStorage()
    purged_rolled_hour(db, T0)
    rec, _ = make(start=T0 + 600, storage=db)
    rec.schema_ready = True
    rec._protected = [conf_row(T0 + 600, outside_temp=1.0)]

    rec.tick(T0 + 660)
    snap = rec.snapshot(lambda: T0 + 660)[1]["recorder"]
    assert (snap["protected_rows"], snap["waiting_rows"]) == (0, 0)  # nothing stuck
    assert (snap["refused_rows"], snap["rows_written"], snap["dropped_rows"]) == (1, 0, 0)
    assert snap["last_refusal"] == {"at": "2027-01-15T08:11:00Z", "hours": ["2027-01-15T08:00:00Z"],
                                    "rows": 1, "reason": snap["last_refusal"]["reason"]}
    assert "already purged" in snap["last_refusal"]["reason"]
    assert snap["db_last_error"] is None  # the database was fine; the refusal was ours
    with db.session() as s:
        assert s.read_minutes(T0, T0 + H) == []  # nothing rebuilt the purged hour

    # The recorder keeps working: a later minute in a writable hour is persisted normally.
    rec._waiting.append(conf_row(T0 + H, outside_temp=2.0))
    rec.tick(T0 + H + 120)
    assert sorted(db.rows) == [T0 + H]
    snap = rec.snapshot(lambda: T0 + H + 120)[1]["recorder"]
    assert (snap["refused_rows"], snap["rows_written"], snap["protected_rows"]) == (1, 1, 0)


def test_a_clock_stepped_back_into_a_purged_hour_does_not_wedge_the_recorder():
    """The end-to-end path: restart under an old wall clock, real minutes, no permanent block."""
    db = FakeStorage()
    purged_rolled_hour(db, T0)
    rec, _ = make(start=T0 + 600, storage=db)
    rec.on_connect(T0 + 600)
    feed(rec, T0 + 600, T0 + 780)
    rec.tick(T0 + 780)
    assert queued(rec) == ([], [])  # the protected batch is not wedged
    assert rec.refused_rows == 3 and rec.rows_written == 0 and rec.dropped_rows == 0
    with db.session() as s:
        assert s.read_minutes(T0, T0 + H) == []

    # The clock is corrected; minutes of a writable hour flow again.
    feed(rec, T0 + H, T0 + H + 180)
    rec.tick(T0 + H + 180)
    assert sorted(db.rows) == [T0 + H, T0 + H + 60, T0 + H + 120]
    assert rec.rows_written == 3 and rec.dropped_rows == 0
    assert all(ts >= T0 + H for ts in db.rows)  # the purged hour stayed empty


def test_refusal_keeps_the_writable_rows_of_a_mixed_batch():
    db = FakeStorage()
    purged_rolled_hour(db, T0)
    rec, _ = make(start=T0 + 600, storage=db)
    rec._protected = [conf_row(T0 + 600, outside_temp=1.0), conf_row(T0 + H, outside_temp=2.0)]
    rec.schema_ready = True

    rec.tick(T0 + H + 120)
    assert sorted(db.rows) == [T0 + H]  # only the writable row landed
    snap = rec.snapshot(lambda: T0 + H + 120)[1]["recorder"]
    assert (snap["refused_rows"], snap["rows_written"]) == (1, 1)
    assert (snap["protected_rows"], snap["waiting_rows"], snap["dropped_rows"]) == (0, 0, 0)


def test_refusal_does_not_stop_rollup_and_purge():
    """Maintenance must not be starved by a batch that can never be written."""
    db = FakeStorage()
    purged_rolled_hour(db, T0)
    rec, _ = make(start=T0 + 600, storage=db)
    rec.retention_days = 365
    rec._protected = [conf_row(T0 + 600, outside_temp=1.0)]
    rec.schema_ready = True

    rec.tick(T0 + 400 * 86400)
    assert rec.refused_rows == 1
    assert rec.last_purge_at is not None  # the flush completed, so maintenance ran


def test_ambiguous_outcome_is_never_treated_as_a_refusal():
    """Regression: an outage must still protect and retry the batch unchanged."""
    rec, db = make()
    db.available = False
    rec.on_connect(T0)
    feed(rec, T0, T0 + 120)
    rec.tick(T0 + 120)
    assert (rec.refused_rows, rec.last_refusal) == (0, None)
    assert queued(rec)[0] == [T0, T0 + 60]  # protected, unchanged
    db.available = True
    rec.tick(T0 + 121)
    assert sorted(db.rows) == [T0, T0 + 60]
    assert (rec.refused_rows, rec.dropped_rows, rec.rows_written) == (0, 0, 2)


# ------------------------------------------------------------------ Recorder.safe_future_minute
#
# Stage 4B checkpoint B (docs/ARCHITECTURE.md §25.2.1): the read-only, database-I/O-free fact a
# policy PUT takes and releases before any transaction. Direct regression tests for the reasoning
# behind it, not just an inline exercise of unrelated internals.


def test_safe_future_minute_ordinary_clock_returns_next_whole_minute():
    rec, _ = make(start=T0)
    now = T0 + 30  # within the still-open first minute; nothing has advanced yet
    assert rec.safe_future_minute(now) == floor_minute(now) + MINUTE == T0 + 60


def test_safe_future_minute_follows_the_accumulator_when_it_is_ahead_of_raw_wall_clock():
    """The accumulator's open minute can be ahead of a caller's ``now`` (a queued backlog, or the
    post-clock-step gap of ARCHITECTURE.md §24): the boundary must follow it, not the raw clock."""
    rec, _ = make(start=T0)
    rec.on_connect(T0)
    feed(rec, T0, T0 + 181)  # advances the accumulator's open minute well past T0
    assert rec.accumulator.minute_start > T0  # sanity: the accumulator really did move ahead
    stale_now = T0 + 30  # a "raw now" that is behind where the accumulator already is
    got = rec.safe_future_minute(stale_now)
    assert got == rec.accumulator.minute_start + MINUTE  # follows the accumulator, not raw now
    assert got > floor_minute(stale_now) + MINUTE


def test_safe_future_minute_is_strictly_after_every_waiting_row():
    rec, _ = make(start=T0)
    rec.on_connect(T0)
    feed(rec, T0, T0 + 181)  # closes 3 minutes into _waiting; no tick, so nothing is flushed yet
    waiting_ts = queued(rec)[1]
    assert waiting_ts == [T0, T0 + 60, T0 + 120]
    boundary = rec.safe_future_minute(T0 + 181 + 5)
    assert all(ts < boundary for ts in waiting_ts)


def test_safe_future_minute_is_strictly_after_every_protected_row():
    rec, db = make(start=T0)
    db.available = False
    rec.on_connect(T0)
    feed(rec, T0, T0 + 181)
    rec.tick(T0 + 181)  # flush fails (ambiguous outage): rows move into _protected, retained
    protected_ts, waiting_ts = queued(rec)
    assert protected_ts == [T0, T0 + 60, T0 + 120] and waiting_ts == []
    boundary = rec.safe_future_minute(T0 + 181 + 5)
    assert all(ts < boundary for ts in protected_ts)
