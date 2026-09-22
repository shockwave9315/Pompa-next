"""Readable physical facts stay separate from canonical history and HTTP output."""

import threading
from dataclasses import fields

from conftest import Api, RUNNING, T0
from pompa.capabilities import TypedPayload, effective_capabilities
from pompa.catalog import RECORDED_KEYS, Outcome
from pompa.ingest import Ingest, PhysicalReading


def test_all_and_only_readable_reference_identities_have_small_slots():
    ingest = Ingest(600)
    expected = tuple(
        [f"TOP{i}" for i in range(144)]
        + [f"OPT{i}" for i in range(7)]
        + [f"XTOP{i}" for i in range(6)]
    )
    assert tuple(ingest.physical_readings) == expected
    assert tuple(reading.identity for reading in ingest.physical_snapshot()) == expected
    assert len(ingest.physical_readings) == 157
    assert len(ingest._physical_by_topic) == 157
    assert all(reading.topic is not None for reading in ingest.physical_snapshot())
    assert not any(identity.startswith("SET") for identity in ingest.physical_readings)
    assert {field.name for field in fields(PhysicalReading)} == {
        "identity", "topic", "payload", "received_at", "retained",
    }
    assert len(ingest.sources) == 25  # canonical diagnostics are not cloned per capability
    assert all(r.payload is r.received_at is r.retained is None for r in ingest.physical_snapshot())


def test_documented_topics_and_all_verified_xtop_paths_are_mapped():
    ingest = Ingest(600)
    for capability in effective_capabilities():
        reference = capability.reference
        if reference.family in ("TOP", "OPT"):
            assert ingest.physical_readings[reference.identity].topic == reference.topic
    assert ingest.physical_readings["TOP9"].topic == "main/DHW_Target_Temp"
    assert ingest.physical_readings["OPT3"].topic == "optional/Z2_Mixing_Valve"
    assert {identity: ingest.physical_readings[identity].topic for identity in (
        "XTOP0", "XTOP1", "XTOP2", "XTOP3", "XTOP4", "XTOP5",
    )} == {
        "XTOP0": "extra/Heat_Power_Consumption_Extra",
        "XTOP1": "extra/Cool_Power_Consumption_Extra",
        "XTOP2": "extra/DHW_Power_Consumption_Extra",
        "XTOP3": "extra/Heat_Power_Production_Extra",
        "XTOP4": "extra/Cool_Power_Production_Extra",
        "XTOP5": "extra/DHW_Power_Production_Extra",
    }
    assert len(set(ingest._physical_by_topic)) == 157


def test_noncore_numeric_text_model_and_opt_readings_replace_in_place():
    ingest = Ingest(600)
    ingest.connect(T0)
    ingest.message("main/DHW_Target_Temp", "48", False, T0 + 1)
    assert ingest.physical_readings["TOP9"] == PhysicalReading(
        "TOP9", "main/DHW_Target_Temp", TypedPayload("48", 48, "number"), T0 + 1, False
    )
    ingest.message("main/Error", "No error", True, T0 + 2)
    assert ingest.physical_readings["TOP44"].payload == TypedPayload("No error", "No error", "text")
    assert ingest.physical_readings["TOP44"].retained is True
    model = "E2 D5 0B 08 95 02 D6 0F 68 95"
    ingest.message("main/Heat_Pump_Model", model, False, T0 + 3)
    assert ingest.physical_readings["TOP92"].payload == TypedPayload(model, model, "text")
    ingest.message("optional/Z2_Mixing_Valve", "0", True, T0 + 4)
    assert ingest.physical_readings["OPT3"] == PhysicalReading(
        "OPT3", "optional/Z2_Mixing_Valve", TypedPayload("0", 0, "number"), T0 + 4, True
    )
    ingest.message("main/DHW_Target_Temp", "49", False, T0 + 5)
    assert ingest.physical_readings["TOP9"] == PhysicalReading(
        "TOP9", "main/DHW_Target_Temp", TypedPayload("49", 49, "number"), T0 + 5, False
    )
    assert ingest.physical_readings["OPT0"].payload is None
    assert ingest.uncatalogued_topics == {
        "main/DHW_Target_Temp", "main/Error", "main/Heat_Pump_Model",
        "optional/Z2_Mixing_Valve",
    }


def test_core_xtop_and_physical_sentinel_coexist_with_canonical_processing():
    ingest = Ingest(600)
    ingest.connect(T0)
    ingest.message("extra/Heat_Power_Consumption_Extra", "18", True, T0 + 1)
    assert ingest.physical_readings["XTOP0"].payload == TypedPayload("18", 18, "number")
    assert ingest.physical_readings["XTOP0"].retained is True
    assert not ingest.sources["extra/Heat_Power_Consumption_Extra"].seen_live
    ingest.message("main/Heat_Power_Production", "-200", False, T0 + 2)
    physical = ingest.physical_readings["TOP15"]
    assert physical.payload == TypedPayload("-200", -200, "number")
    assert physical.retained is False
    source = ingest.sources["main/Heat_Power_Production"]
    assert (source.last_value, source.last_outcome, source.value) == (
        None, Outcome.SENTINEL, None
    )
    assert ingest.historical("co_power_production", T0 + 3) is None


def test_absent_opt_and_unknown_topic_create_no_reading():
    ingest = Ingest(600)
    ingest.connect(T0)
    before = ingest.physical_snapshot()
    ingest.message("unrelated/Anything", "7", False, T0 + 1)
    ingest.message("commands/SetHeatpump", "1", False, T0 + 2)
    assert ingest.physical_snapshot() == before
    assert ingest.physical_readings["OPT3"].payload is None
    assert ingest.uncatalogued_topics == {
        "unrelated/Anything", "commands/SetHeatpump",
    }


def test_verified_cooling_xtops_are_typed_physical_only_and_remain_uncatalogued():
    api = Api()
    api.connect(T0)
    before_live = api.body("/api/v1/live", T0 + 3)
    before_metrics = api.body("/api/v1/metrics", T0 + 3)
    history_args = {"from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:01:00Z"}
    before_history = api.body("/api/v1/history", T0 + 3, **history_args)

    api.msg(T0 + 1, "extra/Cool_Power_Consumption_Extra", "12.5")
    api.msg(T0 + 2, "extra/Cool_Power_Production_Extra", "No error", retained=True)

    assert api.ingest.physical_readings["XTOP1"] == PhysicalReading(
        "XTOP1", "extra/Cool_Power_Consumption_Extra",
        TypedPayload("12.5", 12.5, "number"), T0 + 1, False
    )
    assert api.ingest.physical_readings["XTOP4"] == PhysicalReading(
        "XTOP4", "extra/Cool_Power_Production_Extra",
        TypedPayload("No error", "No error", "text"), T0 + 2, True
    )
    assert not any(topic.startswith("extra/Cool_") for topic in api.ingest.sources)
    assert api.ingest.uncatalogued_topics == {
        "extra/Cool_Power_Consumption_Extra", "extra/Cool_Power_Production_Extra"
    }
    assert api.body("/api/v1/live", T0 + 3) == before_live
    assert api.body("/api/v1/metrics", T0 + 3) == before_metrics
    assert api.body("/api/v1/history", T0 + 3, **history_args) == before_history
    for identity in ("xtop_1", "xtop_4"):
        assert api.get("/api/v1/history", T0 + 3, **{**history_args, "series": identity}).status_code == 400
    api.tick(T0 + 60)
    assert api.storage.rows == {}
    assert api.storage.rollup == {}
    assert api.storage.upsert_calls == 0
    assert tuple(RECORDED_KEYS) == tuple(api.ingest._by_metric)


def test_connection_disconnect_offline_and_clock_step_clear_current_readings():
    api = Api()
    api.connect(T0)
    api.msg(T0 + 1, "main/DHW_Target_Temp", "48")
    assert api.ingest.physical_readings["TOP9"].payload is not None
    api.connect(T0 + 2)  # a new epoch discards the previous physical value
    assert api.ingest.physical_readings["TOP9"].payload is None
    api.msg(T0 + 3, "main/DHW_Target_Temp", "48", retained=True)
    assert api.ingest.physical_readings["TOP9"].retained is True
    api.disconnect(T0 + 4)
    assert api.ingest.physical_readings["TOP9"] == PhysicalReading(
        "TOP9", "main/DHW_Target_Temp"
    )
    api.connect(T0 + 5)
    api.msg(T0 + 10, "main/DHW_Target_Temp", "49")
    api.lwt(T0 + 11, "Offline")
    assert api.ingest.physical_readings["TOP9"].payload is None
    api.lwt(T0 + 12, "Online")
    api.msg(T0 + 20, "main/DHW_Target_Temp", "50")
    api.msg(T0 + 15, "main/Error", "No error")  # Recorder detects backward wall clock
    assert api.ingest.clock_steps == 1
    assert api.ingest.physical_readings["TOP9"].payload is None
    assert api.ingest.physical_readings["TOP44"] == PhysicalReading(
        "TOP44", "main/Error", TypedPayload("No error", "No error", "text"), T0 + 15, False
    )


def test_noncore_message_does_not_write_database_or_change_default_api():
    api = Api()
    api.connect(T0)
    before_live = api.body("/api/v1/live", T0 + 1)
    before_metrics = api.body("/api/v1/metrics", T0 + 1)
    before_status = api.body("/api/v1/status", T0 + 1)
    before_history = api.body(
        "/api/v1/history", T0 + 1,
        **{"from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:01:00Z"}
    )
    api.msg(T0 + 1, "main/DHW_Target_Temp", "48")
    assert api.recorder.physical_readings()[9].payload == TypedPayload("48", 48, "number")
    assert api.body("/api/v1/live", T0 + 1) == before_live
    assert api.body("/api/v1/metrics", T0 + 1) == before_metrics
    assert api.body(
        "/api/v1/history", T0 + 1,
        **{"from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:01:00Z"}
    ) == before_history
    after_status = api.body("/api/v1/status", T0 + 1)
    assert after_status["mqtt"]["uncatalogued_topics"] == ["main/DHW_Target_Temp"]
    after_status["mqtt"]["uncatalogued_topics"] = []
    assert after_status == before_status
    assert api.storage.rows == {}
    assert api.storage.upsert_calls == 0
    assert api.storage.schema_calls == 0


def test_noncore_reading_does_not_change_canonical_source_priority_or_minute_row():
    api = Api()
    api.connect(T0)
    for t in range(T0, T0 + 60, 10):
        api.publish(t, RUNNING)
        api.msg(t, "main/DHW_Target_Temp", "48")
    assert api.ingest.historical("co_power_consumption", T0 + 59).source.id == "XTOP0"
    api.tick(T0 + 60)
    row = api.storage.rows[T0]
    assert tuple(row) == RECORDED_KEYS
    assert row["co_power_consumption"] == 900.0  # preferred XTOP0, not TOP16
    assert row["main_outlet_temp"] == 35.0
    assert api.recorder.physical_readings()[9].payload == TypedPayload("48", 48, "number")


def test_physical_snapshot_is_atomic_under_recorder_lock():
    api = Api()
    api.connect(T0)
    done = threading.Event()
    failures = []

    def publish():
        for i in range(1, 201):
            api.msg(T0 + i, "main/DHW_Target_Temp", str(i))
        done.set()

    def read():
        while not done.is_set():
            reading = api.recorder.physical_readings()[9]
            if reading.payload is not None and reading.payload.value != reading.received_at - T0:
                failures.append(reading)

    threads = [threading.Thread(target=publish), threading.Thread(target=read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert api.recorder.physical_readings()[9].payload.value == 200
