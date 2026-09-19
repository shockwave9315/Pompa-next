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
    assert status["recorder"]["buffered_rows"] == 0
    assert status["database"]["oldest_minute"] == "2027-01-15T08:01:00Z"
    assert status["database"]["newest_minute"] == "2027-01-15T08:04:00Z"
    xtop0 = next(s for s in status["sources"] if s["id"] == "XTOP0")
    assert xtop0["seen_live"] is False and xtop0["last_retained"] is True
