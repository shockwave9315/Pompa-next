from dataclasses import replace

import pytest

from conftest import REPO_ROOT
from pompa.capabilities import (
    ReferenceError,
    build_capabilities,
    effective_capabilities,
    parse_documented,
    parse_observed,
    reference_dir,
)
from pompa.catalog import METRICS, RECORDED_KEYS, Source


ROOT = REPO_ROOT / "docs/reference/heishamon"
DOCUMENTED = (ROOT / "MQTT-Topics.md").read_text(encoding="utf-8")
OBSERVED = (ROOT / "realne_dane.md").read_text(encoding="utf-8")
CORE_KEYS = (
    "main_outlet_temp", "main_inlet_temp", "main_target_temp", "dhw_tank_temp",
    "outside_temp", "water_pressure", "pump_flow", "pump_speed", "compressor_freq",
    "compressor_current", "fan1_speed", "co_power_consumption", "co_power_production",
    "dhw_power_consumption", "dhw_power_production", "heatpump_state",
    "defrosting_state", "three_way_valve", "operating_mode", "operations_counter",
    "operations_hours",
)


def test_exact_reference_coverage_and_order():
    documented = parse_documented(DOCUMENTED)
    observed = parse_observed(OBSERVED, documented)
    assert tuple(e.identity for e in documented) == tuple(
        [f"TOP{i}" for i in range(144)]
        + [f"OPT{i}" for i in range(7)]
        + [f"SET{i}" for i in range(1, 47)]
    )
    assert tuple(e.identity for e in observed) == tuple(f"XTOP{i}" for i in range(6))
    assert len(documented) == 197
    assert len(observed) == 6
    assert all(e.provenance == "documented" for e in documented)
    assert all(e.provenance == "observed" and e.topic is None for e in observed)
    catalog = effective_capabilities()
    assert tuple(e.reference.identity for e in catalog) == tuple(
        e.identity for e in documented + observed
    )
    assert len({e.reference.identity for e in catalog}) == len(catalog) == 203
    assert len({e.key for e in catalog}) == 203
    assert catalog == build_capabilities(documented, observed)


def test_reference_facts_and_generic_keys():
    entries = {e.reference.identity: e for e in effective_capabilities()}
    assert entries["TOP44"].key == "top_44"
    assert entries["OPT3"].key == "opt_3"
    assert entries["SET16"].key == "set_16"
    assert entries["XTOP1"].key == "xtop_1"
    assert entries["TOP44"].reference.topic == "main/Error"
    assert entries["OPT3"].reference.topic == "optional/Z2_Mixing_Valve"
    assert entries["SET16"].reference.topic == "commands/SetCurves"
    assert entries["XTOP1"].reference.name == "Cool_Power_Consumption_Extra"
    assert entries["XTOP1"].reference.topic is None
    assert entries["XTOP0"].source is METRICS[11].sources[0]


@pytest.mark.parametrize("bad", [
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp |", "TOP6 | |"),
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp |", "TOPx | main/Main_Outlet_Temp |"),
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp |", "| main/Main_Outlet_Temp |"),
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp |", "TOP6 main/Main_Outlet_Temp |"),
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp |", "TOP6 | main/Main_Outlet_Temp |\nTOP6 | main/Main_Outlet_Temp |"),
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp |", "TOP6 | main/Pump_Flow |"),
    DOCUMENTED.replace("TOP7 | main/Main_Target_Temp |", "TOP7 | main/Main_Outlet_Temp |"),
    DOCUMENTED.replace("SET9  | SetOperationMode |", "SET9  | |"),
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp |", "\nTOP6 | main/Main_Outlet_Temp |"),
    DOCUMENTED.replace("TOP6 | main/Main_Outlet_Temp | Main outlet water temperature (°C)\n", ""),
    DOCUMENTED.replace("ID | Topic | Response/Description", "ID | Path | Response/Description", 1),
    DOCUMENTED.replace("## Command Topics:", "## Commands:"),
])
def test_malformed_or_duplicate_documented_rows_fail(bad):
    with pytest.raises(ReferenceError):
        parse_documented(bad)


def test_malformed_duplicate_or_conflicting_observation_fails():
    documented = parse_documented(DOCUMENTED)
    for bad in (
        OBSERVED.replace("XTOP0\tHeat_Power_Consumption_Extra\t", "XTOPx\tHeat_Power_Consumption_Extra\t"),
        OBSERVED.replace("XTOP0\tHeat_Power_Consumption_Extra\t", "\tHeat_Power_Consumption_Extra\t"),
        OBSERVED.replace("TOP6\tMain_Outlet_Temp\t", "TOP6\tWrong_Name\t"),
        OBSERVED + "\nXTOP0\tHeat_Power_Consumption_Extra\t18\tWatt",
        OBSERVED.replace("XTOP2\tDHW_Power_Consumption_Extra\t0\tWatt\n", ""),
    ):
        with pytest.raises(ReferenceError):
            parse_observed(bad, documented)


def test_only_verified_snapshot_name_discrepancies_are_accepted():
    documented = parse_documented(DOCUMENTED)
    assert parse_observed(OBSERVED, documented)
    with pytest.raises(ReferenceError, match="TOP111"):
        parse_observed(OBSERVED.replace("TOP111\tZ1_Sensor_Settings", "TOP111\tOther_Sensor"), documented)


def test_core_association_and_priority_are_unchanged():
    assert tuple(m.key for m in METRICS) == CORE_KEYS
    assert RECORDED_KEYS == CORE_KEYS
    entries = {e.reference.identity: e for e in effective_capabilities()}
    associated = [e for e in entries.values() if e.metric is not None]
    assert len(associated) == sum(len(m.sources) for m in METRICS) == 25
    for metric in METRICS:
        for priority, source in enumerate(metric.sources):
            capability = entries[source.id]
            assert capability.metric is metric
            assert capability.source is source
            assert capability.source_priority == priority
            if priority == 0:
                assert capability.key == metric.key
            else:
                assert capability.key == f"top_{int(source.id.removeprefix('TOP'))}"
            if source.id.startswith("TOP"):
                assert capability.reference.topic == source.topic
    assert tuple(s.id for s in METRICS[11].sources) == ("XTOP0", "TOP16")
    assert tuple(s.id for s in METRICS[12].sources) == ("XTOP3", "TOP15")
    assert tuple(s.id for s in METRICS[13].sources) == ("XTOP2", "TOP41")
    assert tuple(s.id for s in METRICS[14].sources) == ("XTOP5", "TOP40")


def test_core_topic_conflicts_fail():
    documented = parse_documented(DOCUMENTED)
    observed = parse_observed(OBSERVED, documented)
    wrong_top = replace(METRICS[0], sources=(Source("TOP6", "main/Wrong_Name"),))
    with pytest.raises(ReferenceError, match="Core topic conflicts"):
        build_capabilities(documented, observed, (wrong_top,))
    wrong_xtop = replace(METRICS[11], sources=(Source("XTOP0", "extra/Wrong_Name"),))
    with pytest.raises(ReferenceError, match="Core XTOP name conflicts"):
        build_capabilities(documented, observed, (wrong_xtop,))


def test_runtime_packaging_points_to_authoritative_docs():
    assert reference_dir().resolve() == ROOT.resolve()
    dockerfile = (REPO_ROOT / "backend/Dockerfile").read_text()
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "COPY docs/reference/heishamon/MQTT-Topics.md docs/reference/heishamon/realne_dane.md ./docs/reference/heishamon/" in dockerfile
    assert "dockerfile: backend/Dockerfile" in compose
    assert "context: ." in compose
