import pytest

from conftest import REPO_ROOT
from pompa.catalog import (
    METRICS_BY_KEY,
    RECORDED_KEYS,
    SOURCE_BY_TOPIC,
    Outcome,
    parse_numeric,
    parse_value,
)
from pompa.config import ConfigError, load_settings

SAMPLE_1M_COLUMNS = (
    "main_outlet_temp", "main_inlet_temp", "main_target_temp", "dhw_tank_temp",
    "outside_temp", "water_pressure", "pump_flow", "pump_speed", "compressor_freq",
    "compressor_current", "fan1_speed", "co_power_consumption", "co_power_production",
    "dhw_power_consumption", "dhw_power_production", "heatpump_state",
    "defrosting_state", "three_way_valve", "operating_mode", "operations_counter",
    "operations_hours",
)


def parse(key, source_id, payload):
    metric = METRICS_BY_KEY[key]
    source = next(s for s in metric.sources if s.id == source_id)
    return parse_value(metric, source, payload)


def test_recorded_keys_match_sample_1m_columns():
    assert RECORDED_KEYS == SAMPLE_1M_COLUMNS


def test_kinds():
    last = {k for k in RECORDED_KEYS if METRICS_BY_KEY[k].kind == "last"}
    assert last == {"operating_mode", "operations_counter", "operations_hours"}


@pytest.mark.parametrize("key,order", [
    ("co_power_consumption", ["XTOP0", "TOP16"]),
    ("co_power_production", ["XTOP3", "TOP15"]),
    ("dhw_power_consumption", ["XTOP2", "TOP41"]),
    ("dhw_power_production", ["XTOP5", "TOP40"]),
])
def test_power_source_priority(key, order):
    assert [s.id for s in METRICS_BY_KEY[key].sources] == order


def test_zero_is_a_real_value():
    assert parse("co_power_consumption", "XTOP0", "0") == (0.0, Outcome.VALID)
    assert parse("co_power_consumption", "TOP16", "0") == (0.0, Outcome.VALID)
    assert parse("compressor_freq", "TOP8", "0") == (0.0, Outcome.VALID)
    assert parse("outside_temp", "TOP14", "0") == (0.0, Outcome.VALID)
    assert parse("heatpump_state", "TOP0", "0") == (0.0, Outcome.VALID)


def test_sentinels_are_unknown():
    assert parse("co_power_consumption", "TOP16", "-200") == (None, Outcome.SENTINEL)
    assert parse("dhw_power_production", "TOP40", "-200.0") == (None, Outcome.SENTINEL)
    assert parse("outside_temp", "TOP14", "-78") == (None, Outcome.SENTINEL)
    assert parse("main_inlet_temp", "TOP5", "-128") == (None, Outcome.SENTINEL)
    assert parse("heatpump_state", "TOP0", "-1") == (None, Outcome.SENTINEL)


def test_sentinels_are_per_source_not_global():
    # -200 is a TOP power sentinel only; on XTOP it is simply out of range.
    assert parse("co_power_consumption", "XTOP0", "-200") == (None, Outcome.REJECTED)
    # A legitimately negative temperature is valid.
    assert parse("outside_temp", "TOP14", "-12.5") == (-12.5, Outcome.VALID)


@pytest.mark.parametrize("payload", ["", "abc", "No error", "nan", "inf", "-inf"])
def test_non_numeric_rejected(payload):
    assert parse("main_outlet_temp", "TOP6", payload) == (None, Outcome.REJECTED)


def test_out_of_range_rejected():
    assert parse("heatpump_state", "TOP0", "2") == (None, Outcome.REJECTED)
    assert parse("operating_mode", "TOP4", "9") == (None, Outcome.REJECTED)
    assert parse("pump_flow", "TOP1", "-0.5") == (None, Outcome.REJECTED)


def _reference_rows():
    text = (REPO_ROOT / "docs/reference/heishamon/realne_dane.md").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines()[1:]:
        ident, name, value, _ = line.split("\t", 3)
        rows[ident] = (name, value)
    return rows


# Expected parse of the real HeishaMon snapshot in docs/reference/heishamon/realne_dane.md.
REFERENCE_EXPECTED = {
    "TOP0": 1.0, "TOP1": 0.13, "TOP4": 3.0, "TOP5": 25.5, "TOP6": 25.5, "TOP7": 23.0,
    "TOP8": 0.0, "TOP10": 44.0, "TOP11": 7724.0, "TOP12": 7651.0, "TOP14": 23.0,
    "TOP15": None, "TOP16": None, "TOP20": 0.0, "TOP26": 0.0, "TOP40": None,
    "TOP41": None, "TOP62": 0.0, "TOP65": 0.0, "TOP67": 0.0, "TOP115": 1.7,
    "XTOP0": 18.0, "XTOP2": 0.0, "XTOP3": 0.0, "XTOP5": 0.0,
}


def test_real_reference_snapshot():
    rows = _reference_rows()
    seen = set()
    for metric, source in SOURCE_BY_TOPIC.values():
        name, raw = rows[source.id]
        # The catalog topic name must be the reference name for that ID.
        assert source.topic.split("/", 1)[1] == name, source.id
        value, outcome = parse_value(metric, source, raw)
        assert value == REFERENCE_EXPECTED[source.id], source.id
        if value is None:
            assert (raw, outcome) == ("-200", Outcome.SENTINEL), source.id
        seen.add(source.id)
    assert seen == set(REFERENCE_EXPECTED)


BASE_ENV = {"MQTT_HOST": "broker", "DB_HOST": "db", "DB_USER": "pompa"}


def test_config_defaults():
    s = load_settings(BASE_ENV)
    assert s.mqtt_client_id == "pompa-next"
    assert s.db_name == "pompa_next"
    assert s.api_port == 8001
    assert s.stale_after_seconds == 600
    assert s.write_buffer_rows == 60
    assert s.mqtt_topic_prefix == "panasonic_heat_pump"


def test_config_overrides():
    s = load_settings({**BASE_ENV, "MQTT_TOPIC_PREFIX": "hp", "STALE_AFTER_SECONDS": "300",
                       "API_PORT": "9000", "MQTT_CLIENT_ID": "x"})
    assert (s.mqtt_topic_prefix, s.stale_after_seconds, s.api_port, s.mqtt_client_id) == ("hp", 300, 9000, "x")


@pytest.mark.parametrize("override", [
    {"MQTT_HOST": ""},
    {"STALE_AFTER_SECONDS": "abc"},
    {"STALE_AFTER_SECONDS": "10"},
    {"WRITE_BUFFER_ROWS": "0"},
    {"MQTT_TOPIC_PREFIX": "hp/#"},
    {"MQTT_TOPIC_PREFIX": "hp/"},
    {"LOG_LEVEL": "LOUD"},
])
def test_config_rejects_invalid(override):
    with pytest.raises(ConfigError):
        load_settings({**BASE_ENV, **override})


# ------------------------------------------------------------------ Stage 4B checkpoint B:
# shared numeric-parsing primitive (docs/ARCHITECTURE.md §25.2.1). ``parse_value`` is now a thin
# wrapper over ``parse_numeric``; these cases prove the extraction changed nothing.

SENTINELS = frozenset({-78.0, -128.0})


@pytest.mark.parametrize("payload, expected", [
    ("0", (0.0, Outcome.VALID)),
    ("-12.5", (-12.5, Outcome.VALID)),
    ("-78", (None, Outcome.SENTINEL)),
    ("-128", (None, Outcome.SENTINEL)),
    ("-200", (None, Outcome.REJECTED)),  # below min_value, not a sentinel of this set
    ("100", (None, Outcome.REJECTED)),  # above max_value
    ("nan", (None, Outcome.REJECTED)),
    ("inf", (None, Outcome.REJECTED)),
    ("-inf", (None, Outcome.REJECTED)),
    ("not a number", (None, Outcome.REJECTED)),
    ("-50", (-50.0, Outcome.VALID)),  # lower boundary equality
    ("50", (50.0, Outcome.VALID)),  # upper boundary equality
])
def test_parse_numeric_primitive(payload, expected):
    assert parse_numeric(payload, SENTINELS, -50.0, 50.0) == expected


def test_canonical_parse_value_is_exactly_parse_numeric():
    """``parse_value`` must be byte-for-byte equivalent to calling the primitive directly."""
    metric = METRICS_BY_KEY["outside_temp"]
    source = next(s for s in metric.sources if s.id == "TOP14")
    for payload in ("0", "-12.5", "-78", "-128", "nan", "inf", "not a number", "23.999999"):
        assert parse_value(metric, source, payload) == parse_numeric(
            payload, source.sentinels, metric.min_value, metric.max_value)
