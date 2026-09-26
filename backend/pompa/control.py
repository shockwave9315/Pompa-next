"""Pure HeishaMon control domain (Stage 4E-B; docs/ARCHITECTURE.md §25.5).

This module is the single authoritative definition of every executable semantic control:
identity, upstream command, class, value schema, validation, payload encoding, readback
meaning, prerequisites and documented restrictions. It performs no I/O. It never
publishes MQTT, serves HTTP, touches storage or imports the Recorder. The runtime
(Stage 4E-C) passes in live reading facts and publishes a :class:`PreparedCommand`.

All protocol facts below come from the pinned upstream HeishaMon
(``heishamon/HeishaMon`` commit ``0de4f3c02598f542e7859a772e627e5c1ebc2ce3``). The firmware
parses heat-pump payloads with Arduino ``String.toInt()`` and performs no range
validation, so the checks here are the only validation.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pompa.capabilities import Capability, effective_capabilities, normalize_payload

Family = Literal["heat_pump", "optional_pcb"]
ControlClass = Literal["setting", "temporary", "trigger", "curve", "pcb_input"]
ReadbackKind = Literal["state", "effect", "none"]
RangeBasis = Literal["documented", "protocol"]
ErrorCode = Literal[
    "unknown_control", "invalid_request", "invalid_value",
    "prerequisite_not_met", "validation_context_unavailable",
]

# Documented applicability notes. They are reported, never enforced: no reliable installed-device
# fact can prove them (docs/ARCHITECTURE.md §25.5.7).
DOCUMENTED_ALL_IN_ONE_ONLY = "documented_all_in_one_only"
DOCUMENTED_H_J_SERIES_ONLY = "documented_h_j_series_only"
FIRMWARE_MIN_4_2_0 = "firmware_min_4_2_0"

# Upstream command names that are known but deliberately not exposed as semantic controls.
EXCLUDED_COMMANDS: Mapping[str, str] = {
    "SetOptPCBByte9": (
        "Firmware-only Optional PCB command (optionalCommands[] at the pinned commit). "
        "OptionalPCB.md documents datagram byte 09 only as '?', so it has no semantic meaning "
        "that could be validated."
    ),
    "SetHeatCoolMode": (
        "Catalog Optional PCB command. The firmware sets bit 7 of datagram byte 06 to "
        "toInt()==1, and OptionalPCB.md names that bit only 'Heat/Cool' (Heat/Cool SW). No "
        "pinned source says which bit value selects heat or cool, so neither request value has a "
        "semantic meaning that could be validated."
    ),
}


class ControlError(ValueError):
    """A request refused before anything could be published."""

    def __init__(self, code: ErrorCode, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _Absent:
    """The request carried no ``value`` (the only valid shape for a trigger)."""

    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = _Absent()


@dataclass(frozen=True, slots=True)
class ReadingFact:
    """One live-reading fact as the existing live path already classifies it.

    ``mode`` is ``live``, ``retained`` or ``none``; ``available`` applies the shared freshness
    rule. Only a live, available reading is current evidence.
    """

    raw: str | None
    mode: str
    available: bool

    @classmethod
    def from_reading(cls, reading: Mapping) -> ReadingFact:
        """Build from one ``/live?include=readings`` entry shape."""
        return cls(reading.get("raw"), reading.get("mode", "none"), bool(reading.get("available")))


Facts = Mapping[str, ReadingFact]


def raw_int(raw: str | None) -> int | None:
    """The integer a TOP payload carries, or ``None`` (absent, text, fractional)."""
    if raw is None:
        return None
    typed = normalize_payload(raw)
    return typed.value if typed.kind == "number" and type(typed.value) is int else None


def live_int(facts: Facts, identity: str) -> int | None:
    """The integer of a live, available reading; ``None`` for absent, retained or stale facts."""
    fact = facts.get(identity)
    if fact is None or fact.mode != "live" or not fact.available:
        return None
    return raw_int(fact.raw)


# --------------------------------------------------------------------------- value schemas


def _invalid(detail: str) -> ControlError:
    return ControlError("invalid_value", detail)


@dataclass(frozen=True, slots=True)
class BooleanValue:
    """``true``/``false`` → ``1``/``0``. Only Python ``bool``; ``0``/``1`` integers are refused."""

    def validate(self, value: object, facts: Facts) -> bool:
        if type(value) is not bool:
            raise _invalid("expected a boolean")
        return value

    def encode(self, value: bool) -> str:
        return "1" if value else "0"

    def schema(self, facts: Facts) -> dict:
        return {"type": "boolean"}


@dataclass(frozen=True, slots=True)
class EnumValue:
    """Semantic enum id → decimal firmware code."""

    options: tuple[tuple[str, int], ...]

    def validate(self, value: object, facts: Facts) -> str:
        if type(value) is not str or value not in dict(self.options):
            raise _invalid(f"expected one of {[name for name, _ in self.options]}")
        return value

    def encode(self, value: str) -> str:
        return str(dict(self.options)[value])

    def schema(self, facts: Facts) -> dict:
        return {"type": "enum", "values": [name for name, _ in self.options]}


@dataclass(frozen=True, slots=True)
class IntegerValue:
    """An integer range. ``basis`` says whether upstream documents it or only the encoding
    bounds it (docs/ARCHITECTURE.md §25.5.5)."""

    minimum: int
    maximum: int
    unit: str | None
    basis: RangeBasis

    def validate(self, value: object, facts: Facts) -> int:
        # bool is an int subclass; a JSON true must never become payload "1".
        if type(value) is not int:
            raise _invalid("expected an integer")
        if not self.minimum <= value <= self.maximum:
            raise _invalid(f"expected {self.minimum}..{self.maximum}")
        return value

    def encode(self, value: int) -> str:
        return str(value)

    def schema(self, facts: Facts) -> dict:
        return {"type": "integer", "min": self.minimum, "max": self.maximum,
                "unit": self.unit, "range_basis": self.basis}


@dataclass(frozen=True, slots=True)
class IntegerChoiceValue:
    """A closed set of integers, each with its documented payload."""

    choices: tuple[tuple[int, str], ...]
    unit: str | None

    def validate(self, value: object, facts: Facts) -> int:
        if type(value) is not int or value not in dict(self.choices):
            raise _invalid(f"expected one of {[choice for choice, _ in self.choices]}")
        return value

    def encode(self, value: int) -> str:
        return dict(self.choices)[value]

    def schema(self, facts: Facts) -> dict:
        return {"type": "integer_choice", "values": [choice for choice, _ in self.choices],
                "unit": self.unit}


@dataclass(frozen=True, slots=True)
class NumberValue:
    """A finite decimal number (Optional PCB emulated temperatures, parsed with ``toFloat()``)."""

    minimum: float
    maximum: float
    unit: str | None

    def validate(self, value: object, facts: Facts) -> int | float:
        # An int is always finite; math.isfinite() would overflow on a huge one.
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
            raise _invalid("expected a finite number")
        if not self.minimum <= value <= self.maximum:
            raise _invalid(f"expected {self.minimum}..{self.maximum}")
        return value

    def encode(self, value: int | float) -> str:
        if type(value) is int or value == 0:
            return str(int(value))
        # Shortest round-trip digits, never exponent notation.
        return format(Decimal(repr(value)), "f")

    def schema(self, facts: Facts) -> dict:
        return {"type": "number", "min": self.minimum, "max": self.maximum, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class TriggerValue:
    """A one-shot action. The request carries no value; the payload is always ``1``, because the
    firmware encodes ``0`` as "no change" for these commands."""

    def validate(self, value: object, facts: Facts) -> None:
        return None

    def encode(self, value: None) -> str:
        return "1"

    def schema(self, facts: Facts) -> dict:
        return {"type": "trigger"}


SHIFT = IntegerValue(-5, 5, "K", "documented")


@dataclass(frozen=True, slots=True)
class RequestTemperatureValue:
    """A zone request that is a curve shift or a direct temperature, chosen by a live TOP.

    ``0`` in the context TOP means compensation curve (shift), ``1`` direct (TOP76/TOP81).
    """

    context: str
    direct: IntegerValue

    def active(self, facts: Facts) -> str | None:
        return {0: "shift", 1: "direct"}.get(live_int(facts, self.context))

    def validate(self, value: object, facts: Facts) -> int:
        active = self.active(facts)
        if active is None:
            raise ControlError(
                "validation_context_unavailable",
                f"no live {self.context} reading selects the shift or direct range",
            )
        return (SHIFT if active == "shift" else self.direct).validate(value, facts)

    def encode(self, value: int) -> str:
        return str(value)

    def schema(self, facts: Facts) -> dict:
        return {
            "type": "request_temperature", "context_identity": self.context,
            "active": self.active(facts),
            "ranges": {name: {"min": schema.minimum, "max": schema.maximum, "unit": schema.unit,
                              "range_basis": schema.basis}
                       for name, schema in (("shift", SHIFT), ("direct", self.direct))},
        }


CURVE_FIELDS = ("target_high", "target_low", "outside_high", "outside_low")


@dataclass(frozen=True, slots=True)
class CurveValue:
    """A partial ``SetCurves`` update of one zone/mode, encoded only from supplied fields."""

    zone: int
    mode: Literal["heat", "cool"]
    field_range: IntegerValue

    def validate(self, value: object, facts: Facts) -> dict[str, int]:
        if type(value) is not dict:
            raise _invalid("expected an object of curve fields")
        if not value:
            raise _invalid("expected at least one curve field")
        unknown = sorted(str(name) for name in value if name not in CURVE_FIELDS)
        if unknown:
            raise _invalid(f"unknown curve fields {unknown}")
        return {name: self.field_range.validate(value[name], facts)
                for name in CURVE_FIELDS if name in value}

    def encode(self, value: Mapping[str, int]) -> str:
        document: dict = {}
        for name in CURVE_FIELDS:  # deterministic order: target before outside, high before low
            if name in value:
                location, point = name.split("_")
                document.setdefault(location, {})[point] = value[name]
        return json.dumps({f"zone{self.zone}": {self.mode: document}}, separators=(",", ":"))

    def schema(self, facts: Facts) -> dict:
        field = {"min": self.field_range.minimum, "max": self.field_range.maximum,
                 "unit": self.field_range.unit, "range_basis": self.field_range.basis}
        return {"type": "curve", "fields": {name: dict(field) for name in CURVE_FIELDS}}


ValueSchema = (BooleanValue | EnumValue | IntegerValue | IntegerChoiceValue | NumberValue
               | TriggerValue | RequestTemperatureValue | CurveValue)


# ------------------------------------------------------------------------ readback metadata


@dataclass(frozen=True, slots=True)
class Readback:
    """What a heat-pump-published TOP would mean for a request.

    ``state``: the TOP decodes the protocol byte the command writes. ``effect``: the TOP is the
    resulting machine state, not an acknowledgement. ``none``: nothing reads the command back.
    ``decode`` maps the TOP integer to the control's semantic value; ``None`` means the TOP
    carries the semantic integer itself. ``fields`` maps curve fields to their TOPs.
    """

    kind: ReadbackKind
    identity: str | None = None
    decode: Mapping[int, object] | None = None
    fields: Mapping[str, str] | None = None
    effect: object | None = None

    def identities(self) -> tuple[str, ...]:
        if self.fields is not None:
            return tuple(self.fields.values())
        return (self.identity,) if self.identity is not None else ()

    def value_of(self, raw: str | None) -> object | None:
        """The semantic value a raw TOP payload carries, or ``None`` if it does not map."""
        number = raw_int(raw)
        if number is None:
            return None
        if self.decode is None:
            return number
        return self.decode.get(number)


NO_READBACK = Readback("none")


def _state(identity: str, decode: Mapping[int, object] | None = None) -> Readback:
    return Readback("state", identity, decode)


BOOL_DECODE: Mapping[int, object] = {0: False, 1: True}


def _enum_decode(options: tuple[tuple[str, int], ...],
                 extra: Mapping[int, str] | None = None) -> Mapping[int, object]:
    """Invert an enum; ``extra`` adds further readable codes of the same semantic value."""
    decode: dict[int, object] = {code: name for name, code in options}
    decode.update(extra or {})
    return decode


# ---------------------------------------------------------------------------- prerequisites


@dataclass(frozen=True, slots=True)
class Prerequisite:
    """A documented condition for the command to have any effect.

    ``identity`` is the readable TOP proving it, or ``None`` when it is not observable over MQTT.
    Only a live reading of a documented state (``documented``) can make it ``False``; absent,
    retained, stale, ``-1`` (documented "unknown") or undocumented values stay ``None``.
    """

    id: str
    identity: str | None
    satisfied_by: frozenset[int] = frozenset()
    documented: frozenset[int] = frozenset()

    def evaluate(self, facts: Facts) -> bool | None:
        if self.identity is None:
            return None
        value = live_int(facts, self.identity)
        # MQTT-Topics.md: state topics report -1 for "unknown" in abnormal situations; it is
        # outside every documented domain, like any other undocumented code.
        return value in self.satisfied_by if value in self.documented else None


@dataclass(frozen=True, slots=True)
class PrerequisiteResult:
    id: str
    identity: str | None
    satisfied: bool | None


# Documented TOP domains: TOP110/TOP122 are 0/1 settings; TOP4 is 0..8 (MQTT-Topics.md).
HEAT_PUMP_OPTIONAL_PCB = Prerequisite("heat_pump_optional_pcb", "TOP110", frozenset({1}),
                                      frozenset({0, 1}))
HEISHAMON_OPTIONAL_PCB_EMULATION = Prerequisite("heishamon_optional_pcb_emulation", None)
DHW_OPERATION_MODE = Prerequisite("dhw_operation_mode", "TOP4", frozenset({3, 4, 5, 6, 8}),
                                  frozenset(range(9)))
EXTERNAL_COMPRESSOR_CONTROL = Prerequisite("external_compressor_control", "TOP122", frozenset({1}),
                                           frozenset({0, 1}))
PCB_PREREQUISITES = (HEAT_PUMP_OPTIONAL_PCB, HEISHAMON_OPTIONAL_PCB_EMULATION)


# ------------------------------------------------------------------------------ definitions


@dataclass(frozen=True, slots=True)
class Control:
    key: str
    identity: str
    command: str
    family: Family
    klass: ControlClass
    value: ValueSchema
    readback: Readback
    service: bool = False
    prerequisites: tuple[Prerequisite, ...] = ()
    restrictions: tuple[str, ...] = ()

    @property
    def context_identity(self) -> str | None:
        return self.value.context if isinstance(self.value, RequestTemperatureValue) else None


def _hp(key: str, set_id: int, command: str, klass: ControlClass, value: ValueSchema,
        readback: Readback, **extra) -> Control:
    return Control(key, f"SET{set_id}", command, "heat_pump", klass, value, readback, **extra)


def _pcb(key: str, command: str, value: ValueSchema, *extra_prerequisites: Prerequisite,
         restrictions: tuple[str, ...] = ()) -> Control:
    return Control(key, command, command, "optional_pcb", "pcb_input", value, NO_READBACK,
                   prerequisites=PCB_PREREQUISITES + extra_prerequisites, restrictions=restrictions)


def _int(minimum: int, maximum: int, unit: str | None, basis: RangeBasis = "documented") -> IntegerValue:
    return IntegerValue(minimum, maximum, unit, basis)


# Protocol bounds of the firmware's "value + 128" byte encoding. -128 would encode byte 0,
# which the Panasonic set query treats as "no change", so it is excluded.
SIGNED_BYTE = (-127, 127)

BOOL = BooleanValue()
TRIGGER = TriggerValue()
QUIET = (("off", 0), ("level_1", 1), ("level_2", 2), ("level_3", 3))
POWERFUL = (("off", 0), ("min_30", 1), ("min_60", 2), ("min_90", 3))
OPERATION = (("heat", 0), ("cool", 1), ("auto", 2), ("dhw", 3), ("heat_dhw", 4),
             ("cool_dhw", 5), ("auto_dhw", 6))
ZONES = (("zone1", 0), ("zone2", 1), ("zone1_zone2", 2))
PAD_HEATER = (("disabled", 0), ("type_a", 1), ("type_b", 2))
BIVALENT = (("alternative", 0), ("parallel", 1), ("advanced_parallel", 2))
HEATING_CONTROL = (("comfort", 0), ("efficiency", 1))
SMART_DHW = (("variable", 0), ("standard", 1))
QUIET_PRIORITY = (("sound", 0), ("capacity", 1))
FLOWRATE = (("delta_t", 0), ("max_duty", 1))
DHW_SENSOR = (("top", 0), ("center", 1))
HEATER_ALLOWED = (("blocked", 0), ("free", 1))
SMART_GRID = (("normal", 0), ("capacity_1", 1), ("hp_dhw_off", 2), ("capacity_2", 3))
THERMOSTAT = (("none", 0), ("cool", 1), ("heat", 2), ("heat_cool", 3))
# OptionalPCB.md byte 14 table; the firmware's default datagram also carries 0xEB (235) = 100 %.
DEMAND_CONTROL = ((5, "43"), (25, "82"), (50, "133"), (75, "184"), (100, "235"))
# temp2hex() converts only within -78..120 °C and clamps outside it (>120 → 0x00, <-78 → 0xFF).
# The resulting NTC byte quantizes non-uniformly (about 0.3 °C to 11 °C per step), so no step
# is claimed.
PCB_TEMPERATURE = NumberValue(-78, 120, "°C")

CURVE_TOPS = {
    (1, "heat"): ("TOP29", "TOP30", "TOP31", "TOP32"),
    (1, "cool"): ("TOP72", "TOP73", "TOP74", "TOP75"),
    (2, "heat"): ("TOP82", "TOP83", "TOP84", "TOP85"),
    (2, "cool"): ("TOP86", "TOP87", "TOP88", "TOP89"),
}


def _curve(zone: int, mode: Literal["heat", "cool"]) -> Control:
    return _hp(f"zone{zone}_{mode}_curve", 16, "SetCurves", "curve",
               CurveValue(zone, mode, _int(*SIGNED_BYTE, "°C", "protocol")),
               Readback("state", fields=dict(zip(CURVE_FIELDS, CURVE_TOPS[(zone, mode)]))))


CONTROLS: tuple[Control, ...] = (
    _hp("heat_pump_power", 1, "SetHeatpump", "setting", BOOL, _state("TOP0", BOOL_DECODE)),
    _hp("holiday_mode", 2, "SetHolidayMode", "setting", BOOL,
        _state("TOP19", {0: False, 1: True, 2: True})),  # 1 = scheduled, 2 = active
    _hp("quiet_mode", 3, "SetQuietMode", "setting", EnumValue(QUIET),
        _state("TOP18", _enum_decode(QUIET))),
    _hp("powerful_mode", 4, "SetPowerfulMode", "temporary", EnumValue(POWERFUL),
        _state("TOP17", _enum_decode(POWERFUL))),
    _hp("zone1_heat_request", 5, "SetZ1HeatRequestTemperature", "setting",
        RequestTemperatureValue("TOP76", _int(20, SIGNED_BYTE[1], "°C", "protocol")), _state("TOP27")),
    # Direct cool 5..20 °C is the TOP28/TOP35 readback text; the SET6/SET8 rows repeat "20 to max".
    _hp("zone1_cool_request", 6, "SetZ1CoolRequestTemperature", "setting",
        RequestTemperatureValue("TOP81", _int(5, 20, "°C")), _state("TOP28")),
    _hp("zone2_heat_request", 7, "SetZ2HeatRequestTemperature", "setting",
        RequestTemperatureValue("TOP76", _int(20, SIGNED_BYTE[1], "°C", "protocol")), _state("TOP34")),
    _hp("zone2_cool_request", 8, "SetZ2CoolRequestTemperature", "setting",
        RequestTemperatureValue("TOP81", _int(5, 20, "°C")), _state("TOP35")),
    # TOP4 reports Auto as Auto(Heat)=2 or Auto(Cool)=7, and Auto+DHW as 6 or 8.
    _hp("operation_mode", 9, "SetOperationMode", "setting", EnumValue(OPERATION),
        _state("TOP4", _enum_decode(OPERATION, {7: "auto", 8: "auto_dhw"}))),
    _hp("force_dhw", 10, "SetForceDHW", "temporary", BOOL, _state("TOP2", BOOL_DECODE),
        prerequisites=(DHW_OPERATION_MODE,)),
    _hp("dhw_target_temperature", 11, "SetDHWTemp", "setting", _int(40, 75, "°C"), _state("TOP9")),
    _hp("force_defrost", 12, "SetForceDefrost", "trigger", TRIGGER,
        Readback("effect", "TOP26", BOOL_DECODE, effect=True), service=True),
    _hp("force_sterilization", 13, "SetForceSterilization", "trigger", TRIGGER,
        Readback("effect", "TOP69", BOOL_DECODE, effect=True), service=True),
    _hp("pump_service_mode", 14, "SetPump", "setting", BOOL, NO_READBACK, service=True),
    _hp("max_pump_duty", 15, "SetMaxPumpDuty", "setting", _int(64, 254, "duty"), _state("TOP95"),
        service=True),
    _curve(1, "heat"),
    _curve(1, "cool"),
    _curve(2, "heat"),
    _curve(2, "cool"),
    _hp("active_zones", 17, "SetZones", "setting", EnumValue(ZONES), _state("TOP94", _enum_decode(ZONES))),
    _hp("heat_delta", 18, "SetFloorHeatDelta", "setting", _int(1, 15, "K"), _state("TOP23")),
    _hp("cool_delta", 19, "SetFloorCoolDelta", "setting", _int(1, 15, "K"), _state("TOP24")),
    _hp("dhw_heat_delta", 20, "SetDHWHeatDelta", "setting", _int(-12, -2, "K"), _state("TOP22")),
    # Encoded as value + 1; byte 0 would mean "no change", so 0..254.
    _hp("heater_delay_time", 21, "SetHeaterDelayTime", "setting", _int(0, 254, "min", "protocol"),
        _state("TOP96")),
    _hp("heater_start_delta", 22, "SetHeaterStartDelta", "setting",
        _int(*SIGNED_BYTE, "K", "protocol"), _state("TOP97")),
    _hp("heater_stop_delta", 23, "SetHeaterStopDelta", "setting",
        _int(*SIGNED_BYTE, "K", "protocol"), _state("TOP98")),
    _hp("main_schedule", 24, "SetMainSchedule", "setting", BOOL, _state("TOP13", BOOL_DECODE)),
    _hp("alt_external_sensor", 25, "SetAltExternalSensor", "setting", BOOL,
        _state("TOP108", BOOL_DECODE)),
    _hp("external_pad_heater", 26, "SetExternalPadHeater", "setting", EnumValue(PAD_HEATER),
        _state("TOP114", _enum_decode(PAD_HEATER))),
    _hp("buffer_delta", 27, "SetBufferDelta", "setting", _int(0, 10, "K"), _state("TOP113")),
    _hp("buffer_installed", 28, "SetBuffer", "setting", BOOL, _state("TOP99", BOOL_DECODE)),
    _hp("heating_off_outdoor_temperature", 29, "SetHeatingOffOutdoorTemp", "setting",
        _int(5, 35, "°C"), _state("TOP77")),
    _hp("external_control", 30, "SetExternalControl", "setting", BOOL, _state("TOP119", BOOL_DECODE)),
    _hp("external_error_signal", 31, "SetExternalError", "setting", BOOL,
        _state("TOP121", BOOL_DECODE)),
    _hp("external_compressor_control", 32, "SetExternalCompressorControl", "setting", BOOL,
        _state("TOP122", BOOL_DECODE)),
    _hp("external_heat_cool_control", 33, "SetExternalHeatCoolControl", "setting", BOOL,
        _state("TOP120", BOOL_DECODE)),
    _hp("bivalent_control", 34, "SetBivalentControl", "setting", BOOL, _state("TOP129", BOOL_DECODE)),
    _hp("bivalent_mode", 35, "SetBivalentMode", "setting", EnumValue(BIVALENT),
        _state("TOP130", _enum_decode(BIVALENT))),
    _hp("bivalent_start_temperature", 36, "SetBivalentStartTemp", "setting", _int(-15, 35, "°C"),
        _state("TOP131")),
    _hp("bivalent_advanced_start_temperature", 37, "SetBivalentAPStartTemp", "setting",
        _int(-15, 35, "°C"), _state("TOP134")),
    _hp("bivalent_advanced_stop_temperature", 38, "SetBivalentAPStopTemp", "setting",
        _int(-15, 35, "°C"), _state("TOP135")),
    _hp("heating_control", 39, "SetHeatingControl", "setting", EnumValue(HEATING_CONTROL),
        _state("TOP139", _enum_decode(HEATING_CONTROL))),
    _hp("smart_dhw", 40, "SetSmartDHW", "setting", EnumValue(SMART_DHW),
        _state("TOP140", _enum_decode(SMART_DHW))),
    _hp("quiet_mode_priority", 41, "SetQuietModePriority", "setting", EnumValue(QUIET_PRIORITY),
        _state("TOP141", _enum_decode(QUIET_PRIORITY))),
    _hp("pump_flowrate_mode", 42, "SetPumpFlowrateMode", "setting", EnumValue(FLOWRATE),
        _state("TOP106", _enum_decode(FLOWRATE))),
    _hp("dhw_sensor_selection", 43, "SetDHWSensorSelection", "setting", EnumValue(DHW_SENSOR),
        _state("TOP143", _enum_decode(DHW_SENSOR)), restrictions=(DOCUMENTED_ALL_IN_ONE_ONLY,)),
    _hp("dhw_heater_allowed", 44, "SetDHWHeaterState", "setting", EnumValue(HEATER_ALLOWED),
        _state("TOP58", _enum_decode(HEATER_ALLOWED))),
    _hp("room_heater_allowed", 45, "SetRoomHeaterState", "setting", EnumValue(HEATER_ALLOWED),
        _state("TOP59", _enum_decode(HEATER_ALLOWED))),
    _hp("heater_on_outdoor_temperature", 46, "SetHeaterOnOutdoorTemp", "setting", _int(-15, 20, "°C"),
        _state("TOP78")),
    _hp("force_heater", 47, "SetForceHeater", "setting", BOOL, _state("TOP68", BOOL_DECODE),
        service=True, restrictions=(FIRMWARE_MIN_4_2_0,)),
    _hp("fault_reset", 48, "SetReset", "trigger", TRIGGER, NO_READBACK, service=True),
    _pcb("pcb_compressor_switch", "SetCompressorState", BOOL, EXTERNAL_COMPRESSOR_CONTROL),
    _pcb("pcb_smart_grid_mode", "SetSmartGridMode", EnumValue(SMART_GRID)),
    _pcb("pcb_thermostat1_demand", "SetExternalThermostat1State", EnumValue(THERMOSTAT),
         restrictions=(DOCUMENTED_H_J_SERIES_ONLY,)),
    _pcb("pcb_thermostat2_demand", "SetExternalThermostat2State", EnumValue(THERMOSTAT)),
    _pcb("pcb_demand_control", "SetDemandControl", IntegerChoiceValue(DEMAND_CONTROL, "%")),
    _pcb("pcb_pool_temperature", "SetPoolTemp", PCB_TEMPERATURE),
    _pcb("pcb_buffer_temperature", "SetBufferTemp", PCB_TEMPERATURE,
         restrictions=(DOCUMENTED_H_J_SERIES_ONLY,)),
    _pcb("pcb_zone1_room_temperature", "SetZ1RoomTemp", PCB_TEMPERATURE,
         restrictions=(DOCUMENTED_H_J_SERIES_ONLY,)),
    _pcb("pcb_zone1_water_temperature", "SetZ1WaterTemp", PCB_TEMPERATURE),
    _pcb("pcb_zone2_room_temperature", "SetZ2RoomTemp", PCB_TEMPERATURE),
    _pcb("pcb_zone2_water_temperature", "SetZ2WaterTemp", PCB_TEMPERATURE),
    _pcb("pcb_solar_temperature", "SetSolarTemp", PCB_TEMPERATURE),
)

CONTROLS_BY_KEY: Mapping[str, Control] = {control.key: control for control in CONTROLS}
_FAMILY = {"heat_pump": "SET", "optional_pcb": "PCB"}


def check_definitions(controls: tuple[Control, ...], capabilities: tuple[Capability, ...]) -> None:
    """Prove the definitions against the effective capability catalog. Raises ``ValueError``."""
    catalog = {c.reference.identity: c for c in capabilities}
    keys = [control.key for control in controls]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate control key")
    for control in controls:
        capability = catalog.get(control.identity)
        if capability is None:
            raise ValueError(f"{control.key}: unknown capability {control.identity}")
        if capability.reference.family != _FAMILY[control.family]:
            raise ValueError(f"{control.key}: family mismatch for {control.identity}")
        if capability.reference.name != control.command:
            raise ValueError(f"{control.key}: command {control.command} is not {capability.reference.name}")
        if (control.klass == "pcb_input") != (control.family == "optional_pcb"):
            raise ValueError(f"{control.key}: class/family mismatch")
        if (control.klass == "trigger") != isinstance(control.value, TriggerValue):
            raise ValueError(f"{control.key}: trigger class/value mismatch")
        if (control.klass == "curve") != isinstance(control.value, CurveValue):
            raise ValueError(f"{control.key}: curve class/value mismatch")
        if control.family == "optional_pcb" and control.readback.kind != "none":
            raise ValueError(f"{control.key}: Optional PCB inputs have no readback")
        referenced = list(control.readback.identities())
        referenced += [p.identity for p in control.prerequisites if p.identity is not None]
        if control.context_identity is not None:
            referenced.append(control.context_identity)
        for identity in referenced:
            if identity not in catalog or not catalog[identity].readable:
                raise ValueError(f"{control.key}: {identity} is not a readable capability")
    commands = {c.reference.name for c in capabilities if not c.readable}
    covered = {control.command for control in controls}
    excluded = set(EXCLUDED_COMMANDS)
    if covered & excluded:
        raise ValueError(f"Excluded commands are defined: {sorted(covered & excluded)}")
    if covered | (commands & excluded) != commands:
        raise ValueError(f"Undefined commands {sorted(commands - covered - excluded)}; "
                         f"unknown commands {sorted(covered - commands)}")


@lru_cache(maxsize=1)
def definitions() -> Mapping[str, Control]:
    """The verified control definitions, keyed by semantic key."""
    capabilities = effective_capabilities()
    check_definitions(CONTROLS, capabilities)
    return CONTROLS_BY_KEY


@lru_cache(maxsize=1)
def _topics() -> Mapping[str, str]:
    return {c.reference.identity: c.topic for c in effective_capabilities()}


# ------------------------------------------------------------------------------- pure logic


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    """A validated request, encoded for HeishaMon; publishing it is the runtime's job."""

    control: Control
    topic: str  # relative to the configured MQTT prefix, e.g. ``commands/SetDHWTemp``
    payload: str
    requested: object
    expected: object | None
    prerequisites: tuple[PrerequisiteResult, ...]


def evaluate_prerequisites(control: Control, facts: Facts) -> tuple[PrerequisiteResult, ...]:
    return tuple(PrerequisiteResult(p.id, p.identity, p.evaluate(facts)) for p in control.prerequisites)


def expected_readback(control: Control, requested: object) -> object | None:
    """The semantic readback value a successful request would show, or ``None`` (no readback)."""
    readback = control.readback
    if readback.kind == "none":
        return None
    if readback.kind == "effect":
        return readback.effect
    return dict(requested) if isinstance(requested, dict) else requested


def current_state(control: Control, facts: Facts) -> object | None:
    """The semantic value of the current readback reading(s), regardless of mode.

    Returns ``None`` without readback or when the raw value does not map; a curve returns
    its four fields, each possibly ``None``.
    """
    readback = control.readback
    if readback.kind == "none":
        return None
    if readback.fields is not None:
        return {name: readback.value_of(_raw(facts, identity)) for name, identity in readback.fields.items()}
    return readback.value_of(_raw(facts, readback.identity))


def _raw(facts: Facts, identity: str | None) -> str | None:
    fact = facts.get(identity) if identity is not None else None
    return fact.raw if fact is not None else None


def schema(control: Control, facts: Facts) -> dict:
    return control.value.schema(facts)


def prepare(key: str, value: object, facts: Facts) -> PreparedCommand:
    """Validate a semantic request and encode it, or raise :class:`ControlError`.

    Order: unknown key (``unknown_control``), request shape (``invalid_request``), validation
    context and value (``validation_context_unavailable``/``invalid_value``), then documented
    prerequisites proven false by a live reading (``prerequisite_not_met``).
    """
    control = definitions().get(key)
    if control is None:
        raise ControlError("unknown_control", f"no control {key!r}")
    trigger = isinstance(control.value, TriggerValue)
    if trigger and value is not ABSENT:
        raise ControlError("invalid_request", "a trigger takes no value")
    if not trigger and value is ABSENT:
        raise ControlError("invalid_request", "a value is required")
    requested = control.value.validate(value, facts)
    prerequisites = evaluate_prerequisites(control, facts)
    failed = [p.id for p in prerequisites if p.satisfied is False]
    if failed:
        raise ControlError("prerequisite_not_met", f"prerequisite not met: {', '.join(failed)}")
    return PreparedCommand(
        control=control,
        topic=_topics()[control.identity],
        payload=control.value.encode(requested),
        requested=requested,
        expected=expected_readback(control, requested),
        prerequisites=prerequisites,
    )
