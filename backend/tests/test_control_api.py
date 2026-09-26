"""Stage 4E-C control runtime and API (docs/ARCHITECTURE.md §25.5, docs/API.md Stage 4E).

The publisher is a fake with the ``MqttAdapter.publish_command`` signature, so every test counts
exactly which publishes a request made. MQTT readings enter through the recorder entry points,
like the network thread. Readback windows are injected, so no test waits 15 s.
"""

import ast
import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pompa import control, control_runtime
from pompa.api import create_app
from pompa.capabilities import effective_capabilities
from pompa.control_runtime import READBACK_WINDOW_SECONDS, ControlRequestError, ControlRuntime
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder

from conftest import T0

TOPIC = {c.reference.identity: c.topic for c in effective_capabilities()}
# Facts under which every control is executable and request temperatures are in shift mode.
READY = {"TOP76": "0", "TOP81": "0", "TOP4": "4", "TOP110": "1", "TOP122": "1"}


class NoStorage:
    """Any storage access fails the test: control must never touch the database."""

    def __getattr__(self, name):
        raise AssertionError(f"control touched storage.{name}")


class FakePublisher:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.accept = True
        self.raises: Exception | None = None
        self.on_publish = None  # runs after the call is counted, before the result returns

    def publish_command(self, topic, payload):
        self.calls.append((topic, payload))
        if self.raises is not None:
            raise self.raises
        if self.on_publish is not None:
            self.on_publish(topic, payload)
        return self.accept


class Harness:
    def __init__(self, window=2.0, publisher=True):
        self.ingest = Ingest(600)
        self.recorder = Recorder(self.ingest, MinuteAccumulator(self.ingest, T0), NoStorage(), 60)
        self.now = float(T0 + 30)
        self.publisher = FakePublisher() if publisher else None
        self.runtime = ControlRuntime(self.recorder, self.publisher, clock=lambda: self.now,
                                      window_seconds=window)
        self.client = TestClient(create_app(self.recorder, NoStorage(), clock=lambda: self.now,
                                            controls=self.runtime))

    def connect(self):
        self.recorder.on_connect(self.now - 20)
        return self

    def top(self, identity, raw, *, retained=False, age=0.0):
        self.recorder.on_message(TOPIC[identity], str(raw), retained, self.now - age)
        return self

    def ready(self, **extra):
        for identity, raw in {**READY, **extra}.items():
            self.top(identity, raw, age=10)
        return self

    def post(self, key, body=None, raw=None):
        if raw is not None:
            return self.client.post(f"/api/v1/controls/{key}", content=raw,
                                    headers={"content-type": "application/json"})
        return self.client.post(f"/api/v1/controls/{key}", json={} if body is None else body)

    def controls(self):
        body = self.client.get("/api/v1/controls").json()
        return {c["key"]: c for c in body["controls"]}, body


def ready_harness(window=2.0):
    return Harness(window).connect().ready()


# ------------------------------------------------------------------------------ GET /controls

def test_get_returns_every_definition_derived_from_the_domain():
    h = ready_harness()
    by_key, body = h.controls()
    defs = control.definitions()
    assert list(by_key) == list(defs) and len(by_key) == 63
    assert body["mqtt"] == {"connected": True}
    assert body["readback_window_seconds"] == h.runtime.window_seconds == 2.0
    # Production default: the provisional W, until 4E-D measures it on CT109.
    default = TestClient(create_app(h.recorder, NoStorage(), clock=lambda: h.now))
    assert default.get("/api/v1/controls").json()["readback_window_seconds"] == READBACK_WINDOW_SECONDS == 15.0
    assert default.post("/api/v1/controls/heat_delta", json={"value": 3}).json()["code"] == "mqtt_unavailable"
    assert not {"SetHeatCoolMode", "SetOptPCBByte9"} & {c["identity"] for c in by_key.values()}
    facts = control_runtime.reading_facts(h.recorder.readings_observation(lambda: h.now))
    for key, c in defs.items():
        entry = by_key[key]
        assert set(entry) == {"key", "identity", "family", "class", "service", "value", "readback",
                              "state", "prerequisites", "restrictions", "executable",
                              "not_executable_because"}
        assert (entry["identity"], entry["family"], entry["class"], entry["service"]) == (
            c.identity, c.family, c.klass, c.service)
        assert entry["value"] == control.schema(c, facts)
        assert entry["restrictions"] == list(c.restrictions)
        assert [p["id"] for p in entry["prerequisites"]] == [p.id for p in c.prerequisites]
        if c.readback.kind == "none":
            assert entry["readback"] is None and entry["state"] is None
        elif c.readback.fields is not None:
            assert entry["readback"] == {"identity": None, "kind": "state", "fields": dict(c.readback.fields)}
        else:
            assert entry["readback"] == {"identity": c.readback.identity, "kind": c.readback.kind}
        assert entry["executable"] is True and entry["not_executable_because"] == []


def test_get_state_comes_only_from_top_readings():
    h = ready_harness()
    h.top("TOP9", 48).top("TOP18", 4, retained=True).top("TOP29", 32).top("TOP30", "x")
    # Command echoes (HA's retained PCB commands, our own publications) are never state.
    h.recorder.on_message("commands/SetSmartGridMode", "2", True, h.now)
    h.recorder.on_message("commands/SetDHWTemp", "60", False, h.now)
    by_key, body = h.controls()
    assert by_key["dhw_target_temperature"]["state"] == {
        "value": 48, "raw": "48", "mode": "live", "received_at": "2027-01-15T08:00:30Z", "available": True,
    }
    quiet = by_key["quiet_mode"]["state"]
    assert (quiet["value"], quiet["raw"], quiet["mode"], quiet["available"]) == (None, "4", "retained", False)
    curve = by_key["zone1_heat_curve"]["state"]
    assert curve["value"] == {"target_high": 32, "target_low": None, "outside_high": None, "outside_low": None}
    assert curve["fields"]["target_high"]["identity"] == "TOP29"
    assert curve["fields"]["target_low"]["raw"] == "x"
    assert by_key["pcb_smart_grid_mode"]["state"] is None
    assert by_key["force_defrost"]["state"]["value"] is None  # TOP26 not seen yet
    assert h.publisher.calls == []


def test_get_executability():
    h = Harness().connect()
    by_key, _ = h.controls()
    assert by_key["zone1_heat_request"]["not_executable_because"] == ["validation_context_unavailable"]
    assert by_key["zone1_heat_request"]["executable"] is False
    assert by_key["pcb_pool_temperature"]["executable"] is True  # TOP110 unknown never blocks
    assert by_key["pcb_pool_temperature"]["prerequisites"] == [
        {"id": "heat_pump_optional_pcb", "identity": "TOP110", "satisfied": None},
        {"id": "heishamon_optional_pcb_emulation", "identity": None, "satisfied": None},
    ]
    h.top("TOP110", 0, retained=True).top("TOP4", 0, age=700)  # retained and stale prove nothing
    by_key, _ = h.controls()
    assert by_key["pcb_pool_temperature"]["executable"] and by_key["force_dhw"]["executable"]
    h.top("TOP110", 0).top("TOP4", 0).top("TOP76", 1)
    by_key, _ = h.controls()
    assert by_key["pcb_pool_temperature"]["not_executable_because"] == ["prerequisite_not_met"]
    assert by_key["pcb_pool_temperature"]["executable"] is False
    assert by_key["force_dhw"]["not_executable_because"] == ["prerequisite_not_met"]
    assert by_key["zone1_heat_request"]["value"]["active"] == "direct"
    assert by_key["heat_delta"]["executable"] is True
    h.recorder.on_disconnect(h.now)
    by_key, body = h.controls()
    assert body["mqtt"] == {"connected": False}
    assert by_key["heat_delta"]["not_executable_because"] == ["mqtt_disconnected"]
    assert by_key["heat_delta"]["executable"] is False
    # Disconnected readings are no current evidence: the prerequisite is unknown again.
    assert by_key["force_dhw"]["not_executable_because"] == ["mqtt_disconnected"]
    assert by_key["force_dhw"]["prerequisites"][0]["satisfied"] is None


def test_restrictions_never_block_and_zone_sensor_settings_are_not_read():
    h = ready_harness().top("TOP111", 2).top("TOP112", 1)
    by_key, _ = h.controls()
    for key in ("zone1_heat_request", "zone1_cool_request", "zone2_heat_request", "zone2_cool_request"):
        assert by_key[key]["restrictions"] == ["documented_direct_temperature_water_mode_only"]
        assert by_key[key]["executable"] is True
    for key in ("dhw_sensor_selection", "pcb_thermostat1_demand", "force_heater"):
        assert by_key[key]["restrictions"] and by_key[key]["executable"] is True
    h.top("TOP27", -2)
    response = h.post("zone1_heat_request", {"value": -2})
    assert response.status_code == 200 and h.publisher.calls == [("commands/SetZ1HeatRequestTemperature", "-2")]


def test_get_needs_no_database_and_never_publishes():
    h = ready_harness()  # NoStorage fails on any access
    for _ in range(3):
        assert h.client.get("/api/v1/controls").status_code == 200
    assert h.publisher.calls == []


# ------------------------------------------------------------------------ POST errors

ERROR_CASES = [
    ("heat_delta", None, b"not json", 400, "invalid_request"),
    ("heat_delta", None, b"", 400, "invalid_request"),
    ("heat_delta", None, b"[1]", 400, "invalid_request"),
    ("heat_delta", None, b'{"value": 3, "extra": 1}', 400, "invalid_request"),
    ("heat_delta", None, b'{"value": 3, "value": 4}', 400, "invalid_request"),
    ("heat_delta", None, b'{"value": NaN}', 400, "invalid_request"),
    ("zone1_heat_curve", None, b'{"value": {"target_high": 30, "target_high": 31}}', 400, "invalid_request"),
    ("heat_delta", {}, None, 400, "invalid_request"),
    ("force_defrost", {"value": True}, None, 400, "invalid_request"),
    ("force_defrost", {"value": None}, None, 400, "invalid_request"),
    ("nope", {"value": 1}, None, 404, "unknown_control"),
    ("pcb_heat_cool_switch", {"value": True}, None, 404, "unknown_control"),
    ("SetDHWTemp", {"value": 50}, None, 404, "unknown_control"),
    ("quiet_mode", {"value": "Level 1"}, None, 422, "invalid_value"),
    ("heat_delta", {"value": 3.0}, None, 422, "invalid_value"),
    ("heat_delta", {"value": True}, None, 422, "invalid_value"),
    ("heat_delta", {"value": 16}, None, 422, "invalid_value"),
    ("heat_delta", {"value": None}, None, 422, "invalid_value"),
    ("heat_delta", {"value": "3"}, None, 422, "invalid_value"),
    ("zone1_heat_curve", {"value": {}}, None, 422, "invalid_value"),
    ("zone1_heat_curve", {"value": {"target_high": 30, "slope": 1}}, None, 422, "invalid_value"),
    ("zone1_heat_curve", {"value": '{"zone1":{"heat":{"target":{"high":30}}}}'}, None, 422, "invalid_value"),
    ("pcb_demand_control", {"value": 60}, None, 422, "invalid_value"),
]


@pytest.mark.parametrize("key,body,raw,status,code", ERROR_CASES)
def test_every_error_publishes_nothing(key, body, raw, status, code):
    h = ready_harness()
    response = h.post(key, body, raw)
    assert (response.status_code, response.json()["code"]) == (status, code), response.json()
    assert set(response.json()) == {"detail", "code"}
    assert h.publisher.calls == []


def test_prerequisite_and_context_refusals_publish_nothing():
    h = Harness().connect().ready(TOP110="0", TOP4="0")
    for key, body in (("pcb_smart_grid_mode", {"value": "normal"}), ("force_dhw", {"value": True}),
                      ("force_dhw", {"value": False})):  # owner decision F7: false is gated too
        response = h.post(key, body)
        assert (response.status_code, response.json()["code"]) == (409, "prerequisite_not_met")
    h2 = Harness().connect()
    response = h2.post("zone2_cool_request", {"value": 2})
    assert (response.status_code, response.json()["code"]) == (409, "validation_context_unavailable")
    assert h.publisher.calls == [] and h2.publisher.calls == []


def test_mqtt_unavailable_publishes_nothing_and_never_retries():
    h = ready_harness()
    h.publisher.accept = False  # adapter not connected, or paho refused the QoS 0 publish
    response = h.post("heat_delta", {"value": 5})
    assert (response.status_code, response.json()["code"]) == (503, "mqtt_unavailable")
    assert h.publisher.calls == [("commands/SetFloorHeatDelta", "5")]  # one attempt, no retry
    no_publisher = Harness(publisher=False).connect().ready()
    response = no_publisher.post("heat_delta", {"value": 5})
    assert (response.status_code, response.json()["code"]) == (503, "mqtt_unavailable")


# -------------------------------------------------------------------- POST results

def test_no_readback_returns_immediately_with_not_applicable():
    h = ready_harness(window=30.0)
    for key, body, publish in (
        ("fault_reset", {}, ("commands/SetReset", "1")),
        ("pump_service_mode", {"value": True}, ("commands/SetPump", "1")),
        ("pcb_demand_control", {"value": 50}, ("commands/SetDemandControl", "133")),
        ("pcb_zone1_water_temperature", {"value": 35.5}, ("commands/SetZ1WaterTemp", "35.5")),
    ):
        response = h.post(key, body)
        assert response.status_code == 200
        assert h.publisher.calls[-1] == publish
        readback = response.json()["readback"]
        assert readback == {"identity": None, "kind": None, "expected": None, "outcome": "not_applicable",
                            "observed": None, "window_seconds": 30.0, "waited_seconds": 0.0}
    # An echo of the PCB command is still no state and changes nothing.
    h.recorder.on_message("commands/SetDemandControl", "133", False, h.now)
    assert h.controls()[0]["pcb_demand_control"]["state"] is None
    assert len(h.publisher.calls) == 4


def test_matched_after_publish():
    h = ready_harness()
    h.top("TOP9", 48, age=5)
    h.publisher.on_publish = lambda topic, payload: h.top("TOP9", 50)
    response = h.post("dhw_target_temperature", {"value": 50})
    body = response.json()
    assert response.status_code == 200
    assert body["key"] == "dhw_target_temperature" and body["requested"] == 50
    assert body["publish"] == {"status": "sent", "at": "2027-01-15T08:00:30Z"}
    assert body["prerequisites"] == []
    readback = body["readback"]
    assert (readback["identity"], readback["kind"], readback["expected"], readback["outcome"]) == (
        "TOP9", "state", 50, "matched")
    assert readback["observed"]["value"] == 50 and readback["observed"]["raw"] == "50"
    assert readback["waited_seconds"] < 1.0  # returns as soon as it matched
    assert h.publisher.calls == [("commands/SetDHWTemp", "50")]


def test_publish_topic_and_payload_come_from_prepare():
    h = ready_harness(window=0.05)
    cases = [("bivalent_advanced_start_temperature", {"value": -3}, ("commands/SetBivalentAPStartTemp", "-3")),
             ("operation_mode", {"value": "auto_dhw"}, ("commands/SetOperationMode", "6")),
             ("zone2_heat_curve", {"value": {"outside_low": -12, "target_high": 40}},
              ("commands/SetCurves", '{"zone2":{"heat":{"target":{"high":40},"outside":{"low":-12}}}}')),
             ("force_sterilization", {}, ("commands/SetForceSterilization", "1"))]
    for key, body, expected in cases:
        prepared = control.prepare(key, body.get("value", control.ABSENT),
                                   control_runtime.reading_facts(h.recorder.readings_observation(lambda: h.now)))
        assert (prepared.topic, prepared.payload) == expected
        assert h.post(key, body).status_code == 200
        assert h.publisher.calls[-1] == expected


def test_external_writer_during_wait():
    # Another writer sets 49 after our publish; a later 50 is still a factual post-publish match.
    h = ready_harness(window=5.0)
    h.top("TOP9", 50, age=5)

    def later():
        h.top("TOP9", 49)
        threading.Timer(0.05, lambda: h.top("TOP9", 50)).start()

    h.publisher.on_publish = lambda topic, payload: later()
    assert h.post("dhw_target_temperature", {"value": 50}).json()["readback"]["outcome"] == "matched"
    # It stays 49: not observed, and never "unchanged" although 50 was the pre-publish state.
    h = ready_harness(window=0.2)
    h.top("TOP9", 50, age=5)
    h.publisher.on_publish = lambda topic, payload: h.top("TOP9", 49)
    readback = h.post("dhw_target_temperature", {"value": 50}).json()["readback"]
    assert readback["outcome"] == "not_observed"
    assert readback["observed"]["value"] == 49


def test_unchanged_match_waits_the_whole_window():
    h = ready_harness(window=0.3)
    h.top("TOP9", 50, age=5)
    readback = h.post("dhw_target_temperature", {"value": 50}).json()["readback"]
    assert readback["outcome"] == "unchanged_match"
    assert readback["waited_seconds"] >= 0.3
    assert readback["observed"]["value"] == 50


def test_retained_stale_and_absent_readings_never_confirm():
    h = ready_harness(window=0.2)
    h.publisher.on_publish = lambda topic, payload: h.top("TOP9", 50, retained=True)
    readback = h.post("dhw_target_temperature", {"value": 50}).json()["readback"]
    assert (readback["outcome"], readback["observed"]) == ("not_observed", None)
    h = ready_harness(window=0.2)
    h.top("TOP9", 50, age=700)  # a stale equal reading is not a pre-publish match
    readback = h.post("dhw_target_temperature", {"value": 50}).json()["readback"]
    assert (readback["outcome"], readback["observed"]) == ("not_observed", None)
    h = ready_harness(window=0.2)
    h.top("TOP9", 50, retained=True)  # nor is a retained one
    assert h.post("dhw_target_temperature", {"value": 50}).json()["readback"]["outcome"] == "not_observed"
    h = ready_harness(window=0.2)
    h.top("TOP9", 50, age=5)
    # A post-publish undocumented value is a different observation, not a match.
    h.publisher.on_publish = lambda topic, payload: h.top("TOP18", 4)
    h.top("TOP18", 1, age=5)
    readback = h.post("quiet_mode", {"value": "level_1"}).json()["readback"]
    assert readback["outcome"] == "not_observed" and readback["observed"] == {
        "value": None, "raw": "4", "received_at": readback["observed"]["received_at"]}


def test_readback_equality_never_confuses_bool_and_int():
    assert control_runtime._same(True, True) and control_runtime._same(50, 50)
    assert not control_runtime._same(True, 1) and not control_runtime._same(0, False)


def test_a_command_echo_never_confirms():
    h = ready_harness(window=0.2)
    h.publisher.on_publish = lambda topic, payload: h.recorder.on_message(topic, payload, False, h.now)
    readback = h.post("dhw_target_temperature", {"value": 50}).json()["readback"]
    assert readback["outcome"] == "not_observed"
    assert h.recorder.readings_observation(lambda: h.now).readings["TOP9"]["raw"] is None


def test_readback_decodes_through_the_domain_mapping():
    h = ready_harness()
    h.publisher.on_publish = lambda topic, payload: h.top("TOP4", 7)  # Auto(Cool) reports auto
    readback = h.post("operation_mode", {"value": "auto"}).json()["readback"]
    assert (readback["expected"], readback["outcome"], readback["observed"]["raw"]) == ("auto", "matched", "7")
    h.publisher.on_publish = lambda topic, payload: h.top("TOP19", 2)  # holiday active
    assert h.post("holiday_mode", {"value": True}).json()["readback"]["outcome"] == "matched"


def test_effect_readback():
    h = ready_harness()
    h.top("TOP26", 0, age=5)
    h.publisher.on_publish = lambda topic, payload: h.top("TOP26", 1)
    body = h.post("force_defrost", {}).json()
    assert body["requested"] is None
    assert body["readback"]["kind"] == "effect" and body["readback"]["identity"] == "TOP26"
    assert (body["readback"]["expected"], body["readback"]["outcome"]) == (True, "matched")
    h = ready_harness(window=0.2)
    h.top("TOP69", 1, age=5)  # already sterilizing: not attributable
    assert h.post("force_sterilization", {}).json()["readback"]["outcome"] == "unchanged_match"
    h = ready_harness(window=0.2)
    h.top("TOP26", 0, age=5)  # the effect may begin after W: a fact, not a failure
    response = h.post("force_defrost", {})
    assert response.status_code == 200 and response.json()["readback"]["outcome"] == "not_observed"
    assert len(h.publisher.calls) == 1


def test_curve_readback_uses_only_the_requested_fields():
    h = ready_harness(window=5.0)
    h.top("TOP29", 30, age=5).top("TOP32", -10, age=5).top("TOP30", 99, age=5)

    def staggered():
        h.top("TOP29", 32)
        threading.Timer(0.05, lambda: h.top("TOP32", -15)).start()

    h.publisher.on_publish = lambda topic, payload: staggered()
    body = h.post("zone1_heat_curve", {"value": {"target_high": 32, "outside_low": -15}}).json()
    readback = body["readback"]
    assert body["requested"] == {"target_high": 32, "outside_low": -15}
    assert readback["fields"] == {"target_high": "TOP29", "outside_low": "TOP32"}
    assert (readback["identity"], readback["expected"], readback["outcome"]) == (
        None, {"target_high": 32, "outside_low": -15}, "matched")
    assert {name: seen["value"] for name, seen in readback["observed"].items()} == {
        "target_high": 32, "outside_low": -15}
    # Only one field arrives: not observed, with the per-field facts.
    h = ready_harness(window=0.2)
    h.top("TOP29", 30, age=5)
    h.publisher.on_publish = lambda topic, payload: h.top("TOP29", 32)
    readback = h.post("zone1_heat_curve", {"value": {"target_high": 32, "outside_low": -15}}).json()["readback"]
    assert readback["outcome"] == "not_observed"
    assert readback["observed"]["outside_low"] is None and readback["observed"]["target_high"]["value"] == 32
    # Every requested field already matched and nothing different arrived: unchanged.
    h = ready_harness(window=0.2)
    h.top("TOP29", 32, age=5).top("TOP32", -15, age=5).top("TOP30", 1, age=5)
    readback = h.post("zone1_heat_curve", {"value": {"target_high": 32, "outside_low": -15}}).json()["readback"]
    assert readback["outcome"] == "unchanged_match"
    # A requested field changes to something else after publish: not unchanged.
    h = ready_harness(window=0.2)
    h.top("TOP29", 32, age=5).top("TOP32", -15, age=5)
    h.publisher.on_publish = lambda topic, payload: h.top("TOP32", -14)
    readback = h.post("zone1_heat_curve", {"value": {"target_high": 32, "outside_low": -15}}).json()["readback"]
    assert readback["outcome"] == "not_observed"


# -------------------------------------------------------------- disconnect and reconnect

def test_disconnect_after_an_accepted_publish_stays_a_factual_200():
    h = ready_harness(window=0.2)
    h.publisher.on_publish = lambda topic, payload: h.recorder.on_disconnect(h.now)
    response = h.post("dhw_target_temperature", {"value": 50})
    assert response.status_code == 200
    assert response.json()["publish"]["status"] == "sent"
    assert response.json()["readback"]["outcome"] == "not_observed"
    assert len(h.publisher.calls) == 1  # never retried


def test_reconnect_during_the_window_replays_nothing_and_retained_does_not_confirm():
    h = ready_harness(window=5.0)

    def bounce():
        h.recorder.on_disconnect(h.now)
        h.recorder.on_connect(h.now)
        h.top("TOP9", 50, retained=True)  # the broker's retained delivery after reconnect
        threading.Timer(0.05, lambda: h.top("TOP9", 50)).start()  # a genuinely live one later

    h.publisher.on_publish = lambda topic, payload: bounce()
    readback = h.post("dhw_target_temperature", {"value": 50}).json()["readback"]
    assert readback["outcome"] == "matched"
    assert len(h.publisher.calls) == 1
    h = ready_harness(window=0.2)
    h.publisher.on_publish = lambda topic, payload: (h.recorder.on_disconnect(h.now), h.recorder.on_connect(h.now),
                                                     h.top("TOP9", 50, retained=True))
    assert h.post("dhw_target_temperature", {"value": 50}).json()["readback"]["outcome"] == "not_observed"
    assert len(h.publisher.calls) == 1


def test_a_new_runtime_has_no_pending_command():
    h = ready_harness()
    first = ControlRuntime(h.recorder, h.publisher, clock=lambda: h.now, window_seconds=0.05)
    first._claim("heat_delta")  # a request that never finished in a previous process
    second = ControlRuntime(h.recorder, h.publisher, clock=lambda: h.now, window_seconds=0.05)
    assert second.execute("heat_delta", 3)["publish"]["status"] == "sent"
    assert h.publisher.calls == [("commands/SetFloorHeatDelta", "3")]


# ----------------------------------------------------------------------- in-flight guard

def _run(runtime, key, value, results):
    try:
        results[key] = runtime.execute(key, value)
    except ControlRequestError as error:
        results[key] = error


def test_same_key_is_refused_while_the_first_request_waits():
    h = ready_harness(window=10.0)
    published = threading.Event()
    h.publisher.on_publish = lambda topic, payload: published.set()
    results = {}
    first = threading.Thread(target=_run, args=(h.runtime, "dhw_target_temperature", 50, results))
    first.start()
    assert published.wait(5)
    response = h.post("dhw_target_temperature", {"value": 51})
    assert (response.status_code, response.json()["code"]) == (409, "command_in_progress")
    h.top("TOP9", 50)  # ends the first request's window
    first.join(5)
    assert results["dhw_target_temperature"]["readback"]["outcome"] == "matched"
    assert h.publisher.calls == [("commands/SetDHWTemp", "50")]
    assert h.post("dhw_target_temperature", {"value": 50}).status_code == 200  # released


def test_simultaneous_same_key_requests_publish_once():
    h = ready_harness(window=0.3)
    barrier = threading.Barrier(8)
    results = []

    def go():
        barrier.wait()
        try:
            results.append(h.runtime.execute("heat_delta", 4))
        except ControlRequestError as error:
            results.append(error.code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(r if isinstance(r, str) else "sent" for r in results) == ["command_in_progress"] * 7 + ["sent"]
    assert h.publisher.calls == [("commands/SetFloorHeatDelta", "4")]


def test_different_keys_proceed_concurrently():
    h = ready_harness(window=10.0)
    published = threading.Event()
    h.publisher.on_publish = lambda topic, payload: published.set() if topic == "commands/SetDHWTemp" else None
    results = {}
    first = threading.Thread(target=_run, args=(h.runtime, "dhw_target_temperature", 50, results))
    first.start()
    assert published.wait(5)
    h.publisher.on_publish = lambda topic, payload: h.top("TOP23", 6)
    assert h.post("heat_delta", {"value": 6}).json()["readback"]["outcome"] == "matched"
    h.top("TOP9", 50)
    first.join(5)
    assert [call[0] for call in h.publisher.calls] == ["commands/SetDHWTemp", "commands/SetFloorHeatDelta"]


def test_guard_is_released_after_failure_timeout_and_exception():
    h = ready_harness(window=0.05)
    h.publisher.accept = False
    assert h.post("heat_delta", {"value": 5}).status_code == 503
    h.publisher.accept = True
    assert h.post("heat_delta", {"value": 5}).json()["readback"]["outcome"] == "not_observed"  # timeout
    assert h.post("heat_delta", {"value": 5}).status_code == 200
    h.publisher.raises = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        h.runtime.execute("heat_delta", 5)
    h.publisher.raises = None
    assert h.post("heat_delta", {"value": 5}).status_code == 200
    assert h.post("heat_delta", {"value": 99}).status_code == 422  # validation failure also releases
    assert h.post("heat_delta", {"value": 5}).status_code == 200
    assert h.runtime._in_flight == set()


# -------------------------------------------------------------------- isolation and logging

def test_post_needs_no_database():
    h = ready_harness()  # NoStorage fails on any access
    h.publisher.on_publish = lambda topic, payload: h.top("TOP23", 7)
    assert h.post("heat_delta", {"value": 7}).json()["readback"]["outcome"] == "matched"


def test_waiting_never_blocks_ingest():
    h = ready_harness(window=10.0)
    published = threading.Event()
    h.publisher.on_publish = lambda topic, payload: published.set()
    results = {}
    waiter = threading.Thread(target=_run, args=(h.runtime, "dhw_target_temperature", 50, results))
    waiter.start()
    assert published.wait(5)
    applied = threading.Event()
    threading.Thread(target=lambda: (h.top("TOP23", 3), applied.set())).start()
    assert applied.wait(1)  # the recorder lock is free while the request waits
    assert h.client.get("/api/v1/live").status_code == 200
    h.top("TOP9", 50)
    waiter.join(5)
    assert results["dhw_target_temperature"]["readback"]["outcome"] == "matched"


def test_recorder_and_ingest_do_not_depend_on_control():
    root = Path(control.__file__).parent
    for name in ("recorder.py", "ingest.py", "storage.py", "history.py", "minute.py", "mqtt.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        modules |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
        assert not any("control" in (m or "") for m in modules), name


def test_one_log_line_per_request(caplog):
    h = ready_harness(window=0.05)
    with caplog.at_level(logging.INFO, logger="pompa.control_runtime"):
        h.post("heat_delta", {"value": 5})
        h.post("heat_delta", {"value": 50})
        h.post("fault_reset", {})
    lines = [r.getMessage() for r in caplog.records if r.name == "pompa.control_runtime"]
    assert len(lines) == 3
    assert "key=heat_delta requested=5 publish=sent readback=not_observed" in lines[0]
    assert "publish=invalid_value readback=None" in lines[1]
    assert "key=fault_reset requested={} publish=sent readback=not_applicable" in lines[2]


PARSER_FAILURES = [
    ("heat_delta", b"not json"),
    ("heat_delta", b'{"value":3,"value":4}'),
    ("heat_delta", b'{"value":3,"extra":1}'),
    ("heat_delta", b""),
    ("heat_delta", b"[3]"),
    ("heat_delta", b'{"value": NaN}'),
    ("zone1_heat_curve", b'{"value":{"target_high":30,"target_high":31}}'),
]


def _control_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "pompa.control_runtime"]


@pytest.mark.parametrize("key,raw", PARSER_FAILURES)
def test_a_body_refused_before_the_runtime_logs_exactly_one_line(key, raw, caplog):
    h = ready_harness()
    with caplog.at_level(logging.INFO, logger="pompa.control_runtime"):
        response = h.post(key, raw=raw)
    assert (response.status_code, response.json()) == (400, {**response.json(), "code": "invalid_request"})
    assert h.publisher.calls == []
    lines = _control_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith(
        f"control key={key} requested=<invalid_request> publish=invalid_request readback=None elapsed=")
    for fragment in (b"not json", b"extra", b"NaN", b"31", b"[3]"):
        if fragment in raw:
            assert fragment.decode() not in lines[0]  # the raw body never reaches the log


def test_requests_that_reach_the_runtime_still_log_exactly_one_line(caplog):
    h = ready_harness(window=0.05)
    requests = [("heat_delta", {"value": 50}, 422), ("nope", {"value": 1}, 404),
                ("force_defrost", {"value": True}, 400), ("heat_delta", {"value": 5}, 200)]
    for key, body, status in requests:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="pompa.control_runtime"):
            assert h.post(key, body).status_code == status
        lines = _control_lines(caplog)
        assert len(lines) == 1 and f"control key={key} " in lines[0], lines
        assert "<invalid_request>" not in lines[0]


def test_an_arbitrary_path_key_stays_on_one_log_line(caplog):
    h = ready_harness()
    with caplog.at_level(logging.INFO, logger="pompa.control_runtime"):
        assert h.post("bad%0Akey", {"value": 1}).status_code == 404
        assert h.post("bad%0Akey", raw=b"x").status_code == 400
    lines = _control_lines(caplog)
    assert len(lines) == 2 and not any("\n" in line for line in lines)
    assert all(line.startswith("control key='bad\\nkey' ") for line in lines)


# ----------------------------------------------------------------------------- MQTT adapter

class FakeInfo:
    def __init__(self, rc):
        self.rc = rc


class FakeClient:
    def __init__(self, connected=True, rc=0, raises=None):
        self.connected, self.rc, self.raises, self.calls = connected, rc, raises, []

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos, retain):
        self.calls.append((topic, payload, qos, retain))
        if self.raises:
            raise self.raises
        return FakeInfo(self.rc)


def _adapter(client):
    from pompa.mqtt import MqttAdapter

    adapter = MqttAdapter.__new__(MqttAdapter)
    adapter.prefix = "panasonic_heat_pump/"
    adapter.client = client
    return adapter


def test_adapter_publishes_one_qos0_non_retained_command_only_while_connected():
    import paho.mqtt.client as mqtt

    client = FakeClient()
    assert _adapter(client).publish_command("commands/SetDHWTemp", "50") is True
    assert client.calls == [("panasonic_heat_pump/commands/SetDHWTemp", "50", 0, False)]
    offline = FakeClient(connected=False)
    assert _adapter(offline).publish_command("commands/SetDHWTemp", "50") is False and offline.calls == []
    for failing in (FakeClient(rc=mqtt.MQTT_ERR_NO_CONN), FakeClient(rc=mqtt.MQTT_ERR_QUEUE_SIZE),
                    FakeClient(raises=ValueError("bad"))):
        assert _adapter(failing).publish_command("commands/SetDHWTemp", "50") is False
        assert len(failing.calls) == 1  # no retry
    with pytest.raises(ValueError):
        _adapter(FakeClient()).publish_command("main/DHW_Target_Temp", "50")  # never a generic publish
