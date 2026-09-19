"""The frozen ``/api/v1`` contract: paths, key sets, shapes, ordering, error codes.

Structural assertions only — values belong to the engine tests. These exist so
a Stage 4 frontend can rely on docs/API.md without reading backend code, and so
no accidental field, path or reordering slips in unnoticed.
"""

import pytest

from conftest import T0, Api
from pompa.catalog import METRICS, RECORDED_KEYS

PATHS = {"/health", "/api/v1/status", "/api/v1/live", "/api/v1/metrics", "/api/v1/history"}
RANGE = {"from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:05:00Z"}

LIVE_KEYS = {"now", "mqtt", "metrics"}
LIVE_MQTT_KEYS = {"connected", "alive", "epoch"}
LIVE_ENTRY_KEYS = {"value", "mode", "source_id", "source_topic", "received_at"}
METRIC_ENTRY_KEYS = {"key", "label", "unit", "group", "kind", "history_fields", "energy"}
COP_ENTRY_KEYS = {"key", "label", "unit", "kind", "history_fields"}
STATUS_KEYS = {"now", "mqtt", "recorder", "database", "sources"}
STATUS_MQTT_KEYS = {"connected", "epoch", "connects", "disconnects", "connected_at", "disconnected_at",
                    "lwt", "alive", "alive_since", "last_live_message_at", "stale_after_seconds",
                    "parse_rejects", "uncatalogued_topics"}
STATUS_RECORDER_KEYS = {"process_start", "last_closed_minute", "last_row_minute", "last_written_minute",
                        "rows_closed", "rows_written", "protected_rows", "waiting_rows",
                        "waiting_capacity", "flush_in_progress", "dropped_rows", "schema_ready",
                        "db_last_ok_at", "db_last_error", "db_last_error_at", "retention_1m_days",
                        "rollup", "purge"}
STATUS_DATABASE_KEYS = {"available", "error", "oldest_minute", "newest_minute", "rolled_until", "raw_floor"}
STATUS_SOURCE_KEYS = {"id", "topic", "metric", "seen_live", "historical_value", "epoch_last_live_at",
                      "last_value", "last_outcome", "last_retained", "last_received_at", "first_live_at",
                      "latest_live_at", "live_messages", "retained_messages", "sentinel_messages",
                      "rejected_messages", "gap_count", "gap_sum_seconds", "max_live_gap_seconds",
                      "mean_live_gap_seconds"}
HISTORY_KEYS = {"from", "to", "bucket", "requested_bucket", "buckets", "series"}
BUCKET_KEYS = {"start", "end", "expected_minutes", "recorded_minutes", "coverage_percent"}
SERIES_META = {"label", "unit", "kind"}


@pytest.fixture
def api():
    api = Api()
    api.connect(T0).publish_every(T0, T0 + 300).tick(T0 + 300)
    api.now = float(T0 + 300)
    return api


def history(api, **params):
    return api.body("/api/v1/history", None, **{**RANGE, **params})


# ----------------------------------------------------------------- API surface


def test_exactly_these_paths_exist(api):
    paths = {route.path for route in api.client.app.routes if route.path.startswith(("/health", "/api"))}
    assert paths == PATHS
    assert set(api.body("/openapi.json")["paths"]) == PATHS


def test_every_documented_path_answers_get(api):
    for path in PATHS:
        params = RANGE if path.endswith("/history") else {}
        assert api.get(path, None, **params).status_code == 200, path


# ------------------------------------------------------------------ /health


def test_health_shape(api):
    assert api.body("/health") == {"status": "ok"}


# -------------------------------------------------------------------- /live


def test_live_shape(api):
    body = api.body("/api/v1/live")
    assert set(body) == LIVE_KEYS
    assert set(body["mqtt"]) == LIVE_MQTT_KEYS
    assert list(body["metrics"]) == [m.key for m in METRICS]
    assert all(set(entry) == LIVE_ENTRY_KEYS for entry in body["metrics"].values())
    assert body["now"].endswith("Z")
    assert {entry["mode"] for entry in body["metrics"].values()} <= {"live", "retained", "none"}


def test_live_null_semantics(api):
    entries = api.body("/api/v1/live")["metrics"].values()
    for entry in entries:
        if entry["mode"] == "none":
            assert (entry["value"], entry["source_id"], entry["source_topic"], entry["received_at"]) == (
                None, None, None, None)
        else:
            assert entry["value"] is not None and entry["source_id"] and entry["source_topic"]
            assert entry["received_at"].endswith("Z")


# ----------------------------------------------------------------- /metrics


def test_metrics_shape(api):
    body = api.body("/api/v1/metrics")
    assert set(body) == {"timezone", "history", "metrics", "cop"}
    assert set(body["history"]) == {"buckets", "max_buckets"}
    assert all(set(entry) == METRIC_ENTRY_KEYS for entry in body["metrics"])
    assert all(set(entry) == COP_ENTRY_KEYS for entry in body["cop"])
    assert [entry["key"] for entry in body["cop"]] == ["cop_co", "cop_dhw", "cop_total"]


# ------------------------------------------------------------------ /status


def test_status_shape(api):
    body = api.body("/api/v1/status")
    assert set(body) == STATUS_KEYS
    assert set(body["mqtt"]) == STATUS_MQTT_KEYS
    assert set(body["mqtt"]["lwt"]) == {"state", "retained", "received_at", "messages"}
    assert set(body["recorder"]) == STATUS_RECORDER_KEYS
    assert set(body["recorder"]["rollup"]) == {"last_rolled_hour", "last_rolled_at", "error", "error_at"}
    assert set(body["recorder"]["purge"]) == {"last_run_at", "last_cutoff", "last_deleted_rows",
                                              "deleted_rows", "error", "error_at"}
    assert set(body["database"]) == STATUS_DATABASE_KEYS
    assert all(set(source) == STATUS_SOURCE_KEYS for source in body["sources"])
    assert [source["id"] for source in body["sources"]] == [
        s.id for m in METRICS for s in m.sources]


# ----------------------------------------------------------------- /history


def test_history_shape_and_ordering(api):
    body = history(api, bucket="1m", series="outside_temp,cop_co,main_outlet_temp")
    assert set(body) == HISTORY_KEYS
    assert (body["bucket"], body["requested_bucket"]) == ("1m", "1m")
    assert list(body["series"]) == ["outside_temp", "cop_co", "main_outlet_temp"]
    assert all(set(bucket) == BUCKET_KEYS for bucket in body["buckets"])
    assert [b["start"] for b in body["buckets"]] == sorted(b["start"] for b in body["buckets"])
    assert all(b["end"] > b["start"] for b in body["buckets"])
    for entry in body["series"].values():
        assert all(len(entry[field]) == len(body["buckets"]) for field in entry if field not in SERIES_META)


def test_history_default_series_order_is_the_catalog_order(api):
    body = history(api)
    assert list(body["series"]) == list(RECORDED_KEYS) + ["cop_co", "cop_dhw", "cop_total"]


@pytest.mark.parametrize("series,fields", [
    ("main_outlet_temp", {"avg", "min", "max", "minutes"}),
    ("operating_mode", {"last", "min", "max", "minutes"}),
    ("co_power_consumption", {"avg", "min", "max", "minutes", "kwh"}),
    ("cop_total", {"cop", "paired_minutes", "input_kwh", "output_kwh"}),
])
def test_history_series_field_shapes(api, series, fields):
    entry = history(api, bucket="total", series=series)["series"][series]
    assert set(entry) == SERIES_META | fields


def test_history_bucket_names_and_calendar_dates(api):
    body = api.body("/api/v1/history", None, **{"from": "2027-01-15", "to": "2027-01-16",
                                                "bucket": "1d", "series": "outside_temp"})
    assert (body["from"], body["to"]) == ("2027-01-14T23:00:00Z", "2027-01-15T23:00:00Z")
    assert body["bucket"] == "1d" and len(body["buckets"]) == 1


def test_history_null_and_zero_semantics(api):
    # Starts two minutes before recording: unrecorded minutes are null, not zero.
    body = api.body("/api/v1/history", None, **{"from": "2027-01-15T07:58:00Z",
                                                "to": "2027-01-15T08:05:00Z",
                                                "bucket": "1m", "series": "outside_temp"})
    entry = body["series"]["outside_temp"]
    for i, bucket in enumerate(body["buckets"]):
        if entry["minutes"][i] == 0:
            assert entry["avg"][i] is None and entry["min"][i] is None
        else:
            assert bucket["recorded_minutes"] > 0
    assert entry["minutes"][:2] == [0, 0] and entry["minutes"][2] > 0
    assert history(api, bucket="total", series="dhw_power_production")["series"][
        "dhw_power_production"]["min"] == [0.0]  # a real zero, not null


# -------------------------------------------------------------- error matrix


@pytest.mark.parametrize("path,params,code", [
    ("/api/v1/history", {}, 400),
    ("/api/v1/history", {"from": "2027-01-15T08:00:00Z"}, 400),
    ("/api/v1/history", {**RANGE, "bucket": "3m"}, 400),
    ("/api/v1/history", {**RANGE, "series": "nope"}, 400),
    ("/api/v1/history", {"from": "2027-01-15T08:00:00", "to": "2027-01-15T08:05:00Z"}, 400),
    ("/api/v1/history", {"from": "2027-01-15T08:05:00Z", "to": "2027-01-15T08:00:00Z"}, 400),
    ("/api/v1/history", {"from": "2027-01-13T05:59:00Z", "to": "2027-01-15T08:00:00Z", "bucket": "1m"}, 422),
    ("/api/v1/history", {"from": "1969-12-31T23:00:00Z", "to": "2027-01-15T08:05:00Z"}, 422),
])
def test_error_matrix(api, path, params, code):
    r = api.get(path, None, **params)
    assert r.status_code == code, r.text
    assert set(r.json()) == {"detail"}


def test_history_database_unavailable_is_the_only_503(api):
    api.storage.available = False
    assert api.get("/api/v1/history", None, **RANGE).status_code == 503
    for path in PATHS - {"/api/v1/history"}:
        assert api.get(path).status_code == 200, path


# ------------------------------------------------------- one metadata truth


def test_no_endpoint_carries_a_second_metric_catalog(api):
    """Labels, units and kinds come from one place; drift would show here."""
    catalog = {entry["key"]: entry for entry in api.body("/api/v1/metrics")["metrics"]}
    catalog |= {entry["key"]: entry for entry in api.body("/api/v1/metrics")["cop"]}
    for name, entry in history(api)["series"].items():
        described = catalog[name]
        assert (entry["label"], entry["unit"], entry["kind"]) == (
            described["label"], described["unit"], described["kind"])
        assert set(entry) - SERIES_META == set(described["history_fields"])
    assert set(api.body("/api/v1/live")["metrics"]) <= set(catalog)


def test_openapi_documents_exactly_the_frozen_paths(api):
    spec = api.body("/openapi.json")
    assert set(spec["paths"]) == PATHS
    assert list(spec["paths"]["/api/v1/history"]["get"].get("parameters", [])) != []
    names = {p["name"] for p in spec["paths"]["/api/v1/history"]["get"]["parameters"]}
    assert names == {"from", "to", "bucket", "series"}
