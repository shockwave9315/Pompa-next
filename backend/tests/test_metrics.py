"""``GET /api/v1/metrics`` and its agreement with ``/live`` and ``/history``.

The point of these tests is drift: the catalog, the live endpoint, the metric
endpoint and the history response must describe the same metrics with the same
labels, units, kinds, groups and fields, because there is one source of truth.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import T0, FakeStorage, row
from pompa.api import create_app
from pompa.catalog import METRICS, METRICS_BY_KEY, RECORDED_KEYS
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder
from pompa.timegrid import BUCKETS, LOCAL_TZ_NAME, MAX_BUCKETS

METRIC_KEYS = {"key", "label", "unit", "group", "kind", "history_fields", "energy"}
COP_KEYS = {"key", "label", "unit", "kind", "history_fields"}
COP_FIELDS = ["cop", "paired_minutes", "input_kwh", "output_kwh"]


@pytest.fixture
def storage():
    db = FakeStorage()
    db.rows = {
        T0: dict(row(T0, main_outlet_temp=35.0, co_power_consumption=900.0,
                     co_power_production=3600.0, dhw_power_consumption=0.0,
                     dhw_power_production=0.0, operating_mode=4.0).values),
    }
    return db


@pytest.fixture
def client(storage):
    ingest = Ingest(600)
    recorder = Recorder(ingest, MinuteAccumulator(ingest, T0), storage, 60)
    return TestClient(create_app(recorder, storage, clock=lambda: T0 + 120))


@pytest.fixture
def body(client):
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    return r.json()


def entries(body):
    return {m["key"]: m for m in body["metrics"]}


# ------------------------------------------------------------------- contract


def test_top_level_shape(body):
    assert set(body) == {"timezone", "history", "metrics", "cop"}
    assert body["timezone"] == LOCAL_TZ_NAME == "Europe/Warsaw"
    assert body["history"] == {"buckets": list(BUCKETS), "max_buckets": MAX_BUCKETS}
    assert body["history"]["buckets"] == ["auto", "1m", "5m", "1h", "1d", "total"]


def test_entry_shapes_and_unique_deterministic_keys(body, client):
    assert all(set(m) == METRIC_KEYS for m in body["metrics"])
    assert all(set(c) == COP_KEYS for c in body["cop"])
    keys = [m["key"] for m in body["metrics"]] + [c["key"] for c in body["cop"]]
    assert len(keys) == len(set(keys))
    assert [m["key"] for m in body["metrics"]] == [m.key for m in METRICS]
    assert [c["key"] for c in body["cop"]] == ["cop_co", "cop_dhw", "cop_total"]
    assert client.get("/api/v1/metrics").json() == body  # deterministic order


def test_metadata_comes_from_the_canonical_catalog(body):
    for entry in body["metrics"]:
        metric = METRICS_BY_KEY[entry["key"]]
        assert (entry["label"], entry["unit"], entry["group"], entry["kind"]) == (
            metric.label, metric.unit, metric.group, metric.kind)
    outlet = entries(body)["main_outlet_temp"]
    assert outlet["label"] == "Temperatura zasilania" and outlet["unit"] == "°C"
    assert outlet["group"] == "temperature" and outlet["kind"] == "mean"


@pytest.mark.parametrize("key,fields,energy", [
    ("main_outlet_temp", ["avg", "min", "max", "minutes"], False),
    ("operating_mode", ["last", "min", "max", "minutes"], False),
    ("co_power_consumption", ["avg", "min", "max", "minutes", "kwh"], True),
])
def test_history_fields_per_kind(body, key, fields, energy):
    entry = entries(body)[key]
    assert entry["history_fields"] == fields and entry["energy"] is energy


def test_cop_entries(body):
    assert body["cop"] == [
        {"key": "cop_co", "label": "COP CO", "unit": None, "kind": "cop", "history_fields": COP_FIELDS},
        {"key": "cop_dhw", "label": "COP CWU", "unit": None, "kind": "cop", "history_fields": COP_FIELDS},
        {"key": "cop_total", "label": "COP łącznie", "unit": None, "kind": "cop",
         "history_fields": COP_FIELDS},
    ]


def test_no_topics_or_control_capabilities_are_exposed(client):
    text = client.get("/api/v1/metrics").text
    assert "main/" not in text and "extra/" not in text
    for word in ("topic", "source", "set", "writable", "control"):
        assert f'"{word}"' not in text


# --------------------------------------------------------- cross-endpoint drift


def test_every_live_metric_is_described(body, client):
    live = client.get("/api/v1/live").json()["metrics"]
    assert list(live) == [m["key"] for m in body["metrics"]]


def test_every_history_series_is_described(body, client):
    history = client.get("/api/v1/history", params={
        "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:01:00Z"}).json()["series"]
    described = {m["key"] for m in body["metrics"]} | {c["key"] for c in body["cop"]}
    assert set(history) <= described
    assert set(history) == set(RECORDED_KEYS) | {"cop_co", "cop_dhw", "cop_total"}


def test_catalog_metrics_are_exactly_the_recorded_ones(body):
    # A future non-recorded metric would have no history fields; it must then be
    # described explicitly instead of inheriting this assumption.
    assert [m["key"] for m in body["metrics"]] == list(RECORDED_KEYS)


def test_described_fields_are_exactly_the_fields_history_returns(body, client):
    history = client.get("/api/v1/history", params={
        "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:02:00Z", "bucket": "total"}).json()["series"]
    for entry in body["metrics"] + body["cop"]:
        series = history[entry["key"]]
        assert set(series) == {"label", "unit", "kind"} | set(entry["history_fields"])
        assert (series["label"], series["unit"], series["kind"]) == (
            entry["label"], entry["unit"], entry["kind"])
        assert all(len(series[f]) == 1 for f in entry["history_fields"])


def test_energy_flag_matches_the_kwh_field_actually_returned(body, client):
    history = client.get("/api/v1/history", params={
        "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:02:00Z", "bucket": "total"}).json()["series"]
    for entry in body["metrics"]:
        assert entry["energy"] is ("kwh" in history[entry["key"]])
    assert history["co_power_consumption"]["kwh"] == [900.0 / 60000]


def test_bucket_names_are_the_ones_history_accepts(body, client):
    for bucket in body["history"]["buckets"]:
        r = client.get("/api/v1/history", params={
            "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:02:00Z",
            "bucket": bucket, "series": "outside_temp"})
        assert r.status_code == 200, (bucket, r.text)


# ------------------------------------------------------------- independence


def test_metrics_survives_database_outage(client, storage):
    storage.available = False
    storage.sessions = 0
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200 and storage.sessions == 0
    assert len(r.json()["metrics"]) == len(METRICS)


# --------------------------------------------------------------- adversarial


def test_history_fields_match_an_independent_derivation(body):
    """A second, deliberately hand-written derivation of the same rule."""
    for entry in body["metrics"]:
        metric = METRICS_BY_KEY[entry["key"]]
        expected = ["avg" if metric.kind == "mean" else "last", "min", "max", "minutes"]
        if metric.group == "power" and metric.unit == "W":
            expected.append("kwh")
        assert entry["history_fields"] == expected
        assert entry["energy"] is (metric.group == "power" and metric.unit == "W")


def test_catalog_is_unaffected_by_mqtt_state(client, storage):
    from conftest import RUNNING, Api

    api = Api(storage=storage)
    before = api.body("/api/v1/metrics", T0)
    api.connect(T0).publish(T0 + 1).disconnect(T0 + 2)
    assert api.body("/api/v1/metrics", T0 + 3) == before
    api.connect(T0 + 4).publish(T0 + 5, RUNNING, retained=True)
    assert api.body("/api/v1/metrics", T0 + 6) == before
    assert before == client.get("/api/v1/metrics").json()
