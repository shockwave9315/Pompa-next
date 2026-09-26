from dataclasses import fields, replace

import pytest

from conftest import REPO_ROOT
from pompa.capabilities import (
    Capability,
    ReferenceError,
    build_capabilities,
    capability_dict,
    effective_capabilities,
    parse_documented,
    parse_documented_extra,
    parse_observed,
    parse_optional_pcb,
    reference_dir,
)
from pompa.catalog import METRICS, RECORDED_KEYS, Source


ROOT = REPO_ROOT / "docs/reference/heishamon"
DOCUMENTED = (ROOT / "MQTT-Topics.md").read_text(encoding="utf-8")
OBSERVED = (ROOT / "realne_dane.md").read_text(encoding="utf-8")
OPTIONAL_PCB = (ROOT / "OptionalPCB.md").read_text(encoding="utf-8")
# Upstream OptionalPCB.md names these commands in its set-command table, in this order. The firmware
# also accepts SetOptPCBByte9, which that table does not name (byte 09 is documented only as "?").
PCB_NAMES = (
    "SetHeatCoolMode", "SetCompressorState", "SetSmartGridMode", "SetExternalThermostat1State",
    "SetExternalThermostat2State", "SetPoolTemp", "SetBufferTemp", "SetZ1RoomTemp",
    "SetZ2RoomTemp", "SetSolarTemp", "SetDemandControl", "SetZ2WaterTemp", "SetZ1WaterTemp",
)


def _catalog_inputs():
    documented = parse_documented(DOCUMENTED)
    return (documented, parse_observed(OBSERVED, documented), parse_optional_pcb(OPTIONAL_PCB),
            parse_documented_extra(DOCUMENTED))
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
        + [f"SET{i}" for i in range(1, 49)]
    )
    assert tuple(e.identity for e in observed) == tuple(f"XTOP{i}" for i in range(6))
    pcb = parse_optional_pcb(OPTIONAL_PCB)
    assert tuple(e.identity for e in pcb) == PCB_NAMES
    assert len(documented) == 144 + 7 + 48
    assert len(observed) == 6
    assert len(pcb) == 13
    assert all(e.provenance == "documented" for e in documented + pcb)
    assert all(e.provenance == "observed" and e.topic is None for e in observed)
    catalog = effective_capabilities()
    assert tuple(e.reference.identity for e in catalog) == tuple(
        e.identity for e in documented + pcb + observed
    )
    assert len({e.reference.identity for e in catalog}) == len(catalog) == 218
    assert catalog == build_capabilities(documented, observed, pcb=pcb,
                                         documented_xtop=parse_documented_extra(DOCUMENTED))


def test_capability_contract_has_no_key_field():
    """``identity`` is the one stable physical-capability handle; there is no ``key``."""
    assert {f.name for f in fields(Capability)} == {"reference", "metric", "source", "source_priority"}
    entries = capability_dict(effective_capabilities()[0])
    assert "key" not in entries
    assert set(entries) == {
        "identity", "family", "name", "topic", "description",
        "provenance", "readable", "canonical_metric", "source_priority",
    }


def test_reference_facts():
    entries = {e.reference.identity: e for e in effective_capabilities()}
    assert entries["TOP44"].reference.topic == "main/Error"
    assert entries["OPT3"].reference.topic == "optional/Z2_Mixing_Valve"
    assert entries["SET16"].reference.topic == "commands/SetCurves"
    assert entries["XTOP1"].reference.name == "Cool_Power_Consumption_Extra"
    assert entries["XTOP1"].reference.topic is None
    assert entries["XTOP1"].topic == "extra/Cool_Power_Consumption_Extra"
    assert entries["XTOP4"].reference.topic is None
    assert entries["XTOP4"].topic == "extra/Cool_Power_Production_Extra"
    assert entries["XTOP0"].source is METRICS[11].sources[0]
    assert all(e.topic is not None for e in entries.values())
    assert entries["SET47"].reference.name == "SetForceHeater"
    assert entries["SET47"].reference.topic == "commands/SetForceHeater"
    assert entries["SET48"].reference.name == "SetReset"
    assert entries["SET48"].reference.topic == "commands/SetReset"
    assert entries["SetDemandControl"].reference.family == "PCB"
    assert entries["SetDemandControl"].reference.topic == "commands/SetDemandControl"
    assert entries["SetDemandControl"].reference.description == (
        "Byte 14: Demand Control | from 43 -5% to 234 - 100%"
    )
    assert entries["SetZ1RoomTemp"].reference.description == (
        "Byte 10: Temp. Z1_Room (H/J series only) | Temp [C]"
    )
    assert not any(e.reference.name == "SetOptPCBByte9" for e in entries.values())


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
    # A table-like row after the table body must fail even when it does not
    # match the case-sensitive TOP/OPT/SET detector (lowercase here).
    DOCUMENTED.replace(
        "## Extra Sensor Topics:", "top6 | main/Something | description\n\n## Extra Sensor Topics:", 1
    ),
    # Any other unexpected table-like content (a "|") past the table end must
    # fail fast too, not just a stray identity-shaped row.
    DOCUMENTED + "\nnote | trailing content with a pipe",
    # A table-like row *before* the accepted header must fail too: uppercase
    # identity-like, lowercase, and arbitrary pipe-bearing content alike.
    DOCUMENTED.replace(
        "## Sensor Topics:\n\nID | Topic | Response/Description",
        "## Sensor Topics:\n\nTOP999 | main/Fake | fake row\n\nID | Topic | Response/Description",
        1,
    ),
    DOCUMENTED.replace(
        "## Sensor Topics:\n\nID | Topic | Response/Description",
        "## Sensor Topics:\n\ntop999 | main/fake | fake row\n\nID | Topic | Response/Description",
        1,
    ),
    DOCUMENTED.replace(
        "## Sensor Topics:\n\nID | Topic | Response/Description",
        "## Sensor Topics:\n\nfoo | bar | baz\n\nID | Topic | Response/Description",
        1,
    ),
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
        OBSERVED.replace("TOP6\tMain_Outlet_Temp\t", "TOP999\tMain_Outlet_Temp\t"),
        OBSERVED + "\nXTOP0\tHeat_Power_Consumption_Extra\t18\tWatt",
        OBSERVED.replace("XTOP2\tDHW_Power_Consumption_Extra\t0\tWatt\n", ""),
    ):
        with pytest.raises(ReferenceError):
            parse_observed(bad, documented)


def test_only_verified_snapshot_name_discrepancies_are_accepted():
    documented = parse_documented(DOCUMENTED)
    assert parse_observed(OBSERVED, documented)
    expected = {
        "TOP111": ("Z2_Sensor_Settings", "Z1_Sensor_Settings"),
        "TOP112": ("Z1_Sensor_Settings", "Z2_Sensor_Settings"),
        "TOP123": ("Z1_Pump_State", "Z2_Pump_State"),
        "TOP124": ("Z2_Pump_State", "Z1_Pump_State"),
    }
    documented_names = {entry.identity: entry.name for entry in documented}
    observed_names = {row.split("\t", 2)[0]: row.split("\t", 2)[1]
                      for row in OBSERVED.splitlines()[1:]}
    for identity, (documented_name, observed_name) in expected.items():
        assert documented_names[identity] == documented_name
        assert observed_names[identity] == observed_name
    with pytest.raises(ReferenceError, match="TOP111"):
        parse_observed(OBSERVED.replace("TOP111\tZ1_Sensor_Settings", "TOP111\tOther_Sensor"), documented)


def test_documented_top_does_not_require_observed_top_row():
    lines = DOCUMENTED.splitlines()
    after_last_top = next(i for i, line in enumerate(lines) if line.startswith("TOP143 |")) + 1
    lines.insert(after_last_top, "TOP144 | main/Future_Topic | A newly documented topic")
    documented = parse_documented("\n".join(lines))
    incomplete_snapshot = "\n".join(
        line for line in OBSERVED.splitlines()
        if not line.startswith(("TOP6\t", "TOP111\t"))
    )
    observed = parse_observed(incomplete_snapshot, documented)
    assert tuple(entry.identity for entry in observed) == tuple(f"XTOP{i}" for i in range(6))
    effective = {entry.reference.identity: entry for entry in build_capabilities(documented, observed)}
    assert len(effective) == len(documented) + len(observed) == 144 + 1 + 7 + 48 + 6
    assert effective["TOP6"].reference.topic == "main/Main_Outlet_Temp"
    assert effective["TOP144"].reference.topic == "main/Future_Topic"
    assert effective["TOP144"].source is None


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


@pytest.mark.parametrize("identity", ("XTOP1", "XTOP4"))
def test_verified_xtop_override_rejects_observed_name_drift(identity):
    documented = parse_documented(DOCUMENTED)
    observed = parse_observed(OBSERVED, documented)
    changed = tuple(replace(entry, name="Wrong_Name") if entry.identity == identity else entry
                    for entry in observed)
    with pytest.raises(ReferenceError, match=f"Verified XTOP topic conflicts with observation: {identity}"):
        build_capabilities(documented, changed)


def test_runtime_packaging_points_to_authoritative_docs():
    assert reference_dir().resolve() == ROOT.resolve()
    dockerfile = (REPO_ROOT / "backend/Dockerfile").read_text()
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert ("COPY docs/reference/heishamon/MQTT-Topics.md docs/reference/heishamon/OptionalPCB.md "
            "docs/reference/heishamon/realne_dane.md ./docs/reference/heishamon/") in dockerfile
    assert "dockerfile: backend/Dockerfile" in compose
    assert "context: ." in compose
    ignore = (REPO_ROOT / "backend/Dockerfile.dockerignore").read_text()
    assert [line for line in ignore.splitlines() if line and not line.startswith("#")] == [
        "**", "!backend/", "backend/**", "!backend/requirements.txt", "!backend/pompa/",
        "backend/pompa/**", "!backend/pompa/*.py", "!docs/", "docs/**",
        "!docs/reference/", "docs/reference/**", "!docs/reference/heishamon/",
        "docs/reference/heishamon/**", "!docs/reference/heishamon/MQTT-Topics.md",
        "!docs/reference/heishamon/OptionalPCB.md", "!docs/reference/heishamon/realne_dane.md",
    ]
    assert (ROOT / "MQTT-Topics.md").is_file()
    assert (ROOT / "OptionalPCB.md").is_file()
    assert (ROOT / "realne_dane.md").is_file()


def test_documented_xtop_table_agrees_with_observed_identities_and_topics():
    documented, observed, pcb, extra = _catalog_inputs()
    assert tuple((e.identity, e.name, e.topic) for e in extra) == (
        ("XTOP0", "Heat_Power_Consumption_Extra", "extra/Heat_Power_Consumption_Extra"),
        ("XTOP1", "Cool_Power_Consumption_Extra", "extra/Cool_Power_Consumption_Extra"),
        ("XTOP2", "DHW_Power_Consumption_Extra", "extra/DHW_Power_Consumption_Extra"),
        ("XTOP3", "Heat_Power_Production_Extra", "extra/Heat_Power_Production_Extra"),
        ("XTOP4", "Cool_Power_Production_Extra", "extra/Cool_Power_Production_Extra"),
        ("XTOP5", "DHW_Power_Production_Extra", "extra/DHW_Power_Production_Extra"),
    )
    # The documented table is a cross-check only: XTOP identities stay observed.
    catalog = build_capabilities(documented, observed, pcb=pcb, documented_xtop=extra)
    assert all(c.reference.provenance == "observed" for c in catalog if c.reference.family == "XTOP")
    renamed = tuple(replace(e, name="Other_Extra", topic="extra/Other_Extra") if e.identity == "XTOP1"
                    else e for e in extra)
    with pytest.raises(ReferenceError, match="XTOP1"):
        build_capabilities(documented, observed, pcb=pcb, documented_xtop=renamed)
    with pytest.raises(ReferenceError, match="identities differ"):
        build_capabilities(documented, observed, pcb=pcb, documented_xtop=extra[:5])


@pytest.mark.parametrize("bad", [
    DOCUMENTED.replace("XTOP1 | extra/Cool_Power_Consumption_Extra |", "XTOP1 | main/Cool_Power_Consumption_Extra |"),
    DOCUMENTED.replace("XTOP1 | extra/Cool_Power_Consumption_Extra |", "XTOP9 | extra/Cool_Power_Consumption_Extra |"),
    DOCUMENTED.replace("## Extra Sensor Topics:", "## Extra Topics:"),
    DOCUMENTED.replace(
        "## Option PCB Topics:", "xtop6 | extra/Something | description\n\n## Option PCB Topics:", 1
    ),
])
def test_malformed_documented_xtop_table_fails(bad):
    with pytest.raises(ReferenceError):
        parse_documented_extra(bad)


def test_pcb_rows_bytes_and_values():
    pcb = {e.identity: e for e in parse_optional_pcb(OPTIONAL_PCB)}
    assert all(e.family == "PCB" and e.name == e.identity and e.topic == f"commands/{e.identity}"
               for e in pcb.values())
    expected = {
        "SetHeatCoolMode": ("06", "0/1"), "SetCompressorState": ("06", "0/1"),
        "SetSmartGridMode": ("06", "0/1/2/3"), "SetExternalThermostat1State": ("06", "0/1/2/3"),
        "SetExternalThermostat2State": ("06", "0/1/2/3"), "SetPoolTemp": ("07", "Temp [C]"),
        "SetBufferTemp": ("08", "Temp [C]"), "SetZ1RoomTemp": ("10", "Temp [C]"),
        "SetZ2RoomTemp": ("11", "Temp [C]"), "SetSolarTemp": ("13", "Temp [C]"),
        "SetDemandControl": ("14", "from 43 -5% to 234 - 100%"),
        "SetZ2WaterTemp": ("15", "Temp [C]"), "SetZ1WaterTemp": ("16", "Temp [C]"),
    }
    for name, (byte, value) in expected.items():
        description = pcb[name].description
        assert description.startswith(f"Byte {byte}: ") and description.endswith(f" | {value}")


def test_pcb_identity_cannot_collide_with_other_families():
    documented, observed, pcb, extra = _catalog_inputs()
    others = {e.identity for e in documented + observed} | {e.name for e in documented}
    assert not {e.identity for e in pcb} & others
    renamed = (replace(pcb[0], identity="SET5"),) + pcb[1:]
    with pytest.raises(ReferenceError, match="upstream command names"):
        build_capabilities(documented, observed, pcb=renamed)
    clashing = (replace(pcb[0], topic="commands/SetHeatpump"),) + pcb[1:]
    with pytest.raises(ReferenceError, match="Duplicate topic"):
        build_capabilities(documented, observed, pcb=clashing)


@pytest.mark.parametrize("bad", [
    OPTIONAL_PCB.replace("### Set command byte decrypt:", "### Set command bytes:"),
    OPTIONAL_PCB.replace("| PCB Topic |Topic value|", "| Topic |Topic value|"),
    OPTIONAL_PCB.replace("| SetPoolTemp |Temp [C]| 07 |", "| SetPoolTemp |Temp [C]| 7 |"),
    OPTIONAL_PCB.replace("| SetPoolTemp |", "| setPoolTemp |"),
    OPTIONAL_PCB.replace("| SetPoolTemp |Temp [C]|", "| SetPoolTemp ||"),
    OPTIONAL_PCB.replace("0/1<br/>0/1<br/>0/1/2/3<br/>", "0/1<br/>0/1/2/3<br/>"),
    OPTIONAL_PCB.replace("| SetSolarTemp |", "| SetPoolTemp |"),
    OPTIONAL_PCB.replace("| SetSolarTemp |Temp [C]| 13 |", "\n| SetSolarTemp |Temp [C]| 13 |"),
    OPTIONAL_PCB.replace("| SetZ1WaterTemp |Temp [C] | 16 |", "| SetZ1WaterTemp |Temp [C] | 16 | FF |"),
])
def test_malformed_optional_pcb_reference_fails(bad):
    with pytest.raises(ReferenceError):
        parse_optional_pcb(bad)


def test_reference_refresh_changed_only_the_pinned_upstream_facts():
    """Every pre-refresh identity keeps its facts; only the six upstream warnings changed."""
    entries = {e.reference.identity: capability_dict(e) for e in effective_capabilities()}
    warnings = {"TOP15": "XTOP3", "TOP16": "XTOP0", "TOP38": "XTOP4",
                "TOP39": "XTOP1", "TOP40": "XTOP5", "TOP41": "XTOP2"}
    for identity, xtop in warnings.items():
        assert entries[identity]["description"].endswith(
            f" — invalid on heatpumps with extra data block support, see {xtop}"
        )
    assert entries["TOP56"]["description"] == "Zone1: Actual Temperature (°C)"
    assert entries["SET46"] == {
        "identity": "SET46", "family": "SET", "name": "SetHeaterOnOutdoorTemp",
        "topic": "commands/SetHeaterOnOutdoorTemp",
        "description": "Outdoor temperature for heater ON | -15 to 20",
        "provenance": "documented", "readable": False,
        "canonical_metric": None, "source_priority": None,
    }
