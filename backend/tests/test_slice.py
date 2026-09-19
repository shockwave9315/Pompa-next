"""Vertical slice: MQTT callbacks → ingest → minute close → storage → GET /api/v1/history."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import RUNNING, T0, FakeStorage
from pompa.api import create_app
from pompa.config import load_settings
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.mqtt import MqttAdapter
from pompa.recorder import Recorder

PREFIX = "panasonic_heat_pump"
OK = SimpleNamespace(is_failure=False)
LOST = SimpleNamespace(is_failure=True)


@pytest.fixture(params=["fake", "mariadb"])
def storage(request):
    return FakeStorage() if request.param == "fake" else request.getfixturevalue("mariadb")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeClient:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))


def status_of(api):
    return api.get("/api/v1/status").json()


def test_mqtt_to_history(storage):
    clock = Clock(T0 + 30)  # process starts mid-minute
    settings = load_settings({"MQTT_HOST": "broker", "DB_HOST": "db", "DB_USER": "u"})
    ingest = Ingest(settings.stale_after_seconds)
    recorder = Recorder(ingest, MinuteAccumulator(ingest, clock()), storage, settings.write_buffer_rows)
    adapter = MqttAdapter(settings, recorder, clock=clock)
    client = FakeClient()

    def deliver(topic, payload, retain=False):
        adapter._on_message(client, None, SimpleNamespace(
            topic=f"{PREFIX}/{topic}", payload=payload.encode(), retain=retain))

    def at(t):
        clock.t = t
        return t

    def publish(t, snapshot):
        at(t)
        for topic, payload in snapshot.items():
            deliver(topic, payload)

    at(T0 + 30)
    adapter._on_connect(client, None, None, OK, None)
    assert client.subscriptions == [(f"{PREFIX}/#", 0)]
    deliver("LWT", "Online", retain=True)
    for topic, payload in RUNNING.items():
        deliver(topic, payload, retain=True)
    deliver("main/DHW_Target_Temp", "48")  # not in the core catalog
    adapter._on_message(client, None, SimpleNamespace(topic="other/x", payload=b"1", retain=False))

    for t in range(T0 + 45, T0 + 150, 10):
        publish(t, RUNNING)

    # Connection drop at T0+150; the broker re-delivers retained state on reconnect.
    at(T0 + 150)
    adapter._on_disconnect(client, None, None, LOST, None)
    at(T0 + 155)
    adapter._on_connect(client, None, None, OK, None)
    deliver("LWT", "Online", retain=True)
    for topic, payload in RUNNING.items():
        deliver(topic, payload, retain=True)

    # Live again from T0+170, but XTOP0 is only retained now and pressure is silent.
    after = {k: v for k, v in RUNNING.items()
             if k not in ("extra/Heat_Power_Consumption_Extra", "main/Water_Pressure")}
    for t in range(T0 + 170, T0 + 300, 10):
        publish(t, after)
    recorder.tick(at(T0 + 300))

    api = TestClient(create_app(recorder, storage, clock=clock))
    body = api.get("/api/v1/history", params={
        "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:05:00Z",
        "series": "co_power_consumption,water_pressure,main_outlet_temp"}).json()

    # 0: before process start; 2: disconnect; 1, 3, 4: fully observed.
    assert [b["recorded_minutes"] for b in body["buckets"]] == [0, 1, 0, 1, 1]
    assert body["series"]["co_power_consumption"]["avg"] == [None, 900.0, None, 1000.0, 1000.0]
    assert body["series"]["water_pressure"]["avg"] == [None, 1.7, None, None, None]
    assert body["series"]["main_outlet_temp"]["avg"] == [None, 35.0, None, 35.0, 35.0]

    status = api.get("/api/v1/status").json()
    assert status["mqtt"]["epoch"] == 2 and status["mqtt"]["disconnects"] == 1
    assert status["mqtt"]["lwt"]["retained"] is True
    assert status["mqtt"]["uncatalogued_topics"] == ["main/DHW_Target_Temp"]
    assert status["recorder"]["last_written_minute"] == "2027-01-15T08:04:00Z"
    assert (status["recorder"]["protected_rows"], status["recorder"]["waiting_rows"]) == (0, 0)
    assert status["database"]["oldest_minute"] == "2027-01-15T08:01:00Z"
    assert status["database"]["newest_minute"] == "2027-01-15T08:04:00Z"
    xtop0 = next(s for s in status["sources"] if s["id"] == "XTOP0")
    assert xtop0["seen_live"] is False and xtop0["last_retained"] is True

    # Keep publishing into the next UTC hour so that hour 08:00Z closes and is rolled up.
    for t in range(T0 + 310, T0 + 3720, 10):
        publish(t, after)
    recorder.tick(at(T0 + 3720))
    assert status_of(api)["database"]["rolled_until"] == "2027-01-15T09:00:00Z"
    assert status_of(api)["recorder"]["rollup"]["last_rolled_hour"] == "2027-01-15T08:00:00Z"

    hourly = api.get("/api/v1/history", params={
        "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T09:00:00Z", "bucket": "1h",
        "series": "co_power_consumption,cop_co"}).json()
    assert hourly["bucket"] == "1h"
    # 60 minutes minus the two not recorded: before process start and across the disconnect.
    bucket = hourly["buckets"][0]
    assert (bucket["recorded_minutes"], bucket["expected_minutes"]) == (58, 60)
    assert bucket["coverage_percent"] == 96.7
    # Minute 1 came from XTOP0 (900 W); after the reconnect XTOP0 is only retained, so TOP16
    # (1000 W) supplies the remaining 57 minutes. Production stays on live XTOP3 (3600 W).
    consumed = 900.0 + 57 * 1000.0
    power = hourly["series"]["co_power_consumption"]
    assert power["minutes"] == [58] and power["kwh"] == [pytest.approx(consumed / 60000)]
    cop = hourly["series"]["cop_co"]
    assert cop["paired_minutes"] == [58]
    assert cop["cop"][0] == pytest.approx(58 * 3600.0 / consumed, rel=1e-12)
    assert cop["output_kwh"] == [pytest.approx(58 * 3600.0 / 60000)]

    # The same hour read as one total bucket agrees with the rolled hourly bucket.
    total = api.get("/api/v1/history", params={
        "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T09:00:00Z", "bucket": "total",
        "series": "co_power_consumption,cop_co"}).json()
    assert total["series"] == hourly["series"]
    assert total["buckets"][0]["recorded_minutes"] == bucket["recorded_minutes"]
