"""Report snapshot ownership, settled edges and per-hour extraction consistency."""

import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import Api, FakeStorage, persist_canonical, row
from pompa import activity_history, history, report, report_read
from pompa.activity import build_segments, timeline
from pompa.api import create_app
from pompa.timegrid import Unrepresentable
from test_activity_api import put
from test_activity_durable import CO, DHW, OFF, purge_all

M, H = 60, 3600


def test_frontier_lock_snapshot_and_composition_order(monkeypatch):
    period = report.resolve_period("day", date="2027-01-15")
    api = Api(start=period.end)
    put(api.storage, period.start + H, [CO, OFF], roll=False)
    order, sessions, clocks = [], [], []
    inside = False
    original_resolve = report.resolve_period
    original_frontier = api.recorder.settled_before
    original_session = api.storage.session
    original_history = history.canonical_partials
    original_activity = activity_history.load_timeline
    original_check = report_read._check_recorded
    original_compose = report.compose_report

    def resolve(*args, **kwargs):
        order.append("resolve")
        return original_resolve(*args, **kwargs)

    def clock():
        assert api.recorder._lock.locked()
        clocks.append(period.end)
        return period.end

    def frontier(clock):
        result = original_frontier(clock)
        assert not api.recorder._lock.locked()
        order.append("settled_before returned")
        return result

    @contextmanager
    def session():
        nonlocal inside
        assert order[-1] == "settled_before returned"
        assert not api.recorder._lock.locked()
        order.append("session open")
        with original_session() as supplied:
            inside = True
            sessions.append(supplied)
            yield supplied
        inside = False
        order.append("session closed")

    def canonical(supplied, *args, **kwargs):
        assert inside and supplied is sessions[0] and not api.recorder._lock.locked()
        order.append("history")
        return original_history(supplied, *args, **kwargs)

    def activity(supplied, *args, **kwargs):
        assert inside and supplied is sessions[0] and not api.recorder._lock.locked()
        order.append("activity")
        assert kwargs == {"left_floor": None, "raw_edge": True}
        return original_activity(supplied, *args, **kwargs)

    def check(*args):
        assert inside and not api.recorder._lock.locked()
        order.append("consistency")
        return original_check(*args)

    def compose(source):
        assert not inside and not api.recorder._lock.locked()
        order.append("compose")
        return original_compose(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("report called a public query wrapper")

    monkeypatch.setattr(report, "resolve_period", resolve)
    monkeypatch.setattr(api.recorder, "settled_before", frontier)
    monkeypatch.setattr(api.storage, "session", session)
    monkeypatch.setattr(history, "canonical_partials", canonical)
    monkeypatch.setattr(activity_history, "load_timeline", activity)
    monkeypatch.setattr(report_read, "_check_recorded", check)
    monkeypatch.setattr(report, "compose_report", compose)
    monkeypatch.setattr(history, "query", forbidden)
    monkeypatch.setattr(activity_history, "query", forbidden)
    response = TestClient(create_app(api.recorder, api.storage, clock=clock)).get(
        "/api/v1/report", params={"period": "day", "date": "2027-01-15"})
    assert response.status_code == 200, response.text
    assert len(sessions) == len(clocks) == 1
    assert order == ["resolve", "settled_before returned", "session open", "history", "activity",
                     "consistency", "session closed", "compose"]


@pytest.mark.parametrize("aligned", [False, True])
@pytest.mark.parametrize("gappy", [False, True])
def test_durable_earlier_hour_and_current_raw_edge(any_storage, aligned, gappy):
    period = report.resolve_period("day", date="2027-01-15")
    edge = period.start + H
    put(any_storage, period.start + 10 * M, [OFF])
    put(any_storage, edge, [CO, None if gappy else CO, CO, CO])
    # Earlier raw now disagrees with durable facts; the complete hour must still use durable.
    with any_storage.session() as session:
        session.upsert_minutes([row(period.start + 10 * M, **DHW)])
    closed = edge + (H if aligned else 3 * M)
    body = report_read.query(any_storage, period, closed + 17, closed)
    cov = body["buckets"][1]["coverage"]
    expected = (4 if aligned else 3) - int(gappy)
    assert cov["recorded_minutes"] == expected
    assert cov["gap_minutes"] == (60 if aligned else 3) - expected
    assert body["totals"]["activity"]["classes"]["off"]["minutes"] == 1
    assert body["totals"]["activity"]["classes"]["dhw"]["minutes"] == 0
    assert body["totals"]["activity"]["classes"]["co"]["minutes"] == expected
    assert body["buckets"][1]["energy"]["cop"]["total"]["paired_minutes"] == expected


def test_hour_aligned_frontier_can_use_purged_durable_hour(any_storage):
    period = report.resolve_period("day", date="2027-01-15")
    put(any_storage, period.start + H, [CO] * 4)
    put(any_storage, period.start + 5 * H, [OFF])
    assert purge_all(any_storage) == 4
    body = report_read.query(any_storage, period, period.start + 2 * H, period.start + 2 * H)
    assert body["totals"]["coverage"]["recorded_minutes"] == 4


def test_required_purged_raw_edge_is_422_and_never_durable_fallback(any_storage):
    period = report.resolve_period("day", date="2027-01-15")
    edge = period.start + H
    put(any_storage, edge, [CO] * 4)
    put(any_storage, edge + 4 * H, [OFF])
    assert purge_all(any_storage) == 4
    closed = edge + 3 * M
    with any_storage.session() as session:
        with pytest.raises(Unrepresentable, match="raw.*purged"):
            activity_history.load_timeline(session, edge, closed, closed, raw_edge=True)
    response = Api(storage=any_storage, start=closed).get(
        "/api/v1/report", period="day", date="2027-01-15")
    assert response.status_code == 422
    assert "purged" in response.json()["detail"]


def test_current_edge_rows_only_above_frontier_do_not_become_unavailable(any_storage):
    period = report.resolve_period("day", date="2027-01-15")
    edge = period.start + H
    put(any_storage, edge + 4 * M, [CO])
    body = report_read.query(any_storage, period, edge + 5 * M, edge + 3 * M)
    cov = body["buckets"][1]["coverage"]
    assert cov["recorded_minutes"] == 0
    assert cov["settled_minutes"] == cov["gap_minutes"] == 3
    assert body["totals"]["energy"]["consumption"]["observed_kwh"] is None


@pytest.mark.parametrize("crossing", [False, True])
def test_purged_current_edge_only_outside_period_is_widening_evidence(any_storage, crossing):
    period = report.resolve_period("day", date="2027-01-15")
    put(any_storage, period.end - M if crossing else period.start + H, [CO])
    put(any_storage, period.end, [CO] * 4)
    put(any_storage, period.end + 4 * H, [OFF])
    assert purge_all(any_storage) == 5
    body = report_read.query(any_storage, period, period.end + 5 * M, period.end + 3 * M)
    assert body["totals"]["coverage"]["recorded_minutes"] == 1
    assert body["totals"]["activity"]["classes"]["co"]["minutes"] == 1


def test_equal_total_but_different_per_hour_counts_fail_500(monkeypatch):
    period = report.resolve_period("week", date="2027-01-15")  # both wrong hours share one day bucket
    storage = FakeStorage()
    first, second = period.start + H, period.start + 2 * H
    put(storage, first, [CO, CO], roll=False)
    put(storage, second, [CO], roll=False)
    wrong_rows = [(r.ts, r.values) for r in [row(first, **CO), row(second, **CO), row(second + M, **CO)]]
    wrong = timeline(build_segments(wrong_rows), period.start - H, period.end, period.end)
    assert sum(segment.minutes for segment in wrong.segments) == 3 == len(storage.rows)
    monkeypatch.setattr(activity_history, "load_timeline", lambda *args, **kwargs:
                        activity_history.LoadedTimeline(wrong, wrong.start, wrong.end))
    response = Api(storage=storage, start=period.end).get(
        "/api/v1/report", period="week", date="2027-01-15")
    assert response.status_code == 500
    assert "history=2, activity=1" in response.json()["detail"]


def test_report_has_bounded_range_queries_not_per_bucket_queries(monkeypatch):
    period = report.resolve_period("custom", from_date="2026-10-01", to_date="2026-11-01")
    storage = FakeStorage()
    original = storage.session
    calls = {"read_minutes": [], "read_rollup": [], "read_activity_segments": []}
    @contextmanager
    def session():
        with original() as supplied:
            for name in calls:
                method = getattr(supplied, name)
                def read(*args, _name=name, _method=method, **kwargs):
                    calls[_name].append(args)
                    return _method(*args, **kwargs)
                monkeypatch.setattr(supplied, name, read)
            yield supplied
    monkeypatch.setattr(storage, "session", session)
    body = report_read.query(storage, period, period.end, period.end)
    assert len(body["buckets"]) == 31
    assert storage.sessions == 1
    assert {name: len(values) for name, values in calls.items()} == {
        "read_minutes": 2, "read_rollup": 2, "read_activity_segments": 1}
    assert all(tuple(args[:2]) == (period.start, period.end) for args in calls["read_minutes"][:1])


def test_mariadb_report_keeps_one_generation_across_concurrent_commit(mariadb, monkeypatch):
    mariadb.ensure_schema()
    period = report.resolve_period("day", date="2027-01-15")
    ts = period.start + H + 10 * M
    put(mariadb, ts, [CO])
    history_read, writer_committed = threading.Event(), threading.Event()
    failures, reader_sessions, connections = [], [], {}
    original_session = mariadb.session
    original_history = history.canonical_partials
    original_activity = activity_history.load_timeline

    @contextmanager
    def session():
        with original_session() as supplied:
            supplied._cur.execute("SELECT CONNECTION_ID(), @@tx_isolation")
            connection, isolation = supplied._cur.fetchone()
            connections.setdefault(threading.current_thread().name, []).append(connection)
            assert isolation == "REPEATABLE-READ"
            if threading.current_thread().name != "report-writer":
                reader_sessions.append(supplied)
            yield supplied

    def canonical(supplied, *args, **kwargs):
        loaded = original_history(supplied, *args, **kwargs)
        assert loaded.fold([(period.start + H, period.start + 2 * H)])[0][0]["recorded"].n == 1
        history_read.set()
        assert writer_committed.wait(10), "writer did not commit between history and activity"
        assert not failures
        return loaded

    def activity(supplied, *args, **kwargs):
        assert writer_committed.is_set()
        assert supplied is reader_sessions[0]
        return original_activity(supplied, *args, **kwargs)

    def write():
        try:
            assert history_read.wait(10), "report did not finish history extraction"
            persist_canonical(mariadb, [row(ts + M, **CO)])  # atomic rollup + durable rebuild
        except BaseException as exc:
            failures.append(exc)
        finally:
            writer_committed.set()

    with monkeypatch.context() as patch:
        patch.setattr(mariadb, "session", session)
        patch.setattr(history, "canonical_partials", canonical)
        patch.setattr(activity_history, "load_timeline", activity)
        writer = threading.Thread(target=write, name="report-writer")
        writer.start()
        try:
            old = report_read.query(mariadb, period, period.end, period.end)
        finally:
            writer.join(12)
        assert not writer.is_alive() and not failures
        assert len(reader_sessions) == 1
        assert set(connections["report-writer"]).isdisjoint(
            connections[threading.current_thread().name])
    fresh = report_read.query(mariadb, period, period.end, period.end)
    assert old["totals"]["coverage"]["recorded_minutes"] == 1
    assert old["totals"]["activity"]["classes"]["co"]["minutes"] == 1
    assert fresh["totals"]["coverage"]["recorded_minutes"] == 2
    assert fresh["totals"]["activity"]["classes"]["co"]["minutes"] == 2
