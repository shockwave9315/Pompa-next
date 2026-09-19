"""Core metric catalog: the single definition of every recorded metric.

Topic facts come from docs/reference/heishamon/. TOP topics are published as
``{prefix}/main/<Name>`` (MQTT-Topics.md). The checked-in reference names the
XTOP values (realne_dane.md) but does not state their topic path; ``extra/`` is
the assumed HeishaMon sub-topic and is verified by the Stage 1 runtime
measurement (``/api/v1/status`` lists uncatalogued topics seen under the prefix).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

Kind = Literal["mean", "last"]

TEMP_SENTINELS = frozenset({-78.0, -128.0})
TOP_POWER_SENTINELS = frozenset({-200.0})
# MQTT-Topics.md: "All Topics related with state can have also value -1 - unknown".
STATE_SENTINELS = frozenset({-1.0})


@dataclass(frozen=True)
class Source:
    id: str  # HeishaMon identifier, e.g. "TOP16" or "XTOP0"
    topic: str  # topic below MQTT_TOPIC_PREFIX
    sentinels: frozenset[float] = frozenset()


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str | None
    group: str
    kind: Kind
    sources: tuple[Source, ...]  # history/live priority order
    min_value: float | None = None
    max_value: float | None = None
    record: bool = True


def _top(n: int, name: str, sentinels: frozenset[float] = frozenset()) -> Source:
    return Source(f"TOP{n}", f"main/{name}", sentinels)


def _xtop(n: int, name: str) -> Source:
    return Source(f"XTOP{n}", f"extra/{name}")


def _temp(key: str, label: str, n: int, name: str) -> Metric:
    return Metric(key, label, "°C", "temperature", "mean", (_top(n, name, TEMP_SENTINELS),))


def _power(key: str, label: str, xtop: Source, top: Source) -> Metric:
    return Metric(key, label, "W", "power", "mean", (xtop, top), min_value=0.0)


METRICS: tuple[Metric, ...] = (
    _temp("main_outlet_temp", "Temperatura zasilania", 6, "Main_Outlet_Temp"),
    _temp("main_inlet_temp", "Temperatura powrotu", 5, "Main_Inlet_Temp"),
    _temp("main_target_temp", "Temperatura zadana zasilania", 7, "Main_Target_Temp"),
    _temp("dhw_tank_temp", "Temperatura CWU", 10, "DHW_Temp"),
    _temp("outside_temp", "Temperatura zewnętrzna", 14, "Outside_Temp"),
    Metric("water_pressure", "Ciśnienie wody", "bar", "hydraulics", "mean",
           (_top(115, "Water_Pressure"),), min_value=0.0),
    Metric("pump_flow", "Przepływ", "l/min", "hydraulics", "mean",
           (_top(1, "Pump_Flow"),), min_value=0.0),
    Metric("pump_speed", "Obroty pompy", "r/min", "hydraulics", "mean",
           (_top(65, "Pump_Speed"),), min_value=0.0),
    Metric("compressor_freq", "Częstotliwość sprężarki", "Hz", "compressor", "mean",
           (_top(8, "Compressor_Freq"),), min_value=0.0),
    Metric("compressor_current", "Prąd sprężarki", "A", "compressor", "mean",
           (_top(67, "Compressor_Current"),), min_value=0.0),
    Metric("fan1_speed", "Obroty wentylatora 1", "r/min", "compressor", "mean",
           (_top(62, "Fan1_Motor_Speed"),), min_value=0.0),
    _power("co_power_consumption", "Pobór mocy CO",
           _xtop(0, "Heat_Power_Consumption_Extra"),
           _top(16, "Heat_Power_Consumption", TOP_POWER_SENTINELS)),
    _power("co_power_production", "Moc grzewcza CO",
           _xtop(3, "Heat_Power_Production_Extra"),
           _top(15, "Heat_Power_Production", TOP_POWER_SENTINELS)),
    _power("dhw_power_consumption", "Pobór mocy CWU",
           _xtop(2, "DHW_Power_Consumption_Extra"),
           _top(41, "DHW_Power_Consumption", TOP_POWER_SENTINELS)),
    _power("dhw_power_production", "Moc grzewcza CWU",
           _xtop(5, "DHW_Power_Production_Extra"),
           _top(40, "DHW_Power_Production", TOP_POWER_SENTINELS)),
    Metric("heatpump_state", "Pompa ciepła włączona", None, "state", "mean",
           (_top(0, "Heatpump_State", STATE_SENTINELS),), min_value=0.0, max_value=1.0),
    Metric("defrosting_state", "Odszranianie", None, "state", "mean",
           (_top(26, "Defrosting_State", STATE_SENTINELS),), min_value=0.0, max_value=1.0),
    Metric("three_way_valve", "Zawór trójdrogowy (0=CO, 1=CWU)", None, "state", "mean",
           (_top(20, "ThreeWay_Valve_State", STATE_SENTINELS),), min_value=0.0, max_value=1.0),
    Metric("operating_mode", "Tryb pracy", None, "state", "last",
           (_top(4, "Operating_Mode_State", STATE_SENTINELS),), min_value=0.0, max_value=8.0),
    Metric("operations_counter", "Licznik uruchomień", None, "counter", "last",
           (_top(12, "Operations_Counter"),), min_value=0.0),
    Metric("operations_hours", "Czas pracy", "h", "counter", "last",
           (_top(11, "Operations_Hours"),), min_value=0.0),
)

METRICS_BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}
RECORDED: tuple[Metric, ...] = tuple(m for m in METRICS if m.record)
RECORDED_KEYS: tuple[str, ...] = tuple(m.key for m in RECORDED)
SOURCE_BY_TOPIC: dict[str, tuple[Metric, Source]] = {
    s.topic: (m, s) for m in METRICS for s in m.sources
}

assert len(METRICS_BY_KEY) == len(METRICS), "duplicate metric key"
assert len(SOURCE_BY_TOPIC) == sum(len(m.sources) for m in METRICS), "topic mapped twice"


class Outcome(Enum):
    VALID = "valid"
    SENTINEL = "sentinel"  # a documented "unknown" value, e.g. -200 W on TOP power
    REJECTED = "rejected"  # non-numeric, non-finite, or outside the valid range


def parse_value(metric: Metric, source: Source, payload: str) -> tuple[float | None, Outcome]:
    """Parse one payload. Only VALID yields a number; ``0`` is a real zero."""
    try:
        value = float(payload.strip())
    except ValueError:
        return None, Outcome.REJECTED
    if not math.isfinite(value):
        return None, Outcome.REJECTED
    if value in source.sentinels:
        return None, Outcome.SENTINEL
    if metric.min_value is not None and value < metric.min_value:
        return None, Outcome.REJECTED
    if metric.max_value is not None and value > metric.max_value:
        return None, Outcome.REJECTED
    return value, Outcome.VALID
