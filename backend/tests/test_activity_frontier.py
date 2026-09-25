"""Stage 4C F2: pending persistence is an open activity tail, not a historical gap."""

import threading
from contextlib import contextmanager

import pytest
from starlette.responses import JSONResponse

from conftest import RUNNING, T0, Api, FakeStorage, persist_canonical, row
from pompa import activity_history, recorder as recorder_module
from pompa.minute import iso_utc
from pompa.storage import StorageUnavailable

M, H = 60, 3600


def z(ts):
    return iso_utc(ts)


def activity(api, start, end, now):
    return api.body("/api/v1/activity", t=now, **{"from": z(start), "to": z(end)})


def run_until(api, end, *, start=T0, step=10):
    t = start
    while t < end:
        api.publish(t, RUNNING)
        api.tick(t)
        t += step


def test_pre_tick_closed_minute_is_open_then_recorded():
    api = Api().connect(T0)
    run_until(api, T0 + 181)
    api.publish(T0 + 239.9, RUNNING)
    before = activity(api, T0, T0 + 5 * M, T0 + 240.5)
    assert before["closed_until"] == z(T0 + 3 * M)
    assert before["summary"]["gap_minutes"] == 0
    assert before["timeline"][-1]["type"] == "open"
    assert before["compressor_runs"][0]["end_boundary"] == "open"
    api.tick(T0 + 241)
    after = activity(api, T0, T0 + 5 * M, T0 + 241)
    assert after["closed_until"] == z(T0 + 4 * M)
    assert (after["summary"]["recorded_minutes"], after["summary"]["gap_minutes"]) == (4, 0)


def test_waiting_row_is_open_before_flush():
    api = Api().connect(T0)
    run_until(api, T0 + 181)
    api.publish(T0 + 239.9, RUNNING)
    api.publish(T0 + 240.2, RUNNING)  # MQTT closes the minute but does not flush it
    assert [r.ts for r in api.recorder._waiting] == [T0 + 3 * M]
    body = activity(api, T0, T0 + 5 * M, T0 + 240.5)
    assert body["closed_until"] == z(T0 + 3 * M)
    assert body["summary"]["gap_minutes"] == 0


def test_protected_row_is_open_until_ack(monkeypatch):
    api = Api().connect(T0)
    run_until(api, T0 + 121)
    api.publish(T0 + 179.9, RUNNING)
    with monkeypatch.context() as patch:
        def fail(_storage, _rows):
            raise StorageUnavailable("write unavailable")
        patch.setattr(recorder_module, "persist", fail)
        api.tick(T0 + 180.2)
        assert [r.ts for r in api.recorder._protected] == [T0 + 2 * M]
        body = activity(api, T0, T0 + 4 * M, T0 + 180.5)
        assert body["closed_until"] == z(T0 + 2 * M)
        assert body["summary"]["gap_minutes"] == 0
    api.tick(T0 + 181)
    assert activity(api, T0, T0 + 4 * M, T0 + 181)["summary"]["recorded_minutes"] == 3


def test_outage_backlog_stays_open_and_recovers(monkeypatch):
    api = Api().connect(T0)
    run_until(api, T0 + 121)
    with monkeypatch.context() as patch:
        def fail(_storage, _rows):
            raise StorageUnavailable("write unavailable")
        patch.setattr(recorder_module, "persist", fail)
        run_until(api, T0 + 7 * M + 1, start=T0 + 130)
        body = activity(api, T0, T0 + 8 * M, T0 + 7 * M + 1)
        assert body["closed_until"] == z(T0 + 2 * M)
        assert (body["summary"]["recorded_minutes"], body["summary"]["gap_minutes"]) == (2, 0)
    run_until(api, T0 + 8 * M + 1, start=T0 + 7 * M + 10)
    body = activity(api, T0, T0 + 8 * M, T0 + 8 * M + 1)
    assert (body["summary"]["recorded_minutes"], body["summary"]["gap_minutes"]) == (8, 0)


def test_overflow_drops_become_real_gaps_after_older_rows_settle(monkeypatch):
    api = Api().connect(T0)
    api.recorder.buffer_rows = 3
    run_until(api, T0 + 61)
    with monkeypatch.context() as patch:
        def fail(_storage, _rows):
            raise StorageUnavailable("write unavailable")
        patch.setattr(recorder_module, "persist", fail)
        run_until(api, T0 + 9 * M + 1, start=T0 + 70)
        assert api.recorder.dropped_rows > 0
        assert activity(api, T0, T0 + 10 * M, T0 + 9 * M + 1)["summary"]["gap_minutes"] == 0
    run_until(api, T0 + 10 * M + 1, start=T0 + 9 * M + 10)
    body = activity(api, T0, T0 + 10 * M, T0 + 10 * M + 1)
    assert body["summary"]["gap_minutes"] == api.recorder.dropped_rows


def test_lost_ack_committed_row_remains_open_until_acknowledged():
    api = Api().connect(T0)
    run_until(api, T0 + 121)
    api.publish(T0 + 179.9, RUNNING)
    api.storage.fail_commit = 1
    api.storage.ack_lost = True
    api.tick(T0 + 180.2)
    assert T0 + 2 * M in api.storage.rows  # commit happened
    assert [r.ts for r in api.recorder._protected] == [T0 + 2 * M]
    body = activity(api, T0, T0 + 4 * M, T0 + 180.5)
    assert body["closed_until"] == z(T0 + 2 * M)
    assert body["summary"]["gap_minutes"] == 0
    api.tick(T0 + 181)
    body = activity(api, T0, T0 + 4 * M, T0 + 181)
    assert body["summary"]["recorded_minutes"] == 3


def test_restart_preserves_old_history_and_exposes_real_hole():
    storage = FakeStorage()
    old = Api(storage=storage).connect(T0)
    run_until(old, T0 + 10 * M + 1)
    start = T0 + 12 * M + 30
    new = Api(storage=storage, start=start).connect(start)
    assert activity(new, T0, T0 + 10 * M, start + 1) == activity_history.query(
        storage, T0, T0 + 10 * M, start + 1)
    body = activity(new, T0, T0 + 20 * M, start + 1)
    assert body["summary"]["gap_minutes"] == 2
    assert body["timeline"][-1]["type"] == "open"
    run_until(new, T0 + 15 * M + 1, start=start)
    body = activity(new, T0, T0 + 20 * M, T0 + 15 * M + 1)
    assert body["summary"]["gap_minutes"] == 3


def test_silent_source_gaps_settle_without_a_write():
    api = Api().connect(T0)
    run_until(api, T0 + 5 * M + 1)
    for t in range(T0 + 5 * M + 1, T0 + 40 * M + 2, 30):
        api.tick(t)
    api.tick(T0 + 40 * M + 1)
    body = activity(api, T0, T0 + 40 * M, T0 + 40 * M + 1)
    assert body["closed_until"] == z(T0 + 40 * M)
    assert body["summary"]["gap_minutes"] == 25


def test_backward_clock_step_clamps_frontier_to_wall_minute():
    api = Api(start=T0 + 10 * M)
    now, settled = api.recorder.settled_before(lambda: T0 + 3 * M + 2)
    assert (now, settled) == (T0 + 3 * M + 2, T0 + 3 * M)
    body = activity(api, T0, T0 + 5 * M, now)
    assert body["closed_until"] == z(settled)


def test_frontier_samples_clock_under_lock_without_storage_io():
    api = Api(start=T0 + M)
    before = api.storage.sessions

    def clock():
        assert api.recorder._lock.locked()
        return T0 + M + 1

    assert api.recorder.settled_before(clock) == (T0 + M + 1, T0 + M)
    assert api.storage.sessions == before


def test_old_history_is_byte_identical_and_response_keys_stable():
    storage = FakeStorage()
    persist_canonical(storage, [row(T0 + i * M, compressor_freq=40.0, heatpump_state=1.0)
                                for i in range(5)])
    api = Api(storage=storage, start=T0 + 2 * H)
    response = api.get("/api/v1/activity", t=T0 + 2 * H + 10,
                       **{"from": z(T0), "to": z(T0 + 10 * M)})
    assert response.status_code == 200
    reference = activity_history.query(storage, T0, T0 + 10 * M, T0 + 2 * H + 10)
    assert response.json() == reference
    assert response.content == JSONResponse(reference).body
    assert api.get("/api/v1/history", t=T0 + 2 * H + 10,
                   **{"from": z(T0), "to": z(T0 + 10 * M), "bucket": "1m"}).status_code == 200


def test_api_reads_frontier_before_query_and_rejects_bad_frontiers(monkeypatch):
    api = Api(start=T0 + M)
    called = []
    real_frontier = api.recorder.settled_before
    real_query = activity_history.query

    def frontier(clock):
        called.append("recorder")
        return real_frontier(clock)

    def query(storage, start, end, now, *, settled_before):
        called.append("query")
        assert called == ["recorder", "query"]
        assert settled_before == T0 + M
        return real_query(storage, start, end, now, settled_before=settled_before)

    monkeypatch.setattr(api.recorder, "settled_before", frontier)
    monkeypatch.setattr(activity_history, "query", query)
    assert activity(api, T0, T0 + 2 * M, T0 + M + 1)["closed_until"] == z(T0 + M)
    for invalid in (True, 12.0, T0 + 1, T0 + 2 * M):
        with pytest.raises(ValueError, match="settled_before"):
            real_query(api.storage, T0, T0 + M, T0 + M + 1, settled_before=invalid)


def test_real_mariadb_commit_before_ack_and_snapshot_order(mariadb, monkeypatch):
    mariadb.ensure_schema()
    api = Api(storage=mariadb).connect(T0)
    run_until(api, T0 + 121)
    api.publish(T0 + 179.9, RUNNING)
    committed = threading.Event()
    release = threading.Event()
    real_write = recorder_module.Recorder._write
    real_session = mariadb.session

    def write_then_wait(self, batch, now):
        result = real_write(self, batch, now)
        if batch:
            committed.set()
            assert release.wait(20)
        return result

    monkeypatch.setattr(recorder_module.Recorder, "_write", write_then_wait)
    worker = threading.Thread(target=api.recorder.tick, args=(T0 + 180.2,))
    worker.start()
    try:
        assert committed.wait(20)
        with mariadb.session() as session:
            assert [ts for ts, _ in session.read_minutes(T0, T0 + H)][-1] == T0 + 2 * M
        observed = []
        real_frontier = api.recorder.settled_before

        def frontier(clock):
            result = real_frontier(clock)
            observed.append("frontier")
            return result

        @contextmanager
        def session():
            observed.append("snapshot")
            assert observed == ["frontier", "snapshot"]
            with real_session() as tx:
                yield tx

        monkeypatch.setattr(api.recorder, "settled_before", frontier)
        monkeypatch.setattr(mariadb, "session", session)
        body = activity(api, T0, T0 + 4 * M, T0 + 180.5)
        assert observed == ["frontier", "snapshot"]
        assert body["closed_until"] == z(T0 + 2 * M)
        assert body["summary"]["gap_minutes"] == 0
    finally:
        monkeypatch.setattr(mariadb, "session", real_session)
        release.set()
        worker.join(20)
    assert not worker.is_alive()
    observed.clear()
    body = activity(api, T0, T0 + 4 * M, T0 + 181)
    assert body["summary"]["recorded_minutes"] == 3
