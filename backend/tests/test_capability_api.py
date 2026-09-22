"""Stage 4A opt-in API shapes; Stage 3 defaults stay byte-for-byte factual."""

from conftest import Api, T0
from pompa.catalog import METRICS


CAPABILITY_FIELDS = {
    "identity", "key", "family", "name", "topic", "description", "provenance",
    "readable", "canonical_metric", "source_priority",
}
READING_FIELDS = {"topic", "value", "kind", "raw", "mode", "received_at"}
PATHS = {"/health", "/api/v1/status", "/api/v1/live", "/api/v1/metrics", "/api/v1/history"}
RANGE = {"from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:01:00Z"}


def test_default_bodies_are_exactly_the_base_of_opt_in_bodies():
    api = Api()
    api.connect(T0)
    api.msg(T0 + 1, "main/DHW_Target_Temp", "48")
    metrics = api.body("/api/v1/metrics", T0 + 2)
    expanded_metrics = api.body("/api/v1/metrics", T0 + 2, include="capabilities")
    assert set(metrics) == {"timezone", "history", "metrics", "cop"}
    assert set(expanded_metrics) == set(metrics) | {"capabilities"}
    assert {key: value for key, value in expanded_metrics.items() if key != "capabilities"} == metrics
    live = api.body("/api/v1/live", T0 + 2)
    expanded_live = api.body("/api/v1/live", T0 + 2, include="readings")
    assert set(live) == {"now", "mqtt", "metrics"}
    assert set(expanded_live) == set(live) | {"readings"}
    assert {key: value for key, value in expanded_live.items() if key != "readings"} == live
    assert list(live["metrics"]) == [m.key for m in METRICS]
    assert all(set(entry) == {"value", "mode", "source_id", "source_topic", "received_at"}
               for entry in live["metrics"].values())


def test_capabilities_have_exact_count_order_shape_and_factual_topics():
    entries = Api().body("/api/v1/metrics", include="capabilities")["capabilities"]
    expected = (
        [f"TOP{i}" for i in range(144)]
        + [f"OPT{i}" for i in range(7)]
        + [f"SET{i}" for i in range(1, 47)]
        + [f"XTOP{i}" for i in range(6)]
    )
    assert len(entries) == 203
    assert [entry["identity"] for entry in entries] == expected
    assert all(set(entry) == CAPABILITY_FIELDS for entry in entries)
    by_id = {entry["identity"]: entry for entry in entries}
    assert by_id["TOP9"] == {
        "identity": "TOP9", "key": "top_9", "family": "TOP", "name": "DHW_Target_Temp",
        "topic": "main/DHW_Target_Temp", "description": "DHW target temperature (°C)",
        "provenance": "documented", "readable": True,
        "canonical_metric": None, "source_priority": None,
    }
    assert by_id["OPT3"]["topic"] == "optional/Z2_Mixing_Valve"
    assert by_id["SET1"]["topic"] == "commands/SetHeatpump"
    assert all(by_id[identity]["readable"] is False for identity in expected if identity.startswith("SET"))
    assert all(by_id[identity]["readable"] is True for identity in expected if not identity.startswith("SET"))
    for identity, topic in {
        "XTOP0": "extra/Heat_Power_Consumption_Extra",
        "XTOP1": "extra/Cool_Power_Consumption_Extra",
        "XTOP2": "extra/DHW_Power_Consumption_Extra",
        "XTOP3": "extra/Heat_Power_Production_Extra",
        "XTOP4": "extra/Cool_Power_Production_Extra",
        "XTOP5": "extra/DHW_Power_Production_Extra",
    }.items():
        assert by_id[identity]["topic"] == topic
        assert by_id[identity]["provenance"] == "observed"
    assert all(entry["topic"] is not None for entry in entries if entry["readable"])


def test_canonical_associations_and_source_priority_are_from_core_catalog():
    entries = {entry["identity"]: entry for entry in Api().body(
        "/api/v1/metrics", include="capabilities"
    )["capabilities"]}
    for metric, preferred, fallback in (
        ("co_power_consumption", "XTOP0", "TOP16"),
        ("co_power_production", "XTOP3", "TOP15"),
        ("dhw_power_consumption", "XTOP2", "TOP41"),
        ("dhw_power_production", "XTOP5", "TOP40"),
    ):
        assert (entries[preferred]["canonical_metric"], entries[preferred]["source_priority"]) == (metric, 0)
        assert (entries[fallback]["canonical_metric"], entries[fallback]["source_priority"]) == (metric, 1)
    assert entries["TOP6"]["canonical_metric"] == "main_outlet_temp"
    assert entries["TOP6"]["source_priority"] == 0


def test_unseen_readings_include_every_slot_with_exact_none_shape():
    body = Api().body("/api/v1/live", include="readings")
    readings = body["readings"]
    expected = (
        [f"TOP{i}" for i in range(144)]
        + [f"OPT{i}" for i in range(7)]
        + [f"XTOP{i}" for i in range(6)]
    )
    assert len(readings) == 157
    assert list(readings) == expected
    assert all(set(entry) == READING_FIELDS for entry in readings.values())
    assert readings["TOP9"] == {
        "topic": "main/DHW_Target_Temp", "value": None, "kind": None,
        "raw": None, "mode": "none", "received_at": None,
    }
    assert readings["OPT3"] == {
        "topic": "optional/Z2_Mixing_Valve", "value": None, "kind": None,
        "raw": None, "mode": "none", "received_at": None,
    }
    for identity, topic in (
        ("XTOP1", "extra/Cool_Power_Consumption_Extra"),
        ("XTOP4", "extra/Cool_Power_Production_Extra"),
    ):
        assert readings[identity] == {
            "topic": topic, "value": None, "kind": None,
            "raw": None, "mode": "none", "received_at": None,
        }
    assert all(entry["topic"] is not None for entry in readings.values())
    assert not any(identity.startswith("SET") for identity in readings)
    assert body["mqtt"] == {"connected": False, "alive": False, "epoch": 0}


def test_numeric_text_retained_live_and_physical_sentinel_values():
    api = Api()
    api.connect(T0)
    api.msg(T0 + 1, "main/DHW_Target_Temp", "48")
    api.msg(T0 + 2, "main/Error", "No error", retained=True)
    model = "E2 D5 0B 08 95 02 D6 0F 68 95"
    api.msg(T0 + 3, "main/Heat_Pump_Model", model)
    api.msg(T0 + 4, "main/Heat_Power_Production", "-200")
    readings = api.body("/api/v1/live", T0 + 5, include="readings")["readings"]
    assert readings["TOP9"] == {
        "topic": "main/DHW_Target_Temp", "value": 48, "kind": "number", "raw": "48",
        "mode": "live", "received_at": "2027-01-15T08:00:01Z",
    }
    assert readings["TOP44"] == {
        "topic": "main/Error", "value": "No error", "kind": "text", "raw": "No error",
        "mode": "retained", "received_at": "2027-01-15T08:00:02Z",
    }
    assert readings["TOP92"]["value"] == model
    assert readings["TOP92"]["kind"] == "text"
    assert readings["TOP15"] == {
        "topic": "main/Heat_Power_Production", "value": -200, "kind": "number",
        "raw": "-200", "mode": "live", "received_at": "2027-01-15T08:00:04Z",
    }
    canonical = api.body("/api/v1/live", T0 + 5)["metrics"]["co_power_production"]
    assert canonical["value"] is None and canonical["mode"] == "none"


def test_verified_cooling_xtop_paths_appear_in_opt_in_readings_only():
    api = Api()
    api.connect(T0)
    api.msg(T0 + 1, "extra/Cool_Power_Consumption_Extra", "12.5")
    api.msg(T0 + 2, "extra/Cool_Power_Production_Extra", "0", retained=True)
    body = api.body("/api/v1/live", T0 + 3, include="readings")
    assert body["readings"]["XTOP1"] == {
        "topic": "extra/Cool_Power_Consumption_Extra", "value": 12.5,
        "kind": "number", "raw": "12.5", "mode": "live",
        "received_at": "2027-01-15T08:00:01Z",
    }
    assert body["readings"]["XTOP4"] == {
        "topic": "extra/Cool_Power_Production_Extra", "value": 0,
        "kind": "number", "raw": "0", "mode": "retained",
        "received_at": "2027-01-15T08:00:02Z",
    }
    assert "readings" not in api.body("/api/v1/live", T0 + 3)


def test_opt_in_forms_need_neither_mqtt_nor_database():
    api = Api()
    api.storage.available = False
    metrics = api.get("/api/v1/metrics", include="capabilities")
    live = api.get("/api/v1/live", include="readings")
    assert metrics.status_code == live.status_code == 200
    assert len(metrics.json()["capabilities"]) == 203
    assert len(live.json()["readings"]) == 157
    assert all(entry["mode"] == "none" for entry in live.json()["readings"].values())
    assert api.storage.schema_calls == api.storage.upsert_calls == 0


def test_invalid_empty_comma_or_repeated_include_is_400():
    api = Api()
    for path, invalid in (
        ("/api/v1/live", "capabilities"),
        ("/api/v1/live", "all"),
        ("/api/v1/live", "readings,capabilities"),
        ("/api/v1/live", ""),
        ("/api/v1/metrics", "readings"),
        ("/api/v1/metrics", "all"),
        ("/api/v1/metrics", "capabilities,readings"),
        ("/api/v1/metrics", ""),
    ):
        assert api.client.get(path, params={"include": invalid}).status_code == 400
    for path, valid in (("/api/v1/live", "readings"), ("/api/v1/metrics", "capabilities")):
        assert api.client.get(path, params=[("include", valid), ("include", valid)]).status_code == 400


def test_history_and_openapi_keep_the_five_paths_and_document_only_accepted_includes():
    api = Api()
    assert api.get("/api/v1/history", None, **{**RANGE, "series": "top_9"}).status_code == 400
    spec = api.body("/openapi.json")
    assert set(spec["paths"]) == PATHS
    for path, expected in (("/api/v1/live", "readings"), ("/api/v1/metrics", "capabilities")):
        params = spec["paths"][path]["get"]["parameters"]
        assert len(params) == 1
        assert (params[0]["name"], params[0]["in"], params[0]["required"]) == (
            "include", "query", False
        )
        assert params[0]["schema"]["anyOf"] == [
            {"const": expected, "type": "string"}, {"type": "null"},
        ]
    assert not spec["paths"]["/api/v1/status"]["get"].get("parameters")


def test_opt_in_live_uses_one_lock_and_reads_physical_state_under_it():
    api = Api()
    api.connect(T0)
    original_lock = api.recorder._lock
    original_snapshot = api.ingest.physical_snapshot

    class CountingLock:
        entries = 0
        held = False

        def __enter__(self):
            original_lock.acquire()
            self.entries += 1
            self.held = True

        def __exit__(self, *_):
            self.held = False
            original_lock.release()

    lock = CountingLock()
    api.recorder._lock = lock

    def checked_snapshot():
        assert lock.held
        return original_snapshot()

    api.ingest.physical_snapshot = checked_snapshot

    def separate_snapshot_is_forbidden():
        raise AssertionError("physical_readings() would take a second lock")

    api.recorder.physical_readings = separate_snapshot_is_forbidden
    response = api.get("/api/v1/live", T0 + 1, include="readings")
    assert response.status_code == 200
    assert lock.entries == 1
