"""Stage 4E-B pure control domain (docs/ARCHITECTURE.md §25.5).

Expected values are literals transcribed from the pinned HeishaMon firmware and documentation
(``heishamon/HeishaMon`` @ ``0de4f3c02598f542e7859a772e627e5c1ebc2ce3``), never computed from
``pompa.control``. Payload expectations follow ``HeishaMon/commands.cpp`` (``toInt()``/``toFloat()``
parsing); readback expectations follow ``HeishaMon/decode.h``/``decode.cpp`` and ``MQTT-Topics.md``.
"""

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pompa import control
from pompa.capabilities import effective_capabilities
from pompa.control import ABSENT, ControlError, ReadingFact


# Firmware command tables (commands.h ``commands[]`` / ``optionalCommands[]``), in firmware order.
FIRMWARE_COMMANDS = (
    "SetHeatpump", "SetPump", "SetMaxPumpDuty", "SetQuietMode", "SetZ1HeatRequestTemperature",
    "SetZ1CoolRequestTemperature", "SetZ2HeatRequestTemperature", "SetZ2CoolRequestTemperature",
    "SetForceDHW", "SetForceDefrost", "SetForceSterilization", "SetForceHeater", "SetHolidayMode",
    "SetPowerfulMode", "SetOperationMode", "SetDHWTemp", "SetCurves", "SetZones",
    "SetFloorHeatDelta", "SetFloorCoolDelta", "SetDHWHeatDelta", "SetReset", "SetHeaterDelayTime",
    "SetHeaterStartDelta", "SetHeaterStopDelta", "SetMainSchedule", "SetAltExternalSensor",
    "SetExternalPadHeater", "SetBufferDelta", "SetBuffer", "SetHeatingOffOutdoorTemp",
    "SetExternalControl", "SetExternalError", "SetExternalCompressorControl",
    "SetExternalHeatCoolControl", "SetBivalentControl", "SetBivalentMode", "SetBivalentStartTemp",
    "SetBivalentAPStartTemp", "SetBivalentAPStopTemp", "SetHeatingControl", "SetSmartDHW",
    "SetQuietModePriority", "SetPumpFlowrateMode", "SetDHWSensorSelection", "SetDHWHeaterState",
    "SetRoomHeaterState", "SetHeaterOnOutdoorTemp",
)
FIRMWARE_OPTIONAL_COMMANDS = (
    "SetHeatCoolMode", "SetCompressorState", "SetSmartGridMode", "SetExternalThermostat1State",
    "SetExternalThermostat2State", "SetDemandControl", "SetPoolTemp", "SetBufferTemp",
    "SetZ1RoomTemp", "SetZ1WaterTemp", "SetZ2RoomTemp", "SetZ2WaterTemp", "SetSolarTemp",
    "SetOptPCBByte9",
)


def live(raw):
    return ReadingFact(str(raw), "live", True)


def retained(raw):
    return ReadingFact(str(raw), "retained", False)


def stale(raw):
    return ReadingFact(str(raw), "live", False)


ABSENT_FACT = ReadingFact(None, "none", False)

# Facts under which every control is executable and every request temperature is in shift mode.
OK = {"TOP76": live(0), "TOP81": live(0), "TOP4": live(4), "TOP110": live(1), "TOP122": live(1)}
DIRECT = {**OK, "TOP76": live(1), "TOP81": live(1)}


def prep(key, value=ABSENT, facts=OK):
    return control.prepare(key, value, facts)


def refused(code, key, value=ABSENT, facts=OK):
    with pytest.raises(ControlError) as info:
        control.prepare(key, value, facts)
    assert info.value.code == code, info.value.detail
    return info.value


# ------------------------------------------------------------------- definition completeness


def test_counts_and_unique_keys():
    defs = control.definitions()
    assert len(control.CONTROLS) == len(defs) == 63
    assert sum(c.family == "heat_pump" for c in defs.values()) == 51
    assert sum(c.family == "optional_pcb" for c in defs.values()) == 12
    assert len({c.key for c in control.CONTROLS}) == 63
    assert len(FIRMWARE_COMMANDS) + len(FIRMWARE_OPTIONAL_COMMANDS) == 62


def test_every_firmware_command_is_defined_or_explicitly_excluded():
    catalog = effective_capabilities()
    set_names = {c.reference.name for c in catalog if c.reference.family == "SET"}
    pcb_names = {c.reference.name for c in catalog if c.reference.family == "PCB"}
    assert set_names == set(FIRMWARE_COMMANDS)
    assert pcb_names | set(control.EXCLUDED_COMMANDS) == set(FIRMWARE_OPTIONAL_COMMANDS)
    assert set(control.EXCLUDED_COMMANDS) == {"SetOptPCBByte9", "SetHeatCoolMode"}
    defined = {c.command for c in control.CONTROLS}
    assert defined == (set(FIRMWARE_COMMANDS) | set(FIRMWARE_OPTIONAL_COMMANDS)) - {
        "SetOptPCBByte9", "SetHeatCoolMode",
    }
    assert not any("Byte9" in c.command or "Byte9" in c.key for c in control.CONTROLS)


def test_heat_cool_switch_is_a_known_capability_but_not_a_control():
    # OptionalPCB.md byte 06 "1st bit = Heat/Cool" documents no polarity: neither payload has a
    # semantic meaning, so the catalog keeps the command and no control can publish it.
    capability = {c.reference.identity: c for c in effective_capabilities()}["SetHeatCoolMode"]
    assert capability.reference.family == "PCB" and not capability.readable
    assert not any(c.command == "SetHeatCoolMode" or "heat_cool_switch" in c.key
                   for c in control.CONTROLS)
    for value in (True, False, "heat", "cool"):
        refused("unknown_control", "pcb_heat_cool_switch", value)
    by_key = {c.key: c for c in control.CONTROLS}
    revived = control.CONTROLS + (
        replace(by_key["pcb_compressor_switch"], key="pcb_heat_cool_switch", identity="SetHeatCoolMode",
                command="SetHeatCoolMode"),
    )
    with pytest.raises(ValueError, match="Excluded commands are defined"):
        control.check_definitions(revived, effective_capabilities())


def test_set_curves_is_exactly_four_controls_and_other_commands_are_one():
    by_command = {}
    for c in control.CONTROLS:
        by_command.setdefault(c.command, []).append(c.key)
    assert sorted(by_command.pop("SetCurves")) == [
        "zone1_cool_curve", "zone1_heat_curve", "zone2_cool_curve", "zone2_heat_curve",
    ]
    assert all(len(keys) == 1 for keys in by_command.values())


def test_identities_follow_the_catalog():
    catalog = {c.reference.identity: c for c in effective_capabilities()}
    for c in control.CONTROLS:
        reference = catalog[c.identity].reference
        assert reference.name == c.command
        assert reference.family == {"heat_pump": "SET", "optional_pcb": "PCB"}[c.family]
    assert control.definitions()["bivalent_advanced_start_temperature"].command == "SetBivalentAPStartTemp"
    assert control.definitions()["bivalent_advanced_stop_temperature"].command == "SetBivalentAPStopTemp"
    assert not any("BivalentA" in c.command and "AP" not in c.command for c in control.CONTROLS)


def test_check_definitions_rejects_broken_definitions():
    capabilities = effective_capabilities()
    by_key = {c.key: c for c in control.CONTROLS}

    def broken(key, **changes):
        return tuple(replace(c, **changes) if c.key == key else c for c in control.CONTROLS)

    cases = [
        (control.CONTROLS + (by_key["heat_delta"],), "Duplicate control key"),
        (broken("bivalent_advanced_start_temperature", command="SetBivalentAStartTemp"), "is not"),
        (broken("pcb_smart_grid_mode", readback=control._state("TOP4")), "no readback"),
        (broken("heat_delta", identity="SetPoolTemp"), "family mismatch"),
        (broken("force_defrost", klass="setting"), "trigger"),
        (broken("heat_delta", readback=control._state("SET5")), "not a readable"),
        (tuple(c for c in control.CONTROLS if c.key != "fault_reset"), "Undefined commands"),
    ]
    for controls, message in cases:
        with pytest.raises(ValueError, match=message):
            control.check_definitions(controls, capabilities)


def test_control_module_is_pure():
    source = Path(control.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"__future__", "json", "math", "collections.abc", "dataclasses", "decimal",
                        "functools", "typing", "pompa.capabilities"}
    for forbidden in ("recorder", "storage", "mqtt", "paho", "fastapi", "api", "publish"):
        assert not any(forbidden in (name or "").split(".")[-1] for name in imported)


# ------------------------------------------------------------------------- golden encodings

# (key, semantic value, facts or None for OK, exact MQTT payload)
GOLDEN = [
    ("heat_pump_power", True, None, "1"), ("heat_pump_power", False, None, "0"),
    ("holiday_mode", True, None, "1"), ("holiday_mode", False, None, "0"),
    ("quiet_mode", "off", None, "0"), ("quiet_mode", "level_1", None, "1"),
    ("quiet_mode", "level_2", None, "2"), ("quiet_mode", "level_3", None, "3"),
    ("powerful_mode", "off", None, "0"), ("powerful_mode", "min_30", None, "1"),
    ("powerful_mode", "min_60", None, "2"), ("powerful_mode", "min_90", None, "3"),
    ("zone1_heat_request", -5, None, "-5"), ("zone1_heat_request", 5, None, "5"),
    ("zone1_heat_request", 20, DIRECT, "20"), ("zone1_heat_request", 127, DIRECT, "127"),
    ("zone1_cool_request", -5, None, "-5"), ("zone1_cool_request", 5, None, "5"),
    ("zone1_cool_request", 5, DIRECT, "5"), ("zone1_cool_request", 20, DIRECT, "20"),
    ("zone2_heat_request", 0, None, "0"), ("zone2_heat_request", 35, DIRECT, "35"),
    ("zone2_cool_request", -1, None, "-1"), ("zone2_cool_request", 18, DIRECT, "18"),
    ("operation_mode", "heat", None, "0"), ("operation_mode", "cool", None, "1"),
    ("operation_mode", "auto", None, "2"), ("operation_mode", "dhw", None, "3"),
    ("operation_mode", "heat_dhw", None, "4"), ("operation_mode", "cool_dhw", None, "5"),
    ("operation_mode", "auto_dhw", None, "6"),
    ("force_dhw", True, None, "1"), ("force_dhw", False, None, "0"),
    ("dhw_target_temperature", 40, None, "40"), ("dhw_target_temperature", 75, None, "75"),
    ("force_defrost", ABSENT, None, "1"), ("force_sterilization", ABSENT, None, "1"),
    ("pump_service_mode", True, None, "1"), ("pump_service_mode", False, None, "0"),
    ("max_pump_duty", 64, None, "64"), ("max_pump_duty", 254, None, "254"),
    ("active_zones", "zone1", None, "0"), ("active_zones", "zone2", None, "1"),
    ("active_zones", "zone1_zone2", None, "2"),
    ("heat_delta", 1, None, "1"), ("heat_delta", 15, None, "15"),
    ("cool_delta", 1, None, "1"), ("cool_delta", 15, None, "15"),
    ("dhw_heat_delta", -12, None, "-12"), ("dhw_heat_delta", -2, None, "-2"),
    ("heater_delay_time", 0, None, "0"), ("heater_delay_time", 254, None, "254"),
    ("heater_start_delta", -127, None, "-127"), ("heater_start_delta", 127, None, "127"),
    ("heater_stop_delta", -127, None, "-127"), ("heater_stop_delta", 127, None, "127"),
    ("main_schedule", True, None, "1"), ("main_schedule", False, None, "0"),
    ("alt_external_sensor", True, None, "1"), ("alt_external_sensor", False, None, "0"),
    ("external_pad_heater", "disabled", None, "0"), ("external_pad_heater", "type_a", None, "1"),
    ("external_pad_heater", "type_b", None, "2"),
    ("buffer_delta", 0, None, "0"), ("buffer_delta", 10, None, "10"),
    ("buffer_installed", True, None, "1"), ("buffer_installed", False, None, "0"),
    ("heating_off_outdoor_temperature", 5, None, "5"), ("heating_off_outdoor_temperature", 35, None, "35"),
    ("external_control", True, None, "1"), ("external_control", False, None, "0"),
    ("external_error_signal", True, None, "1"), ("external_error_signal", False, None, "0"),
    ("external_compressor_control", True, None, "1"), ("external_compressor_control", False, None, "0"),
    ("external_heat_cool_control", True, None, "1"), ("external_heat_cool_control", False, None, "0"),
    ("bivalent_control", True, None, "1"), ("bivalent_control", False, None, "0"),
    ("bivalent_mode", "alternative", None, "0"), ("bivalent_mode", "parallel", None, "1"),
    ("bivalent_mode", "advanced_parallel", None, "2"),
    ("bivalent_start_temperature", -15, None, "-15"), ("bivalent_start_temperature", 35, None, "35"),
    ("bivalent_advanced_start_temperature", -15, None, "-15"),
    ("bivalent_advanced_start_temperature", 35, None, "35"),
    ("bivalent_advanced_stop_temperature", -15, None, "-15"),
    ("bivalent_advanced_stop_temperature", 35, None, "35"),
    ("heating_control", "comfort", None, "0"), ("heating_control", "efficiency", None, "1"),
    ("smart_dhw", "variable", None, "0"), ("smart_dhw", "standard", None, "1"),
    ("quiet_mode_priority", "sound", None, "0"), ("quiet_mode_priority", "capacity", None, "1"),
    ("pump_flowrate_mode", "delta_t", None, "0"), ("pump_flowrate_mode", "max_duty", None, "1"),
    ("dhw_sensor_selection", "top", None, "0"), ("dhw_sensor_selection", "center", None, "1"),
    ("dhw_heater_allowed", "blocked", None, "0"), ("dhw_heater_allowed", "free", None, "1"),
    ("room_heater_allowed", "blocked", None, "0"), ("room_heater_allowed", "free", None, "1"),
    ("heater_on_outdoor_temperature", -15, None, "-15"), ("heater_on_outdoor_temperature", 20, None, "20"),
    ("force_heater", True, None, "1"), ("force_heater", False, None, "0"),
    ("fault_reset", ABSENT, None, "1"),
    ("zone1_heat_curve", {"target_high": 32}, None, '{"zone1":{"heat":{"target":{"high":32}}}}'),
    ("zone2_cool_curve", {"outside_low": -127}, None, '{"zone2":{"cool":{"outside":{"low":-127}}}}'),
    ("pcb_compressor_switch", True, None, "1"), ("pcb_compressor_switch", False, None, "0"),
    ("pcb_smart_grid_mode", "normal", None, "0"), ("pcb_smart_grid_mode", "capacity_1", None, "1"),
    ("pcb_smart_grid_mode", "hp_dhw_off", None, "2"), ("pcb_smart_grid_mode", "capacity_2", None, "3"),
    ("pcb_thermostat1_demand", "none", None, "0"), ("pcb_thermostat1_demand", "cool", None, "1"),
    ("pcb_thermostat1_demand", "heat", None, "2"), ("pcb_thermostat1_demand", "heat_cool", None, "3"),
    ("pcb_thermostat2_demand", "none", None, "0"), ("pcb_thermostat2_demand", "cool", None, "1"),
    ("pcb_thermostat2_demand", "heat", None, "2"), ("pcb_thermostat2_demand", "heat_cool", None, "3"),
    # OptionalPCB.md byte 14: 2B=5 %, 52=25 %, 85=50 %, B8=75 %, EB=100 % (decimal payload).
    ("pcb_demand_control", 5, None, "43"), ("pcb_demand_control", 25, None, "82"),
    ("pcb_demand_control", 50, None, "133"), ("pcb_demand_control", 75, None, "184"),
    ("pcb_demand_control", 100, None, "235"),
    ("pcb_pool_temperature", 21.5, None, "21.5"), ("pcb_pool_temperature", -78, None, "-78"),
    ("pcb_buffer_temperature", 120, None, "120"), ("pcb_buffer_temperature", 0.0, None, "0"),
    ("pcb_zone1_room_temperature", 20.25, None, "20.25"),
    ("pcb_zone1_water_temperature", 35, None, "35"),
    ("pcb_zone2_room_temperature", -0.5, None, "-0.5"),
    ("pcb_zone2_water_temperature", 1e-05, None, "0.00001"),
    ("pcb_solar_temperature", 60.125, None, "60.125"),
]


@pytest.mark.parametrize("key,value,facts,payload", GOLDEN)
def test_golden_payload(key, value, facts, payload):
    prepared = prep(key, value, facts or OK)
    assert prepared.payload == payload


def test_golden_table_covers_every_control_and_every_enum_value():
    covered = {}
    for key, value, _facts, _payload in GOLDEN:
        covered.setdefault(key, set()).add(value if not isinstance(value, dict) else "curve")
    assert set(covered) >= set(control.definitions()) - {
        "zone1_cool_curve", "zone2_heat_curve",  # covered by the curve tests below
    }
    for c in control.CONTROLS:
        if isinstance(c.value, control.EnumValue):
            assert covered[c.key] == {name for name, _ in c.value.options}, c.key
        if isinstance(c.value, control.BooleanValue):
            assert covered[c.key] == {True, False}, c.key


def test_topics_come_from_the_catalog():
    assert prep("dhw_target_temperature", 50).topic == "commands/SetDHWTemp"
    # SET37/SET38: the firmware names; Home Assistant's SetBivalentAStartTemp/StopTemp never arrive.
    assert prep("bivalent_advanced_start_temperature", 0).topic == "commands/SetBivalentAPStartTemp"
    assert prep("bivalent_advanced_stop_temperature", 0).topic == "commands/SetBivalentAPStopTemp"
    assert prep("zone2_cool_curve", {"target_low": 10}).topic == "commands/SetCurves"
    assert prep("pcb_smart_grid_mode", "normal").topic == "commands/SetSmartGridMode"
    assert prep("fault_reset").topic == "commands/SetReset"


# ------------------------------------------------------------------------ numeric boundaries

INTEGER_CONTROLS = [c for c in control.CONTROLS if isinstance(c.value, control.IntegerValue)]
EXPECTED_INTEGER_RANGES = {
    "dhw_target_temperature": (40, 75, "°C", "documented"),
    "max_pump_duty": (64, 254, "duty", "documented"),
    "heat_delta": (1, 15, "K", "documented"),
    "cool_delta": (1, 15, "K", "documented"),
    "dhw_heat_delta": (-12, -2, "K", "documented"),
    "heater_delay_time": (0, 254, "min", "protocol"),
    "heater_start_delta": (-127, 127, "K", "protocol"),
    "heater_stop_delta": (-127, 127, "K", "protocol"),
    "buffer_delta": (0, 10, "K", "documented"),
    "heating_off_outdoor_temperature": (5, 35, "°C", "documented"),
    "bivalent_start_temperature": (-15, 35, "°C", "documented"),
    "bivalent_advanced_start_temperature": (-15, 35, "°C", "documented"),
    "bivalent_advanced_stop_temperature": (-15, 35, "°C", "documented"),
    "heater_on_outdoor_temperature": (-15, 20, "°C", "documented"),
}


def test_integer_ranges_are_exactly_the_evidenced_ones():
    assert {c.key: (c.value.minimum, c.value.maximum, c.value.unit, c.value.basis)
            for c in INTEGER_CONTROLS} == EXPECTED_INTEGER_RANGES


@pytest.mark.parametrize("definition", INTEGER_CONTROLS, ids=lambda c: c.key)
def test_integer_boundaries_and_types(definition):
    low, high = definition.value.minimum, definition.value.maximum
    assert prep(definition.key, low).payload == str(low)
    assert prep(definition.key, high).payload == str(high)
    for bad in (low - 1, high + 1, float(low), low + 0.5, True, False, str(low), None, [low], {"v": low}):
        refused("invalid_value", definition.key, bad)


def test_bool_is_not_an_integer_and_integer_is_not_a_bool():
    refused("invalid_value", "heat_delta", True)  # True == 1 is in range, but it is not an integer
    refused("invalid_value", "buffer_delta", False)
    refused("invalid_value", "heat_pump_power", 1)
    refused("invalid_value", "heat_pump_power", 0)
    refused("invalid_value", "heat_pump_power", "1")
    refused("invalid_value", "pcb_demand_control", True)


@pytest.mark.parametrize("key", [c.key for c in control.CONTROLS
                                 if isinstance(c.value, control.NumberValue)])
def test_pcb_temperature_boundaries(key):
    assert prep(key, -78).payload == "-78"
    assert prep(key, 120).payload == "120"
    for bad in (-78.01, 120.5, float("nan"), float("inf"), float("-inf"), True, "21", None,
                10**400, -10**400):  # a huge int must be refused, not overflow a float conversion
        refused("invalid_value", key, bad)


def test_demand_control_accepts_only_the_documented_points():
    for bad in (0, 4, 6, 10, 99, 101, 43, 235, 50.0, "50"):
        refused("invalid_value", "pcb_demand_control", bad)


# ------------------------------------------------------------------------------------ enums

@pytest.mark.parametrize("key,bad", [
    ("quiet_mode", "scheduled"), ("quiet_mode", "Scheduled"), ("quiet_mode", 4), ("quiet_mode", "4"),
    ("quiet_mode", "OFF"), ("quiet_mode", ""), ("quiet_mode", None), ("quiet_mode", 0),
    ("operation_mode", "auto_cool"), ("operation_mode", "auto_heat"), ("operation_mode", 7),
    ("operation_mode", "7"), ("operation_mode", "Heat only"),
    ("powerful_mode", "30 min"), ("active_zones", "both"), ("external_pad_heater", "type-A"),
    ("bivalent_mode", "Advanced Parallel"), ("pcb_smart_grid_mode", 4), ("pcb_smart_grid_mode", "4"),
    ("pcb_thermostat1_demand", "both"), ("dhw_heater_allowed", True),
])
def test_unknown_enum_values_are_rejected(key, bad):
    refused("invalid_value", key, bad)


# ---------------------------------------------------------------------------------- triggers

@pytest.mark.parametrize("key", ["force_defrost", "force_sterilization", "fault_reset"])
def test_trigger_shape(key):
    prepared = prep(key)
    assert prepared.payload == "1" and prepared.requested is None
    for value in (True, False, 0, 1, None, "1"):
        refused("invalid_request", key, value)  # no "off": firmware encodes 0 as "no change"


def test_non_trigger_requires_a_value():
    for c in control.CONTROLS:
        if c.klass != "trigger":
            refused("invalid_request", c.key)


def test_unknown_control():
    refused("unknown_control", "SetDHWTemp", 50)
    refused("unknown_control", "SET11", 50)
    refused("unknown_control", "set_opt_pcb_byte9", 1)


# ------------------------------------------------------------------------------------ curves

CURVE_KEYS = ("zone1_heat_curve", "zone1_cool_curve", "zone2_heat_curve", "zone2_cool_curve")
# Protocol JSON path of each semantic field (MQTT-Topics.md SET16 structure).
CURVE_PATHS = {"target_high": ("target", "high"), "target_low": ("target", "low"),
               "outside_high": ("outside", "high"), "outside_low": ("outside", "low")}


@pytest.mark.parametrize("key", CURVE_KEYS)
@pytest.mark.parametrize("field", list(CURVE_PATHS))
def test_every_curve_field_encodes_only_itself(key, field):
    zone, mode = key.split("_")[0], key.split("_")[1]
    location, point = CURVE_PATHS[field]
    for value in (-127, 127, 0):
        payload = prep(key, {field: value}).payload
        assert payload == f'{{"{zone}":{{"{mode}":{{"{location}":{{"{point}":{value}}}}}}}}}'


def test_multi_field_curve_update_is_deterministic():
    payload = prep("zone1_heat_curve", {"outside_low": -15, "target_low": 23, "target_high": 32}).payload
    assert payload == '{"zone1":{"heat":{"target":{"high":32,"low":23},"outside":{"low":-15}}}}'
    full = prep("zone2_cool_curve", {"outside_low": 20, "outside_high": 30,
                                     "target_low": 10, "target_high": 15}).payload
    assert json.loads(full) == {"zone2": {"cool": {"target": {"high": 15, "low": 10},
                                                   "outside": {"high": 30, "low": 20}}}}
    assert full == '{"zone2":{"cool":{"target":{"high":15,"low":10},"outside":{"high":30,"low":20}}}}'


@pytest.mark.parametrize("bad", [
    {}, {"target": 30}, {"high": 30}, {"target_high": 30, "zone": 1}, {"TARGET_HIGH": 30},
    {"target_high": -128}, {"target_high": 128}, {"target_high": 30.0}, {"target_high": True},
    {"target_high": "30"}, {"target_high": None},
    '{"zone1":{"heat":{"target":{"high":30}}}}', {"zone1": {"heat": {"target": {"high": 30}}}},
    [("target_high", 30)], 30, None,
])
def test_invalid_curve_requests(bad):
    refused("invalid_value", "zone1_heat_curve", bad)


def test_curves_enforce_no_cross_field_invariant():
    # No authoritative invariant exists upstream; "unsensible" curves are protocol-valid.
    assert prep("zone1_heat_curve", {"outside_low": 20, "outside_high": -20,
                                     "target_low": 60, "target_high": 20}).payload


# ------------------------------------------------------------------------ request temperatures

@pytest.mark.parametrize("key,context,direct", [
    ("zone1_heat_request", "TOP76", (20, 127)), ("zone2_heat_request", "TOP76", (20, 127)),
    ("zone1_cool_request", "TOP81", (5, 20)), ("zone2_cool_request", "TOP81", (5, 20)),
])
def test_request_temperature_modes(key, context, direct):
    shift = {**OK, context: live(0)}
    assert prep(key, -5, shift).payload == "-5" and prep(key, 5, shift).payload == "5"
    for bad in (-6, 6, direct[1], 2.0, True):  # cool direct 5 is also a valid +5 shift
        refused("invalid_value", key, bad, shift)
    direct_facts = {**OK, context: live(1)}
    assert prep(key, direct[0], direct_facts).payload == str(direct[0])
    assert prep(key, direct[1], direct_facts).payload == str(direct[1])
    for bad in (direct[0] - 1, direct[1] + 1, -5 if direct[0] > -5 else 0):
        refused("invalid_value", key, bad, direct_facts)
    unavailable = [
        {k: v for k, v in OK.items() if k != context},
        {**OK, context: retained(0)}, {**OK, context: stale(0)}, {**OK, context: ABSENT_FACT},
        {**OK, context: live(2)}, {**OK, context: live(-1)}, {**OK, context: live("1.0")},
        {**OK, context: live("direct")},
    ]
    for facts in unavailable:
        refused("validation_context_unavailable", key, 0, facts)


def test_request_temperature_schema_reports_active_range():
    c = control.definitions()["zone1_heat_request"]
    assert control.schema(c, OK)["active"] == "shift"
    assert control.schema(c, DIRECT)["active"] == "direct"
    assert control.schema(c, {})["active"] is None
    assert control.schema(c, OK)["ranges"] == {
        "shift": {"min": -5, "max": 5, "unit": "K", "range_basis": "documented"},
        "direct": {"min": 20, "max": 127, "unit": "°C", "range_basis": "protocol"},
    }


# ------------------------------------------------------------------------------ prerequisites

def results(prepared):
    return {p.id: p.satisfied for p in prepared.prerequisites}


def test_force_dhw_operation_mode_prerequisite():
    for mode in (3, 4, 5, 6, 8):
        assert results(prep("force_dhw", True, {"TOP4": live(mode)})) == {"dhw_operation_mode": True}
    for mode in (0, 1, 2, 7):
        refused("prerequisite_not_met", "force_dhw", True, {"TOP4": live(mode)})
    # Only a live documented TOP4 state (0..8) proves the prerequisite false.
    for fact in (retained(0), stale(0), ABSENT_FACT, live("x"), live(-1), live("3.0"), live(9), live(-2)):
        assert results(prep("force_dhw", True, {"TOP4": fact})) == {"dhw_operation_mode": None}
    assert results(prep("force_dhw", True, {})) == {"dhw_operation_mode": None}


def test_optional_pcb_prerequisites():
    enabled = {"TOP110": live(1)}
    assert results(prep("pcb_smart_grid_mode", "normal", enabled)) == {
        "heat_pump_optional_pcb": True, "heishamon_optional_pcb_emulation": None,
    }
    refused("prerequisite_not_met", "pcb_smart_grid_mode", "normal", {"TOP110": live(0)})
    # TOP110/TOP122 are documented 0/1 settings; their 2-bit field can also decode an undocumented 2.
    for fact in (retained(0), stale(0), ABSENT_FACT, live(-1), live(2)):
        assert results(prep("pcb_pool_temperature", 20, {"TOP110": fact}))["heat_pump_optional_pcb"] is None
    refused("prerequisite_not_met", "pcb_compressor_switch", True, {"TOP110": live(1), "TOP122": live(0)})
    for fact in (live(-1), live(2), retained(0)):
        assert results(prep("pcb_compressor_switch", True, {"TOP110": live(1), "TOP122": fact}))[
            "external_compressor_control"] is None
    assert results(prep("pcb_compressor_switch", True, {"TOP110": live(1), "TOP122": live(1)})) == {
        "heat_pump_optional_pcb": True, "heishamon_optional_pcb_emulation": None,
        "external_compressor_control": True,
    }
    for c in control.CONTROLS:
        if c.family == "optional_pcb":
            assert [p.id for p in c.prerequisites][:2] == [
                "heat_pump_optional_pcb", "heishamon_optional_pcb_emulation",
            ]


def test_value_errors_precede_prerequisite_errors():
    refused("invalid_value", "force_dhw", 1, {"TOP4": live(0)})
    refused("invalid_value", "pcb_smart_grid_mode", "off", {"TOP110": live(0)})


def test_restrictions_are_reported_not_enforced():
    defs = control.definitions()
    assert defs["dhw_sensor_selection"].restrictions == ("documented_all_in_one_only",)
    assert defs["force_heater"].restrictions == ("firmware_min_4_2_0",)
    for key in ("pcb_thermostat1_demand", "pcb_buffer_temperature", "pcb_zone1_room_temperature"):
        assert defs[key].restrictions == ("documented_h_j_series_only",)
    for key in ("heater_delay_time", "heater_start_delta", "heater_stop_delta", "pump_flowrate_mode"):
        assert defs[key].restrictions == ()  # upstream docs disagree (J-only vs J/K/L)
    assert prep("dhw_sensor_selection", "center", {}).payload == "1"
    assert prep("pcb_thermostat1_demand", "heat", {}).payload == "2"
    assert sum(bool(c.restrictions) for c in control.CONTROLS) == 5


def test_service_marker():
    assert {c.key for c in control.CONTROLS if c.service} == {
        "force_defrost", "force_sterilization", "pump_service_mode", "max_pump_duty",
        "force_heater", "fault_reset",
    }


def test_classes():
    by_class = {}
    for c in control.CONTROLS:
        by_class.setdefault(c.klass, set()).add(c.key)
    assert by_class["temporary"] == {"powerful_mode", "force_dhw"}
    assert by_class["trigger"] == {"force_defrost", "force_sterilization", "fault_reset"}
    assert by_class["curve"] == set(CURVE_KEYS)
    assert len(by_class["pcb_input"]) == 12
    assert len(by_class["setting"]) == 63 - 2 - 3 - 4 - 12


# -------------------------------------------------------------------------------- readback

# (key, readback identity) for every state readback. For each pair the firmware writes and
# decodes the same protocol byte (commands.cpp encoder address == decode.h topicBytes entry).
STATE_READBACK = {
    "heat_pump_power": "TOP0", "holiday_mode": "TOP19", "quiet_mode": "TOP18",
    "powerful_mode": "TOP17", "zone1_heat_request": "TOP27", "zone1_cool_request": "TOP28",
    "zone2_heat_request": "TOP34", "zone2_cool_request": "TOP35", "operation_mode": "TOP4",
    "force_dhw": "TOP2", "dhw_target_temperature": "TOP9", "max_pump_duty": "TOP95",
    "active_zones": "TOP94", "heat_delta": "TOP23", "cool_delta": "TOP24",
    "dhw_heat_delta": "TOP22", "heater_delay_time": "TOP96", "heater_start_delta": "TOP97",
    "heater_stop_delta": "TOP98", "main_schedule": "TOP13", "alt_external_sensor": "TOP108",
    "external_pad_heater": "TOP114", "buffer_delta": "TOP113", "buffer_installed": "TOP99",
    "heating_off_outdoor_temperature": "TOP77", "external_control": "TOP119",
    "external_error_signal": "TOP121", "external_compressor_control": "TOP122",
    "external_heat_cool_control": "TOP120", "bivalent_control": "TOP129",
    "bivalent_mode": "TOP130", "bivalent_start_temperature": "TOP131",
    "bivalent_advanced_start_temperature": "TOP134", "bivalent_advanced_stop_temperature": "TOP135",
    "heating_control": "TOP139", "smart_dhw": "TOP140", "quiet_mode_priority": "TOP141",
    "pump_flowrate_mode": "TOP106", "dhw_sensor_selection": "TOP143",
    "dhw_heater_allowed": "TOP58", "room_heater_allowed": "TOP59",
    "heater_on_outdoor_temperature": "TOP78", "force_heater": "TOP68",
}
CURVE_READBACK = {
    "zone1_heat_curve": ("TOP29", "TOP30", "TOP31", "TOP32"),
    "zone1_cool_curve": ("TOP72", "TOP73", "TOP74", "TOP75"),
    "zone2_heat_curve": ("TOP82", "TOP83", "TOP84", "TOP85"),
    "zone2_cool_curve": ("TOP86", "TOP87", "TOP88", "TOP89"),
}


def test_readback_kinds_and_identities():
    kinds = {}
    for c in control.CONTROLS:
        kinds.setdefault(c.readback.kind, set()).add(c.key)
        if c.readback.kind == "state" and c.readback.fields is None:
            assert STATE_READBACK[c.key] == c.readback.identity
    assert kinds["state"] == set(STATE_READBACK) | set(CURVE_READBACK)
    assert kinds["effect"] == {"force_defrost", "force_sterilization"}
    assert kinds["none"] == {"pump_service_mode", "fault_reset"} | {
        c.key for c in control.CONTROLS if c.family == "optional_pcb"
    }
    for key, tops in CURVE_READBACK.items():
        assert dict(control.definitions()[key].readback.fields) == dict(
            zip(("target_high", "target_low", "outside_high", "outside_low"), tops))


def value_of(key, raw):
    return control.definitions()[key].readback.value_of(raw)


def test_readback_decoding():
    assert [value_of("operation_mode", str(i)) for i in range(10)] == [
        "heat", "cool", "auto", "dhw", "heat_dhw", "cool_dhw", "auto_dhw", "auto", "auto_dhw", None,
    ]
    assert [value_of("holiday_mode", str(i)) for i in range(4)] == [False, True, True, None]
    assert [value_of("quiet_mode", str(i)) for i in range(6)] == [
        "off", "level_1", "level_2", "level_3", None, None,
    ]
    assert [value_of("heat_pump_power", raw) for raw in ("0", "1", "2", "-1", "x", None)] == [
        False, True, None, None, None, None,
    ]
    assert value_of("dhw_target_temperature", "48") == 48
    assert value_of("dhw_target_temperature", "48.5") is None
    assert value_of("dhw_heat_delta", "-8") == -8
    assert [value_of("active_zones", str(i)) for i in range(3)] == ["zone1", "zone2", "zone1_zone2"]
    assert value_of("force_defrost", "1") is True and value_of("force_defrost", "0") is False


def test_expected_readback():
    assert prep("operation_mode", "auto").expected == "auto"
    assert prep("holiday_mode", True).expected is True
    assert prep("force_defrost").expected is True
    assert prep("force_sterilization").expected is True
    assert prep("fault_reset").expected is None
    assert prep("pump_service_mode", True).expected is None
    assert prep("zone1_heat_curve", {"target_low": 23}).expected == {"target_low": 23}
    for c in control.CONTROLS:
        if c.family == "optional_pcb":
            value = {"optional_pcb": None}
            prepared = control.prepare(c.key, _sample(c), OK)
            assert prepared.expected is None and value


def _sample(c):
    v = c.value
    if isinstance(v, control.BooleanValue):
        return True
    if isinstance(v, control.EnumValue):
        return v.options[0][0]
    if isinstance(v, control.IntegerChoiceValue):
        return v.choices[0][0]
    return 20


def test_current_state_never_uses_command_echoes():
    facts = {"TOP9": retained(48), "TOP29": live(32), "TOP30": live(23), "TOP31": live("x"),
             "SetSmartGridMode": live(2), "commands/SetSmartGridMode": live(2)}
    defs = control.definitions()
    assert control.current_state(defs["dhw_target_temperature"], facts) == 48
    assert control.current_state(defs["zone1_heat_curve"], facts) == {
        "target_high": 32, "target_low": 23, "outside_high": None, "outside_low": None,
    }
    assert control.current_state(defs["pcb_smart_grid_mode"], facts) is None
    assert control.current_state(defs["fault_reset"], facts) is None


def test_reading_fact_from_live_reading_shape():
    fact = ReadingFact.from_reading({"topic": "main/Optional_PCB", "value": 1, "kind": "number",
                                     "raw": "1", "mode": "live", "available": True,
                                     "received_at": "2027-01-15T08:00:00Z"})
    assert fact == live(1)
    none = ReadingFact.from_reading({"topic": "main/Optional_PCB", "value": None, "kind": None,
                                     "raw": None, "mode": "none", "available": False,
                                     "received_at": None})
    assert control.live_int({"TOP110": none}, "TOP110") is None
