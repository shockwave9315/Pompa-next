import json
from dataclasses import FrozenInstanceError

import pytest

from conftest import REPO_ROOT
from pompa.capabilities import TypedPayload, normalize_payload
from pompa.catalog import METRICS_BY_KEY, Outcome, parse_value


@pytest.mark.parametrize(("raw", "expected"), [
    ("0", 0), ("1", 1), ("-1", -1), ("+4", 4),
    ("23", 23), ("23.5", 23.5), ("-2.5", -2.5),
    ("-78", -78), ("-128", -128), ("-200", -200),
    ("0.13", 0.13), ("1e3", 1000.0), ("-2.5e-2", -0.025),
    (".5", 0.5), ("23.", 23.0),
])
def test_unambiguous_finite_decimal_is_number(raw, expected):
    result = normalize_payload(raw)
    assert result == TypedPayload(raw=raw, value=expected, kind="number")
    assert isinstance(result.value, (int, float))
    json.dumps(result.value, allow_nan=False)


@pytest.mark.parametrize("raw", [
    "NaN", "nan", "Infinity", "+Infinity", "-inf", "0x10",
    "1_000", "1,2", "1e309", "1e", ".", "--1", "Water",
])
def test_ambiguous_or_nonfinite_number_remains_text(raw):
    assert normalize_payload(raw) == TypedPayload(raw=raw, value=raw, kind="text")


def test_outer_whitespace_is_trimmed_without_changing_raw_or_internal_text():
    assert normalize_payload(" \t-2.5e-2\n") == TypedPayload(
        raw=" \t-2.5e-2\n", value=-0.025, kind="number"
    )
    model = "  E2 D5 0B 08 95 02 D6 0F 68 95  "
    assert normalize_payload(model) == TypedPayload(
        raw=model, value="E2 D5 0B 08 95 02 D6 0F 68 95", kind="text"
    )


@pytest.mark.parametrize("raw", ["", "  \t\n  "])
def test_empty_payload_is_text_not_an_unknown_state(raw):
    assert normalize_payload(raw) == TypedPayload(raw=raw, value="", kind="text")


def test_result_is_immutable():
    with pytest.raises(FrozenInstanceError):
        normalize_payload("0").value = 1


def test_tracked_device_payload_examples():
    rows = {}
    for line in (REPO_ROOT / "docs/reference/heishamon/realne_dane.md").read_text(
        encoding="utf-8"
    ).splitlines()[1:]:
        identity, _, raw, _ = line.split("\t", 3)
        rows[identity] = raw
    expected = {
        "TOP1": (0.13, "number"),
        "TOP44": ("No error", "text"),
        "TOP92": ("E2 D5 0B 08 95 02 D6 0F 68 95", "text"),
        "TOP115": (1.7, "number"),
        "XTOP0": (18, "number"),
        "TOP15": (-200, "number"),
    }
    for identity, (value, kind) in expected.items():
        result = normalize_payload(rows[identity])
        assert (result.raw, result.value, result.kind) == (rows[identity], value, kind)


@pytest.mark.parametrize(("key", "source_id", "raw", "expected"), [
    ("outside_temp", "TOP14", "-78", (None, Outcome.SENTINEL)),
    ("main_inlet_temp", "TOP5", "-128", (None, Outcome.SENTINEL)),
    ("co_power_production", "TOP15", "-200", (None, Outcome.SENTINEL)),
    ("heatpump_state", "TOP0", "-1", (None, Outcome.SENTINEL)),
    ("pump_flow", "TOP1", "0", (0.0, Outcome.VALID)),
    ("heatpump_state", "TOP0", "0", (0.0, Outcome.VALID)),
    ("heatpump_state", "TOP0", "2", (None, Outcome.REJECTED)),
    ("pump_flow", "TOP1", "-0.5", (None, Outcome.REJECTED)),
    ("main_outlet_temp", "TOP6", "No error", (None, Outcome.REJECTED)),
])
def test_existing_canonical_parser_keeps_its_semantics(key, source_id, raw, expected):
    metric = METRICS_BY_KEY[key]
    source = next(source for source in metric.sources if source.id == source_id)
    assert parse_value(metric, source, raw) == expected


def test_physical_fact_and_canonical_sentinel_are_distinct():
    metric = METRICS_BY_KEY["co_power_production"]
    top = next(source for source in metric.sources if source.id == "TOP15")
    assert normalize_payload("-200") == TypedPayload("-200", -200, "number")
    assert parse_value(metric, top, "-200") == (None, Outcome.SENTINEL)
