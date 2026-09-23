"""Subsystem independence and the factual status contract.

An unavailable database or an absent MQTT connection is reported where it is a
fact and nowhere else. No endpoint turns one subsystem's failure into process
failure, and none of them invents a health verdict.
"""

import pytest

from conftest import RUNNING, T0, Api, minutes
from conftest import persist_canonical, recorded
from conftest import row as make_row
from pompa.recorder import roll_next_hour
from pompa.timegrid import HOUR

H0 = T0 - T0 % HOUR  # 2027-01-15T08:00:00Z is already hour-aligned
RANGE = {"from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:10:00Z", "series": "outside_temp"}
VERDICT_WORDS = ("healthy", "degraded", "ready", "partial", "usable", "quality", "score", "good", "stale_score")


@pytest.fixture
def api():
    """Recorded and persisted minutes, then a fresh live state at T0 + 10 min."""
    api = Api()
    api.connect(T0).publish_every(T0, T0 + 600).tick(T0 + 600)
    api.now = float(T0 + 600)
    return api


def test_recorded_and_persisted_before_the_matrix(api):
    assert api.recorder.rows_written == 10 and len(api.storage.rows) == 10
    assert api.body("/api/v1/live", T0 + 600)["metrics"]["outside_temp"]["mode"] == "live"


# ----------------------------------------------------------- MariaDB is down


def test_database_down_matrix(api):
    api.storage.available = False
    now = T0 + 601
    assert api.get("/health", now).json() == {"status": "ok"}
    assert api.get("/health", now).status_code == 200
    assert api.get("/api/v1/live", now).status_code == 200
    assert api.get("/api/v1/metrics", now).status_code == 200
    status = api.get("/api/v1/status", now)
    assert status.status_code == 200
    assert status.json()["database"] == {"available": False, "error": "fake outage", "oldest_minute": None,
                                         "newest_minute": None, "rolled_until": None, "purge_cutoff": None}
    assert api.get("/api/v1/history", now, **RANGE).status_code == 503


def test_database_down_leaves_live_state_and_catalog_complete(api):
    before = api.body("/api/v1/live", T0 + 601)
    catalog = api.body("/api/v1/metrics", T0 + 601)
    api.storage.available = False
    assert api.body("/api/v1/live", T0 + 601) == before
    assert api.body("/api/v1/metrics", T0 + 601) == catalog


def test_status_still_reports_mqtt_and_recorder_facts_while_the_database_is_down(api):
    api.storage.available = False
    body = api.body("/api/v1/status", T0 + 601)
    assert set(body) == {"now", "mqtt", "recorder", "database", "sources"}
    assert body["mqtt"]["connected"] is True and body["mqtt"]["alive"] is True
    assert body["recorder"]["rows_written"] == 10
    assert len(body["sources"]) == 25


# ------------------------------------------------------- MQTT is disconnected


def test_mqtt_disconnected_matrix(api):
    api.disconnect(T0 + 601)
    now = T0 + 602
    assert api.get("/health", now).status_code == 200
    assert api.get("/api/v1/metrics", now).status_code == 200

    live = api.body("/api/v1/live", now)
    assert live["mqtt"] == {"connected": False, "alive": False, "epoch": 1}
    assert all(e["mode"] == "none" for e in live["metrics"].values())

    status = api.body("/api/v1/status", now)
    assert status["mqtt"]["connected"] is False and status["mqtt"]["alive"] is False
    assert status["database"]["available"] is True

    history = api.body("/api/v1/history", now, **RANGE)
    assert sum(b["recorded_minutes"] for b in history["buckets"]) == 10


def test_mqtt_disconnected_exposes_retained_values_only_where_factual(api):
    api.disconnect(T0 + 601).connect(T0 + 602)
    api.msg(T0 + 603, "extra/Heat_Power_Consumption_Extra", "900", retained=True)
    live = api.body("/api/v1/live", T0 + 604)
    modes = {key: e["mode"] for key, e in live["metrics"].items()}
    assert modes.pop("co_power_consumption") == "retained"
    assert set(modes.values()) == {"none"}
    assert live["mqtt"]["alive"] is False
    # A retained value is never persisted, so history keeps exactly the ten recorded minutes.
    api.tick(T0 + 1200)
    assert len(api.storage.rows) == 10


def test_mqtt_never_connected_still_serves_everything_but_live_values():
    api = Api()
    api.storage.rows = dict(api.storage.rows)
    for path in ("/health", "/api/v1/live", "/api/v1/metrics", "/api/v1/status"):
        assert api.get(path, T0 + 60).status_code == 200
    assert api.body("/api/v1/status", T0 + 60)["mqtt"]["epoch"] == 0
    assert api.get("/api/v1/history", T0 + 60, **RANGE).status_code == 200


# --------------------------------------------------------------- status facts


def test_status_and_live_agree_about_mqtt(api):
    now = T0 + 601
    live = api.body("/api/v1/live", now)["mqtt"]
    status = api.body("/api/v1/status", now)["mqtt"]
    assert live == {k: status[k] for k in live}


def test_status_sources_stay_the_diagnostic_view_of_the_same_selection(api):
    now = T0 + 601
    live = api.body("/api/v1/live", now)["metrics"]["co_power_consumption"]
    source = next(s for s in api.body("/api/v1/status", now)["sources"] if s["id"] == live["source_id"])
    assert (source["topic"], source["historical_value"]) == (live["source_topic"], live["value"])
    assert source["seen_live"] is True and source["last_retained"] is False


def test_status_reports_a_rollup_failure_as_a_fact():
    api = Api()
    persist_canonical(api.storage, minutes(H0, 120))  # two stored hours, nothing rolled yet
    api.storage.fail_commit = 1
    api.tick(H0 + 3 * HOUR)
    rollup = api.body("/api/v1/status", H0 + 3 * HOUR)["recorder"]["rollup"]
    assert rollup["error"] == "commit failed" and rollup["error_at"] is not None
    assert rollup["last_rolled_hour"] is None
    assert api.body("/api/v1/status", H0 + 3 * HOUR)["database"]["rolled_until"] is None
    api.tick(H0 + 3 * HOUR + 1)  # retried on a later tick, nothing lost
    rollup = api.body("/api/v1/status", H0 + 3 * HOUR + 1)["recorder"]["rollup"]
    assert rollup["error"] is None and rollup["last_rolled_hour"] == "2027-01-15T09:00:00Z"


def test_status_reports_a_purge_refusal_as_a_fact():
    api = Api()
    persist_canonical(api.storage, minutes(H0, 10 * 60))
    while roll_next_hour(api.storage, H0 + 10 * HOUR) is not None:
        pass
    with api.storage.session() as s:  # a rollup that does not account for its minutes
        s.replace_rollup_hour(H0, [("recorded", 1, 1.0, 1.0, 1.0, 1.0)])
    stored = len(api.storage.rows)
    api.tick(H0 + 400 * 86400)
    purge = api.body("/api/v1/status", H0 + 400 * 86400)["recorder"]["purge"]
    assert "accounts for 1 of" in purge["error"] and purge["last_deleted_rows"] == 0
    assert len(api.storage.rows) == stored


def test_status_reports_a_rebuild_refusal_as_a_fact_through_http():
    """A minute landing in a purged rolled hour must surface as refused_rows/last_refusal, via HTTP."""
    api = Api()
    persist_canonical(api.storage, minutes(H0, 60))  # one rolled hour
    while roll_next_hour(api.storage, H0 + HOUR) is not None:
        pass
    with api.storage.session() as s:  # the hour's raw evidence is now physically gone
        s.delete_minutes_before(H0 + HOUR)

    api.recorder.schema_ready = True
    api.recorder._protected = [recorded(make_row(H0, outside_temp=1.0))]
    api.tick(H0 + HOUR + 60)

    recorder = api.body("/api/v1/status", H0 + HOUR + 60)["recorder"]
    assert recorder["refused_rows"] == 1
    assert recorder["last_refusal"] is not None
    assert recorder["last_refusal"]["hours"] == ["2027-01-15T08:00:00Z"]


def test_status_reports_a_pending_protected_batch_and_waiting_rows():
    api = Api(start=T0)
    api.connect(T0).publish_every(T0, T0 + 600)
    api.storage.fail_commit = 1
    api.tick(T0 + 600)  # the batch is submitted, the write is not confirmed
    recorder = api.body("/api/v1/status", T0 + 600)["recorder"]
    assert recorder["protected_rows"] == 10 and recorder["waiting_rows"] == 0
    assert recorder["db_last_error"] == "commit failed" and recorder["dropped_rows"] == 0
    api.publish_every(T0 + 600, T0 + 900)  # minutes 10..13 close; minute 14 is still open
    recorder = api.body("/api/v1/status", T0 + 900)["recorder"]
    assert recorder["protected_rows"] == 10 and recorder["waiting_rows"] == 4


def test_status_reports_waiting_overflow_drops():
    api = Api(start=T0)
    api.recorder.buffer_rows = 3
    api.storage.available = False
    api.connect(T0).publish_every(T0, T0 + 600)
    recorder = api.body("/api/v1/status", T0 + 600)["recorder"]
    assert recorder["waiting_rows"] == 3 and recorder["waiting_capacity"] == 3
    assert recorder["dropped_rows"] == 6 and recorder["rows_closed"] == 9


@pytest.mark.parametrize("path", ["/health", "/api/v1/live", "/api/v1/metrics", "/api/v1/status"])
def test_no_endpoint_invents_a_health_verdict(api, path):
    text = api.get(path, T0 + 601).text
    for word in VERDICT_WORDS:
        assert f'"{word}"' not in text


def test_health_is_process_liveness_only(api):
    api.storage.available = False
    api.disconnect(T0 + 601)
    r = api.get("/health", T0 + 602)
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_live_and_metrics_stay_in_step_with_a_running_recorder(api):
    """Repeated reads while the recorder writes: no endpoint depends on another's state."""
    for i in range(5):
        t = T0 + 600 + i * 60
        api.publish(t)
        api.tick(t)
        live = api.body("/api/v1/live", t)
        catalog = api.body("/api/v1/metrics", t)
        assert list(live["metrics"]) == [m["key"] for m in catalog["metrics"]]
        assert api.body("/api/v1/status", t)["recorder"]["rows_written"] == len(api.storage.rows)


def test_history_range_is_unaffected_by_live_state(api):
    before = api.body("/api/v1/history", T0 + 601, **RANGE)
    api.publish(T0 + 601, RUNNING)  # the open minute is not history
    assert api.body("/api/v1/history", T0 + 601, **RANGE) == before
