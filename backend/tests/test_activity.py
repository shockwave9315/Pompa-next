"""Stage 4C-A activity domain truth: classification, segments, spans, boundaries, summaries.

Pure; no database. Expected values are hand-derived constants or equality with
the existing canonical history algebra.
"""

import dataclasses
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import IDLE as IDLE_SNAPSHOT, RUNNING, T0, row
from pompa import activity as act
from pompa.activity import (
    ACTIVITY_RULE_VERSION, ENERGY_SERIES, Activity, Boundary, Compressor, Gap, activity_events,
    build_segments, classify, compressor_off_intervals, compressor_runs, defrosts, fold_energy,
    project, summarize, timeline,
)
from pompa.aggregation import combine_maps, cop, energy_kwh, fold_minutes
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.timegrid import HOUR, expected_minutes, local_date, local_midnight

M = 60
assert T0 % HOUR == 0

# Minute shapes. Unspecified metrics are NULL (unknown), never zero.
CO = dict(compressor_freq=40.0, defrosting_state=0.0, heatpump_state=1.0, three_way_valve=0.0)
DHW = {**CO, "three_way_valve": 1.0}
TRANSITION = {**CO, "three_way_valve": 0.5}
IDLE = dict(compressor_freq=0.0, defrosting_state=0.0, heatpump_state=1.0, three_way_valve=0.0)
OFF = {**IDLE, "heatpump_state": 0.0}
UNKNOWN = {**CO, "compressor_freq": None}
DEFROST = {**CO, "defrosting_state": 1.0}
GAP = None  # no row at all


def rows_of(start, *shapes):
    """Consecutive minutes from ``start``; ``GAP`` leaves the minute without a row."""
    out = []
    for i, shape in enumerate(shapes):
        if shape is not None:
            r = row(start + i * M, **shape)
            out.append((r.ts, r.values))
    return out


def tl_of(rows, start, end, closed_until=None):
    return timeline(build_segments(rows), start, end, end + HOUR if closed_until is None else closed_until)


def spans(items):
    return [(s.state, (s.start - T0) // M, s.minutes) for s in items]


# ------------------------------------------------------------------ classification

POWER = dict(co_power_consumption=900.0, co_power_production=3600.0,
             dhw_power_consumption=0.0, dhw_power_production=0.0)
DHW_POWER = dict(co_power_consumption=0.0, co_power_production=0.0,
                 dhw_power_consumption=1500.0, dhw_power_production=4500.0)
BOTH_POWER = dict(co_power_consumption=400.0, co_power_production=1200.0,
                  dhw_power_consumption=600.0, dhw_power_production=1800.0)
NO_POWER = dict(co_power_consumption=100.0, co_power_production=18.0,
                dhw_power_consumption=0.0, dhw_power_production=100.0)
NO_VALVE = {**CO, "three_way_valve": None}

CLASSIFICATION = [
    ("defrost beats valve", {**DHW, "defrosting_state": 1.0}, Activity.DEFROST, Compressor.ON),
    ("defrost beats power", {**NO_VALVE, **DHW_POWER, "defrosting_state": 0.25}, Activity.DEFROST,
     Compressor.ON),
    ("defrost beats unknown compressor", {**DEFROST, "compressor_freq": None}, Activity.DEFROST,
     Compressor.UNKNOWN),
    ("defrost with stopped compressor", {**DEFROST, "compressor_freq": 0.0}, Activity.DEFROST,
     Compressor.OFF),
    ("unknown defrost is not excluded", {**CO, "defrosting_state": None}, Activity.UNKNOWN,
     Compressor.ON),
    ("compressor NULL", UNKNOWN, Activity.UNKNOWN, Compressor.UNKNOWN),
    ("compressor NULL, heat pump off", {**OFF, "compressor_freq": None}, Activity.UNKNOWN,
     Compressor.UNKNOWN),
    ("compressor 0, heat pump 0", OFF, Activity.OFF, Compressor.OFF),
    ("compressor 0, heat pump 1", IDLE, Activity.IDLE, Compressor.OFF),
    ("compressor 0, heat pump part of minute", {**IDLE, "heatpump_state": 0.5}, Activity.IDLE,
     Compressor.OFF),
    ("compressor 0, heat pump unknown", {**IDLE, "heatpump_state": None}, Activity.UNKNOWN,
     Compressor.OFF),
    ("valve 0", CO, Activity.CO, Compressor.ON),
    ("valve 1", DHW, Activity.DHW, Compressor.ON),
    ("fractional valve", TRANSITION, Activity.TRANSITION, Compressor.ON),
    ("nearly-CWU valve is still a transition", {**CO, "three_way_valve": 0.999999},
     Activity.TRANSITION, Compressor.ON),
    ("running part of the minute", {**CO, "compressor_freq": 0.000001}, Activity.CO, Compressor.ON),
    ("valve NULL, CO power", {**NO_VALVE, **POWER}, Activity.CO, Compressor.ON),
    ("valve NULL, DHW power", {**NO_VALVE, **DHW_POWER}, Activity.DHW, Compressor.ON),
    ("valve NULL, both", {**NO_VALVE, **BOTH_POWER}, Activity.TRANSITION, Compressor.ON),
    ("valve NULL, no evidence (100 W is not above 100 W)", {**NO_VALVE, **NO_POWER},
     Activity.UNKNOWN, Compressor.ON),
    ("valve NULL, all power unknown", NO_VALVE, Activity.UNKNOWN, Compressor.ON),
    ("valve NULL, CO power but DHW unknown", {**NO_VALVE, **POWER, "dhw_power_consumption": None},
     Activity.UNKNOWN, Compressor.ON),
    ("valve NULL, one CO channel suffices", {**NO_VALVE, **POWER, "co_power_production": None},
     Activity.CO, Compressor.ON),
    ("valve NULL, DHW power but CO unknown", {**NO_VALVE, **DHW_POWER, "co_power_production": None},
     Activity.UNKNOWN, Compressor.ON),
    ("valve alone with compressor off", {**IDLE, "three_way_valve": 1.0}, Activity.IDLE,
     Compressor.OFF),
    ("valve and DHW power tail with compressor off", {**IDLE, **DHW_POWER, "three_way_valve": 1.0},
     Activity.IDLE, Compressor.OFF),
    ("CO power tail with compressor off", {**IDLE, **POWER}, Activity.IDLE, Compressor.OFF),
    ("valve alone with heat pump off", {**OFF, "three_way_valve": 1.0}, Activity.OFF,
     Compressor.OFF),
]


@pytest.mark.parametrize("values, activity, compressor",
                         [c[1:] for c in CLASSIFICATION], ids=[c[0] for c in CLASSIFICATION])
def test_classification(values, activity, compressor):
    assert classify(row(T0, **values).values) == (activity, compressor)


def test_unread_column_is_not_an_unknown_metric():
    values = dict(row(T0, **CO).values)
    del values["three_way_valve"]
    with pytest.raises(ValueError):
        classify(values)
    with pytest.raises(ValueError):
        build_segments([(T0, values)])
    assert set(act.ACTIVITY_COLUMNS) <= set(row(T0).values)


def test_operations_counter_and_mode_never_classify():
    for shape in (CO, IDLE, OFF, UNKNOWN, DEFROST, NO_VALVE):
        base = classify(row(T0, **shape).values)
        for counter in (None, 0.0, 7651.0, 7652.0):
            for mode in (None, 0.0, 4.0):
                values = row(T0, **shape, operations_counter=counter, operating_mode=mode).values
                assert classify(values) == base


# ------------------------------------------------------------------ version-1 golden

# Independent, literal meaning of ACTIVITY_RULE_VERSION = 1. Nothing below is derived from the
# implementation or from the tests above. 4C-B persists segments under this version, so a
# failure here means stored history would silently change meaning: restore the old behaviour,
# or bump ACTIVITY_RULE_VERSION and add a new golden next to this one. Never edit it in place.
N = None
GOLDEN_V1_COLUMNS = ("compressor_freq", "defrosting_state", "heatpump_state", "three_way_valve",
                     "co_power_consumption", "co_power_production",
                     "dhw_power_consumption", "dhw_power_production")
GOLDEN_V1_CLASSIFICATION = (
    ((40.0, 1.0, 1.0, 1.0, N, N, N, N), ("defrost", "on")),
    ((40.0, 0.25, 1.0, N, 0.0, 0.0, 1500.0, 4500.0), ("defrost", "on")),
    ((N, 1.0, 1.0, 0.0, N, N, N, N), ("defrost", "unknown")),
    ((0.0, 0.5, 1.0, 0.0, N, N, N, N), ("defrost", "off")),
    ((40.0, N, 1.0, 0.0, 900.0, 3600.0, 0.0, 0.0), ("unknown", "on")),
    ((0.0, N, 0.0, 0.0, N, N, N, N), ("unknown", "off")),
    ((N, 0.0, 1.0, 0.0, 900.0, 3600.0, 0.0, 0.0), ("unknown", "unknown")),
    ((N, 0.0, 0.0, 0.0, N, N, N, N), ("unknown", "unknown")),
    ((0.0, 0.0, 0.0, 0.0, 900.0, 3600.0, 0.0, 0.0), ("off", "off")),
    ((0.0, 0.0, 0.0, 1.0, N, N, N, N), ("off", "off")),
    ((0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1500.0, 4500.0), ("idle", "off")),
    ((0.0, 0.0, 0.5, 0.0, N, N, N, N), ("idle", "off")),
    ((0.0, 0.0, N, 0.0, N, N, N, N), ("unknown", "off")),
    ((40.0, 0.0, 1.0, 0.0, N, N, N, N), ("co", "on")),
    ((40.0, 0.0, 0.0, 0.0, N, N, N, N), ("co", "on")),
    ((0.000001, 0.0, 1.0, 0.0, N, N, N, N), ("co", "on")),
    ((40.0, 0.0, 1.0, 1.0, 900.0, 3600.0, 0.0, 0.0), ("dhw", "on")),
    ((40.0, 0.0, 1.0, 0.5, N, N, N, N), ("transition", "on")),
    ((40.0, 0.0, 1.0, 0.999999, N, N, N, N), ("transition", "on")),
    ((40.0, 0.0, 1.0, N, 900.0, 3600.0, 0.0, 0.0), ("co", "on")),
    ((40.0, 0.0, 1.0, N, 100.01, N, 0.0, 0.0), ("co", "on")),
    ((40.0, 0.0, 1.0, N, 0.0, 0.0, 1500.0, 4500.0), ("dhw", "on")),
    ((40.0, 0.0, 1.0, N, 400.0, 1200.0, 600.0, 1800.0), ("transition", "on")),
    ((40.0, 0.0, 1.0, N, 400.0, N, 600.0, N), ("transition", "on")),
    ((40.0, 0.0, 1.0, N, 100.0, 18.0, 0.0, 100.0), ("unknown", "on")),
    ((40.0, 0.0, 1.0, N, 900.0, 3600.0, N, 0.0), ("unknown", "on")),
    ((40.0, 0.0, 1.0, N, 0.0, N, 1500.0, 4500.0), ("unknown", "on")),
    ((40.0, 0.0, 1.0, N, N, N, N, N), ("unknown", "on")),
)
# Consecutive minutes from one hour's last 5 minutes; (minute, values, operations_counter), and
# the minute-8 gap. Minutes may share a segment only with equal activity, compressor state and
# exact defrost fraction, and never across a UTC hour or a missing minute.
GOLDEN_V1_SEGMENT_INPUT = (
    (0, (40.0, 0.0, 1.0, 0.0, 900.0, 3600.0, 0.0, 0.0), 10.0),
    (1, (30.0, 0.0, 0.5, 0.0, 800.0, 3000.0, N, 0.0), 11.0),  # frequency/state/power/counter differ
    (2, (40.0, 0.0, 1.0, 0.0, 700.0, 2800.0, 10.0, 0.0), 12.0),
    (3, (40.0, 0.0, 1.0, 1.0, N, N, N, N), 12.0),
    (4, (40.0, 0.0, 1.0, 1.0, N, N, N, N), 12.0),
    (5, (40.0, 0.0, 1.0, 1.0, N, N, N, N), 12.0),  # next UTC hour
    (6, (40.0, 0.5, 1.0, 0.0, N, N, N, N), 12.0),
    (7, (40.0, 0.5, 1.0, 1.0, N, N, N, N), 12.0),  # equal fraction, different valve
    (9, (40.0, 1.0, 1.0, 0.0, N, N, N, N), 12.0),
    (10, (40.0, 0.583333, 1.0, 0.0, N, N, N, N), 12.0),  # only the fraction differs
    (11, (0.0, 0.583333, 1.0, 0.0, N, N, N, N), 12.0),  # only the compressor differs
    (12, (0.0, 0.0, 1.0, 0.0, N, N, N, N), 12.0),
    (13, (0.0, 0.0, 0.25, 1.0, N, N, N, N), 12.0),
    (14, (0.0, 0.0, 0.0, 1.0, N, N, N, N), 12.0),
    (15, (N, 0.0, 1.0, 0.0, N, N, N, N), 12.0),
    (16, (40.0, N, 1.0, 0.0, N, N, N, N), 12.0),
    (17, (40.0, 0.0, 1.0, N, N, N, N, N), 12.0),  # unknown/on again; only the fraction differs
)
GOLDEN_V1 = {
    "activity_rule_version": 1,
    "power_active_threshold_w": 100.0,
    "activity_values": frozenset({"off", "idle", "co", "dhw", "transition", "defrost", "unknown"}),
    "compressor_values": frozenset({"off", "on", "unknown"}),
    "activity_columns": frozenset(GOLDEN_V1_COLUMNS),
    "energy_series": ("co_power_consumption", "co_power_production", "dhw_power_consumption",
                      "dhw_power_production", "pair_co_in", "pair_co_out", "pair_dhw_in",
                      "pair_dhw_out", "pair_total_in", "pair_total_out"),
    "segment_fields": ("start", "minutes", "activity", "compressor", "defrost_fraction", "energy"),
    "classification": tuple(expected for _, expected in GOLDEN_V1_CLASSIFICATION),
    # (first minute, minutes, activity, compressor, defrost_fraction)
    "segments": (
        (0, 3, "co", "on", 0.0),
        (3, 2, "dhw", "on", 0.0),
        (5, 1, "dhw", "on", 0.0),
        (6, 2, "defrost", "on", 0.5),
        (9, 1, "defrost", "on", 1.0),
        (10, 1, "defrost", "on", 0.583333),
        (11, 1, "defrost", "off", 0.583333),
        (12, 2, "idle", "off", 0.0),
        (14, 1, "off", "off", 0.0),
        (15, 1, "unknown", "unknown", 0.0),
        (16, 1, "unknown", "on", N),
        (17, 1, "unknown", "on", 0.0),
    ),
    # Energy ingredients of the first segment: series -> (n, sum, min, max, last).
    "first_segment_energy": {
        "co_power_consumption": (3, 2400.0, 700.0, 900.0, 700.0),
        "co_power_production": (3, 9400.0, 2800.0, 3600.0, 2800.0),
        "dhw_power_consumption": (2, 10.0, 0.0, 10.0, 10.0),
        "dhw_power_production": (3, 0.0, 0.0, 0.0, 0.0),
        "pair_co_in": (3, 2400.0, 700.0, 900.0, 700.0),
        "pair_co_out": (3, 9400.0, 2800.0, 3600.0, 2800.0),
        "pair_dhw_in": (2, 10.0, 0.0, 10.0, 10.0),
        "pair_dhw_out": (2, 0.0, 0.0, 0.0, 0.0),
        "pair_total_in": (2, 1610.0, 710.0, 900.0, 710.0),
        "pair_total_out": (2, 6400.0, 2800.0, 3600.0, 2800.0),
    },
}


def _golden_values(literal, counter=None):
    return row(T0, **dict(zip(GOLDEN_V1_COLUMNS, literal)), operations_counter=counter).values


def _implemented_v1_meaning():
    start = T0 + HOUR - 5 * M
    rows = [(start + minute * M, _golden_values(literal, counter))
            for minute, literal, counter in GOLDEN_V1_SEGMENT_INPUT]
    segments = act.build_segments(rows)
    return {
        "activity_rule_version": ACTIVITY_RULE_VERSION,
        "power_active_threshold_w": act.POWER_ACTIVE_THRESHOLD_W,
        "activity_values": frozenset(a.value for a in Activity),
        "compressor_values": frozenset(c.value for c in Compressor),
        "activity_columns": frozenset(act.ACTIVITY_COLUMNS),
        "energy_series": act.ENERGY_SERIES,
        "segment_fields": tuple(f.name for f in dataclasses.fields(act.ActivitySegment)),
        "classification": tuple(tuple(v.value for v in act.classify(_golden_values(literal)))
                                for literal, _ in GOLDEN_V1_CLASSIFICATION),
        "segments": tuple(((s.start - start) // M, s.minutes, s.activity.value, s.compressor.value,
                           s.defrost_fraction) for s in segments),
        "first_segment_energy": {k: (v.n, v.sum, v.min, v.max, v.last)
                                 for k, v in segments[0].energy.items()},
    }


def test_version_1_golden_is_pinned():
    observed = _implemented_v1_meaning()
    assert observed.keys() == GOLDEN_V1.keys()
    for key, expected in GOLDEN_V1.items():
        assert observed[key] == expected, (
            f"{key!r} changed while ACTIVITY_RULE_VERSION == 1: persisted segments would change "
            "meaning; bump the version and add a new golden instead")


@pytest.mark.parametrize("mutation", ["classification", "energy_series", "segment_key",
                                      "hour_split", "threshold"])
def test_version_1_golden_detects_semantic_drift(monkeypatch, mutation):
    """The golden is not tautological: each kind of unversioned change it guards fails it."""
    if mutation == "classification":  # e.g. fractional heat-pump state with a stopped compressor
        original = act.classify
        monkeypatch.setattr(act, "classify", lambda v: (Activity.OFF, Compressor.OFF)
                            if v["heatpump_state"] == 0.5 and v["compressor_freq"] == 0
                            else original(v))
    elif mutation == "energy_series":
        monkeypatch.setattr(act, "ENERGY_SERIES", act.ENERGY_SERIES[:-2])
    elif mutation == "segment_key":  # grouping that ignores the exact defrost fraction
        original = act.build_segments

        def merged(rows):
            out = []
            for seg in original(rows):
                last = out[-1] if out else None
                if last and last.end == seg.start and last.start // HOUR == seg.start // HOUR \
                        and (last.activity, last.compressor) == (seg.activity, seg.compressor):
                    out[-1] = dataclasses.replace(last, minutes=last.minutes + seg.minutes)
                else:
                    out.append(seg)
            return out

        monkeypatch.setattr(act, "build_segments", merged)
    elif mutation == "hour_split":
        monkeypatch.setattr(act, "floor_hour", lambda t: 0)
    else:
        monkeypatch.setattr(act, "POWER_ACTIVE_THRESHOLD_W", 50.0)
    observed = _implemented_v1_meaning()
    assert [k for k, v in GOLDEN_V1.items() if observed[k] != v]


# ------------------------------------------------------------------ segments

def test_segments_break_at_hour_gap_and_interpretation_only():
    start = T0 + HOUR - 3 * M
    rows = rows_of(start, CO, CO, CO, CO, CO, GAP, CO, DHW, DHW)
    segments = build_segments(rows)
    assert [((s.start - start) // M, s.minutes, s.activity) for s in segments] == [
        (0, 3, Activity.CO), (3, 2, Activity.CO), (6, 1, Activity.CO), (7, 2, Activity.DHW)]
    assert all(s.start // HOUR == (s.end - M) // HOUR for s in segments)


def test_hour_local_build_equals_whole_build():
    rng = random.Random(4)
    shapes = [rng.choice([CO, DHW, IDLE, OFF, UNKNOWN, DEFROST, TRANSITION, GAP]) for _ in range(300)]
    rows = rows_of(T0 + 17 * M, *shapes)
    per_hour = []
    for hour in sorted({ts // HOUR for ts, _ in rows}):
        per_hour += build_segments([r for r in rows if r[0] // HOUR == hour])
    assert per_hour == build_segments(rows)


def test_segments_reject_unordered_or_unaligned_minutes():
    with pytest.raises(ValueError):
        build_segments(rows_of(T0, CO, CO)[::-1])
    with pytest.raises(ValueError):
        build_segments([(T0 + 1, row(T0, **CO).values)])


# ------------------------------------------------------------------ no smoothing

MIDDLES = {"idle": IDLE, "gap": GAP, "unknown": UNKNOWN, "dhw": DHW, "defrost": DEFROST,
           "transition": TRANSITION, "off": OFF}


@pytest.mark.parametrize("length", [1, 2, 3, 4, 5, 6, 10])
@pytest.mark.parametrize("middle", MIDDLES)
def test_no_smoothing_or_bridging(middle, length):
    shapes = [CO] * 10 + [MIDDLES[middle]] * length + [CO] * 10
    tl = tl_of(rows_of(T0, *shapes), T0, T0 + len(shapes) * M)
    events = activity_events(tl)
    if middle == "gap":
        assert [(s.state, s.minutes) for s in events] == [(Activity.CO, 10), (Activity.CO, 10)]
        assert [(i.start - T0, i.minutes) for i in tl.items if isinstance(i, Gap)] == [(600, length)]
        assert (events[0].end_boundary, events[1].start_boundary) == (Boundary.GAP, Boundary.GAP)
    else:
        middle_activity = classify(row(T0, **MIDDLES[middle]).values)[0]
        assert [(s.state, s.minutes) for s in events] == [
            (Activity.CO, 10), (middle_activity, length), (Activity.CO, 10)]
    if middle in ("defrost",):
        assert [(d.minutes, d.observed_defrost_seconds) for d in defrosts(tl)] == [(length, 60.0 * length)]
    runs = compressor_runs(tl)
    if middle in ("dhw", "defrost", "transition"):  # compressor observed on throughout
        assert [(r.minutes, r.start_boundary, r.end_boundary) for r in runs] == [
            (20 + length, Boundary.OUTSIDE_EVIDENCE, Boundary.OUTSIDE_EVIDENCE)]
    else:
        assert [r.minutes for r in runs] == [10, 10]


DEFROST_UNKNOWN = {**CO, "defrosting_state": None}


def test_separate_defrosts_stay_separate():
    shapes = [CO, DEFROST, DEFROST, CO, DEFROST, GAP, DEFROST, DEFROST_UNKNOWN, DEFROST, UNKNOWN, DEFROST]
    tl = tl_of(rows_of(T0, *shapes), T0, T0 + len(shapes) * M)
    assert [((d.start - T0) // M, d.minutes, d.start_boundary, d.end_boundary) for d in defrosts(tl)] == [
        (1, 2, Boundary.OBSERVED, Boundary.OBSERVED),
        (4, 1, Boundary.OBSERVED, Boundary.GAP),
        (6, 1, Boundary.GAP, Boundary.UNKNOWN),
        (8, 1, Boundary.UNKNOWN, Boundary.OBSERVED),  # unknown compressor, known defrost 0
        (10, 1, Boundary.OBSERVED, Boundary.OUTSIDE_EVIDENCE),
    ]


# Neighbours whose overall activity is unknown although the defrost signal is a known 0.
DEFROST_ZERO_UNKNOWN_ACTIVITY = {
    "unknown compressor": {**CO, "compressor_freq": None},
    "unknown valve, no power": {**CO, "three_way_valve": None},
    "unknown valve, one power side unknown": {**CO, **POWER, "three_way_valve": None,
                                              "dhw_power_production": None},
    "stopped compressor, unknown heat pump state": {**IDLE, "heatpump_state": None},
}


@pytest.mark.parametrize("neighbour", DEFROST_ZERO_UNKNOWN_ACTIVITY.values(),
                         ids=DEFROST_ZERO_UNKNOWN_ACTIVITY.keys())
def test_defrost_edges_are_proven_by_the_defrost_signal(neighbour):
    assert classify(row(T0, **neighbour).values)[0] is Activity.UNKNOWN
    tl = tl_of(rows_of(T0, neighbour, DEFROST, DEFROST, neighbour), T0, T0 + 4 * M)
    [d] = defrosts(tl)
    assert (d.start_boundary, d.end_boundary, d.start_observed, d.end_observed) == (
        Boundary.OBSERVED, Boundary.OBSERVED, True, True)
    # The activity timeline still reports the change to an unknown activity truthfully.
    [event] = [e for e in activity_events(tl) if e.state is Activity.DEFROST]
    assert (event.start_boundary, event.end_boundary) == (Boundary.UNKNOWN, Boundary.UNKNOWN)
    # Compressor-run edges are untouched: an unknown compressor remains unknown.
    [run] = compressor_runs(tl)
    expected = Boundary.UNKNOWN if neighbour["compressor_freq"] is None else (
        Boundary.OBSERVED if neighbour["compressor_freq"] == 0 else Boundary.OUTSIDE_EVIDENCE)
    assert run.start_boundary is expected


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("neighbour, boundary", [
    (DEFROST_UNKNOWN, Boundary.UNKNOWN), ({**UNKNOWN, "defrosting_state": None}, Boundary.UNKNOWN),
    (GAP, Boundary.GAP), (CO, Boundary.OBSERVED), (OFF, Boundary.OBSERVED),
])
def test_defrost_edge_evidence_on_each_side(side, neighbour, boundary):
    shapes = [neighbour, DEFROST, CO] if side == "before" else [CO, DEFROST, neighbour]
    tl = tl_of(rows_of(T0, *shapes), T0, T0 + 3 * M)
    [d] = defrosts(tl)
    assert (d.start_boundary if side == "before" else d.end_boundary) is boundary
    assert (d.end_boundary if side == "before" else d.start_boundary) is Boundary.OBSERVED


def test_defrost_right_edge_open_and_outside_evidence():
    open_tl = tl_of(rows_of(T0, CO, DEFROST), T0, T0 + HOUR, closed_until=T0 + 2 * M)
    assert defrosts(open_tl)[0].end_boundary is Boundary.OPEN
    window_tl = tl_of(rows_of(T0, DEFROST, DEFROST), T0, T0 + 2 * M)
    assert (defrosts(window_tl)[0].start_boundary, defrosts(window_tl)[0].end_boundary) == (
        Boundary.OUTSIDE_EVIDENCE, Boundary.OUTSIDE_EVIDENCE)


# ------------------------------------------------------------------ runs, starts, ends

@pytest.mark.parametrize("before, boundary", [
    (OFF, Boundary.OBSERVED), (IDLE, Boundary.OBSERVED), (UNKNOWN, Boundary.UNKNOWN),
    (GAP, Boundary.GAP), ({**IDLE, "defrosting_state": None}, Boundary.OBSERVED),
])
def test_run_start_evidence(before, boundary):
    tl = tl_of(rows_of(T0, IDLE, before, CO, CO, CO, IDLE), T0, T0 + 6 * M)
    [run] = compressor_runs(tl)
    assert (run.start - T0, run.minutes, run.start_boundary, run.start_observed) == (
        120, 3, boundary, boundary is Boundary.OBSERVED)
    assert summarize(tl, T0, T0 + 6 * M).observed_starts == (1 if boundary is Boundary.OBSERVED else 0)


@pytest.mark.parametrize("after, boundary", [
    (OFF, Boundary.OBSERVED), (IDLE, Boundary.OBSERVED), (UNKNOWN, Boundary.UNKNOWN), (GAP, Boundary.GAP),
])
def test_run_end_evidence(after, boundary):
    tl = tl_of(rows_of(T0, IDLE, CO, CO, CO, after, IDLE), T0, T0 + 6 * M)
    [run] = compressor_runs(tl)
    assert (run.end - T0, run.minutes, run.end_boundary, run.end_observed) == (
        240, 3, boundary, boundary is Boundary.OBSERVED)
    s = summarize(tl, T0, T0 + 6 * M)
    assert s.observed_stops == (1 if boundary is Boundary.OBSERVED else 0)
    assert s.complete_run_minutes == ((3,) if boundary is Boundary.OBSERVED else ())


def test_unknown_compressor_is_neither_off_nor_on():
    tl = tl_of(rows_of(T0, OFF, UNKNOWN, CO, UNKNOWN, OFF, CO, OFF), T0, T0 + 7 * M)
    assert [(r.start - T0, r.minutes, r.start_boundary, r.end_boundary) for r in compressor_runs(tl)] == [
        (120, 1, Boundary.UNKNOWN, Boundary.UNKNOWN), (300, 1, Boundary.OBSERVED, Boundary.OBSERVED)]
    s = summarize(tl, T0, T0 + 7 * M)
    assert (s.observed_starts, s.observed_stops) == (1, 1)
    assert s.compressor_minutes == {"off": 3, "on": 2, "unknown": 2}


def test_evidence_window_edges_are_not_starts_or_stops():
    tl = tl_of(rows_of(T0, CO, CO, CO), T0, T0 + 3 * M)
    [run] = compressor_runs(tl)
    assert (run.start_boundary, run.end_boundary) == (Boundary.OUTSIDE_EVIDENCE, Boundary.OUTSIDE_EVIDENCE)
    s = summarize(tl, T0, T0 + 3 * M)
    assert (s.observed_starts, s.observed_stops, s.compressor_runs_overlapping, s.complete_run_minutes) == (0, 0, 1, ())


def test_run_spans_activity_and_defrost_but_events_do_not():
    tl = tl_of(rows_of(T0, OFF, CO, CO, DEFROST, DEFROST, CO, DHW, TRANSITION, CO, OFF), T0, T0 + 10 * M)
    [run] = compressor_runs(tl)
    assert (run.start - T0, run.minutes, run.start_observed, run.end_observed) == (60, 8, True, True)
    assert spans(activity_events(tl)) == [
        (Activity.OFF, 0, 1), (Activity.CO, 1, 2), (Activity.DEFROST, 3, 2), (Activity.CO, 5, 1),
        (Activity.DHW, 6, 1), (Activity.TRANSITION, 7, 1), (Activity.CO, 8, 1), (Activity.OFF, 9, 1)]


def test_defrost_with_stopped_compressor_splits_the_run():
    stopped = {**DEFROST, "compressor_freq": 0.0}
    tl = tl_of(rows_of(T0, OFF, CO, DEFROST, stopped, DEFROST, CO, OFF), T0, T0 + 7 * M)
    assert [(r.start - T0, r.minutes, r.start_observed, r.end_observed) for r in compressor_runs(tl)] == [
        (60, 2, True, True), (240, 2, True, True)]
    [d] = defrosts(tl)
    assert (d.start - T0, d.minutes) == (120, 3)


def test_operations_counter_is_not_start_truth():
    shapes = [{**OFF, "operations_counter": 10.0}, {**CO, "operations_counter": 10.0},
              {**OFF, "operations_counter": 10.0}, {**CO, "operations_counter": 10.0},
              {**OFF, "operations_counter": 13.0}, {**OFF, "operations_counter": 14.0}]
    s = summarize(tl_of(rows_of(T0, *shapes), T0, T0 + 6 * M), T0, T0 + 6 * M)
    assert (s.observed_starts, s.observed_stops) == (2, 2)  # counter moved by 4 and never with a start


def test_off_intervals_are_exact_only_between_observed_runs():
    shapes = [CO, OFF, OFF, OFF, CO, OFF, GAP, OFF, CO, OFF, UNKNOWN, OFF, CO, IDLE, IDLE]
    tl = tl_of(rows_of(T0, *shapes), T0, T0 + len(shapes) * M)
    assert [((i.start - T0) // M, i.minutes, i.start_boundary, i.end_boundary)
            for i in compressor_off_intervals(tl)] == [
        (1, 3, Boundary.OBSERVED, Boundary.OBSERVED),
        (5, 1, Boundary.OBSERVED, Boundary.GAP),
        (7, 1, Boundary.GAP, Boundary.OBSERVED),
        (9, 1, Boundary.OBSERVED, Boundary.UNKNOWN),
        (11, 1, Boundary.UNKNOWN, Boundary.OBSERVED),
        (13, 2, Boundary.OBSERVED, Boundary.OUTSIDE_EVIDENCE),
    ]
    assert summarize(tl, T0, T0 + len(shapes) * M).exact_off_interval_minutes == (3,)


# ------------------------------------------------------------------ gaps, current minute

def test_missing_row_is_a_gap_not_unknown():
    tl = tl_of(rows_of(T0, CO, GAP, GAP, UNKNOWN, CO), T0, T0 + 5 * M)
    assert [type(i).__name__ for i in tl.items] == ["ActivitySegment", "Gap", "ActivitySegment",
                                                    "ActivitySegment"]
    s = summarize(tl, T0, T0 + 5 * M)
    assert (s.closed_minutes, s.recorded_minutes, s.gap_minutes) == (5, 3, 2)
    assert s.activity_minutes["unknown"] == 1
    assert sum(s.activity_minutes.values()) == s.recorded_minutes


def test_current_run_stays_open_without_fake_trailing_gap():
    now = T0 + 5 * M + 25  # minute T0+5m is still open
    closed_until = T0 + 5 * M
    tl = tl_of(rows_of(T0, OFF, OFF, CO, CO, CO), T0, T0 + HOUR, closed_until)
    assert not any(isinstance(i, Gap) for i in tl.items)
    [run] = compressor_runs(tl)
    assert (run.start_observed, run.end_boundary, run.end_observed) == (True, Boundary.OPEN, False)
    s = summarize(tl, T0, T0 + HOUR)
    assert (s.closed_minutes, s.gap_minutes) == (5, 0)
    assert s.closed_minutes == expected_minutes(T0, T0 + HOUR, now)
    assert (s.observed_starts, s.observed_stops, s.complete_run_minutes) == (1, 0, ())
    [event] = [e for e in activity_events(tl) if e.state is Activity.CO]
    assert event.end_boundary is Boundary.OPEN


def test_historical_missing_minute_before_frontier_is_a_gap_not_open():
    closed_until = T0 + 6 * M
    tl = tl_of(rows_of(T0, OFF, CO, CO, CO, GAP), T0, T0 + HOUR, closed_until)
    [run] = compressor_runs(tl)
    assert run.end_boundary is Boundary.GAP
    assert tl.items[-1] == Gap(T0 + 4 * M, closed_until)


def test_evidence_window_end_before_frontier_is_outside_evidence():
    tl = tl_of(rows_of(T0, OFF, CO, CO), T0, T0 + 3 * M, closed_until=T0 + HOUR)
    assert compressor_runs(tl)[0].end_boundary is Boundary.OUTSIDE_EVIDENCE


def test_unclosed_minute_row_is_rejected():
    with pytest.raises(ValueError):
        tl_of(rows_of(T0, CO, CO), T0, T0 + HOUR, closed_until=T0 + M)


# ------------------------------------------------------------------ cross-hour and midnight

def test_run_and_event_cross_the_hour_without_persisted_state():
    start = T0 + HOUR - 3 * M
    rows = rows_of(start, OFF, CO, CO, CO, CO, CO, OFF)
    first_hour = build_segments([r for r in rows if r[0] < T0 + HOUR])
    second_hour = build_segments([r for r in rows if r[0] >= T0 + HOUR])
    tl = timeline(first_hour + second_hour, start, start + 7 * M, start + HOUR)
    [run] = compressor_runs(tl)
    assert (run.start, run.end, run.start_observed, run.end_observed) == (
        start + M, T0 + HOUR + 3 * M, True, True)
    assert len(run.segments) == 2
    assert [(e.state, e.minutes) for e in activity_events(tl)] == [
        (Activity.OFF, 1), (Activity.CO, 5), (Activity.OFF, 1)]


MIDNIGHT = local_midnight(date(2027, 1, 16))
DAY1 = (local_midnight(date(2027, 1, 15)), MIDNIGHT)
DAY2 = (MIDNIGHT, local_midnight(date(2027, 1, 17)))


def midnight(*scenario):
    """OFF around the scenario minutes 23:58, 23:59, 00:00 ... local."""
    before = (MIDNIGHT - 2 * M - DAY1[0]) // M
    after = (DAY2[1] - DAY1[0]) // M - before - len(scenario)
    shapes = [OFF] * before + list(scenario) + [OFF] * after
    tl = tl_of(rows_of(DAY1[0], *shapes), DAY1[0], DAY2[1])
    return tl, summarize(tl, *DAY1), summarize(tl, *DAY2)


def test_midnight_continuous_run():
    tl, d1, d2 = midnight(CO, CO, CO, CO, CO, CO, CO, CO)  # 23:58 .. 00:05
    [run] = compressor_runs(tl)
    assert (run.start, run.end, run.start_observed, run.end_observed) == (
        MIDNIGHT - 2 * M, MIDNIGHT + 6 * M, True, True)
    [p1], [p2] = project([run], *DAY1), project([run], *DAY2)
    assert (p1.minutes, p1.starts_before_range, p1.ends_after_range) == (2, False, True)
    assert (p2.minutes, p2.starts_before_range, p2.ends_after_range) == (6, True, False)
    assert (d1.observed_starts, d1.observed_stops, d1.compressor_runs_overlapping, d1.complete_run_minutes) == (1, 0, 1, (8,))
    assert (d2.observed_starts, d2.observed_stops, d2.compressor_runs_overlapping, d2.complete_run_minutes) == (0, 1, 1, ())
    assert (d1.compressor_minutes["on"], d2.compressor_minutes["on"]) == (2, 6)
    both = summarize(tl, DAY1[0], DAY2[1])  # overlap counts are not additive across ranges
    assert (both.compressor_runs_overlapping, both.observed_starts, both.observed_stops) == (1, 1, 1)
    assert both.observed_starts == d1.observed_starts + d2.observed_starts
    assert both.observed_stops == d1.observed_stops + d2.observed_stops


def test_midnight_actual_stop_claims_no_continuation():
    tl, d1, d2 = midnight(CO, CO, OFF, OFF, CO, CO, CO, CO)  # stops at 00:00, restarts 00:02
    first, second = compressor_runs(tl)
    assert (first.end, first.end_observed) == (MIDNIGHT, True)
    assert project([first], *DAY2) == []
    assert project([first], *DAY1)[0].ends_after_range is False
    assert project([second], *DAY2)[0].starts_before_range is False
    assert (d1.observed_starts, d1.observed_stops, d1.complete_run_minutes) == (1, 1, (2,))
    assert (d2.observed_starts, d2.observed_stops, d2.complete_run_minutes) == (1, 1, (4,))
    assert d2.exact_off_interval_minutes == (2,)
    assert d1.exact_off_interval_minutes == ()


def test_midnight_restart():
    tl, d1, d2 = midnight(CO, CO, OFF, CO, CO, CO, CO, CO)
    assert [(r.start_observed, r.end_observed) for r in compressor_runs(tl)] == [(True, True), (True, True)]
    assert (d2.observed_starts, d2.exact_off_interval_minutes) == (1, (1,))


@pytest.mark.parametrize("breaker, boundary", [(GAP, Boundary.GAP), (UNKNOWN, Boundary.UNKNOWN)])
def test_midnight_gap_or_unknown_breaks_the_run(breaker, boundary):
    tl, d1, d2 = midnight(CO, CO, breaker, CO, CO, CO, CO, CO)
    first, second = compressor_runs(tl)
    assert (first.end, first.end_boundary, second.start, second.start_boundary) == (
        MIDNIGHT, boundary, MIDNIGHT + M, boundary)
    assert project([first], *DAY1)[0].ends_after_range is False
    assert project([second], *DAY2)[0].starts_before_range is False
    assert (d1.observed_starts, d1.observed_stops, d1.complete_run_minutes) == (1, 0, ())
    assert (d2.observed_starts, d2.observed_stops, d2.complete_run_minutes) == (0, 1, ())
    assert d2.gap_minutes == (1 if breaker is GAP else 0)


def test_midnight_defrost_crossing_midnight():
    tl, d1, d2 = midnight(CO, {**DEFROST, "defrosting_state": 0.5}, DEFROST, DEFROST, DEFROST,
                          {**DEFROST, "defrosting_state": 0.25}, CO, CO)
    [d] = defrosts(tl)
    assert (d.start, d.minutes, d.observed_defrost_seconds) == (MIDNIGHT - M, 5, 225.0)
    [p1], [p2] = project([d], *DAY1), project([d], *DAY2)
    assert (p1.observed_defrost_seconds, p1.ends_after_range) == (30.0, True)
    assert (p2.observed_defrost_seconds, p2.starts_before_range) == (195.0, True)
    assert (d1.defrosts_overlapping, d1.observed_defrost_seconds,
            d2.defrosts_overlapping, d2.observed_defrost_seconds) == (1, 30.0, 1, 195.0)
    both = summarize(tl, DAY1[0], DAY2[1])
    assert (both.defrosts_overlapping, both.observed_defrost_seconds) == (1, 225.0)
    assert both.compressor_runs_overlapping == 1


def test_next_day_window_does_not_search_backwards():
    """Evidence starting at midnight cannot know the run's start; it claims nothing."""
    first = MIDNIGHT - 2 * M
    rows = rows_of(first - 5 * M, *([OFF] * 5 + [CO] * 8 + [OFF] * 5))
    tl = tl_of(rows, *DAY2)
    [run] = compressor_runs(tl)
    assert (run.start, run.start_boundary, run.end_observed) == (MIDNIGHT, Boundary.OUTSIDE_EVIDENCE, True)
    [p] = project([run], *DAY2)
    assert p.starts_before_range is False
    s = summarize(tl, *DAY2)
    assert (s.observed_starts, s.observed_stops, s.complete_run_minutes) == (0, 1, ())


# ------------------------------------------------------------------ Europe/Warsaw DST days

@pytest.mark.parametrize("day, minutes", [(date(2027, 3, 28), 1380), (date(2027, 10, 31), 1500)])
def test_dst_days(day, minutes):
    start, end = local_midnight(day), local_midnight(day + timedelta(days=1))
    assert (end - start) // M == minutes
    shift = int(datetime(day.year, day.month, day.day, 1, tzinfo=timezone.utc).timestamp())
    assert local_date(shift - 1).day == day.day and shift - start in (2 * HOUR, 3 * HOUR)
    count = (end - start) // M
    shapes = [CO if shift - 10 * M <= start + i * M < shift + 10 * M else OFF for i in range(count)]
    tl = tl_of(rows_of(start, *shapes), start, end)
    [run] = compressor_runs(tl)
    assert (run.start, run.minutes, run.start_observed, run.end_observed) == (shift - 10 * M, 20, True, True)
    s = summarize(tl, start, end)
    assert (s.closed_minutes, s.recorded_minutes, s.gap_minutes) == (minutes, minutes, 0)
    assert s.closed_minutes == expected_minutes(start, end, end + HOUR)
    assert (s.observed_starts, s.observed_stops, s.complete_run_minutes) == (1, 1, (20,))
    assert s.compressor_minutes == {"off": minutes - 20, "on": 20, "unknown": 0}


# ------------------------------------------------------------------ canonical minute evidence

class Driver:
    """HeishaMon publishing a full snapshot every 5 s through the real ingest/accumulator."""

    def __init__(self, snapshot_at, start=T0, end=T0 + 6 * M):
        ingest = Ingest(600)
        acc = MinuteAccumulator(ingest, start)
        self.rows = []
        t = start
        ingest.connect(t)
        while t < end:
            self.rows += acc.advance(t)
            for topic, payload in snapshot_at(t).items():
                ingest.message(topic, payload, False, t)
            t += 5
        self.rows += acc.advance(end)

    def pairs(self):
        return [(r.ts, r.values) for r in self.rows]


def test_fractional_defrost_from_canonical_minutes():
    def snapshot(t):
        return {**RUNNING, "main/Defrosting_State": "1" if T0 + 25 <= t < T0 + 190 else "0"}

    rows = Driver(snapshot).pairs()
    fractions = [v["defrosting_state"] for _, v in rows]
    assert fractions == [0.583333, 1.0, 1.0, 0.166667, 0.0, 0.0]
    tl = timeline(build_segments(rows), T0, T0 + 6 * M, T0 + 6 * M)
    [d] = defrosts(tl)
    exact = sum(v * 60 for v in fractions)
    assert (d.start, d.minutes) == (T0, 4)
    assert d.observed_defrost_seconds == pytest.approx(exact, abs=1e-9)
    assert d.observed_defrost_seconds == pytest.approx(165.0, abs=1e-3)
    # The fractional boundary minutes are never merged with the full ones.
    assert [(s.start - T0, s.minutes, s.defrost_fraction) for s in d.segments] == [
        (0, 1, 0.583333), (60, 2, 1.0), (180, 1, 0.166667)]


def test_every_minute_clip_of_a_defrost_is_exact():
    shapes = [CO] + [{**DEFROST, "defrosting_state": f} for f in (0.583333, 1.0, 1.0, 1.0, 0.166667)] + [CO]
    rows = rows_of(T0, *shapes)
    tl = tl_of(rows, T0, T0 + len(shapes) * M)
    [d] = defrosts(tl)
    by_minute = {ts: v["defrosting_state"] * 60 for ts, v in rows if v["defrosting_state"] > 0}
    for a in range(T0, T0 + len(shapes) * M + 1, M):
        for b in range(a, T0 + len(shapes) * M + 1, M):
            expected = sum(s for ts, s in by_minute.items() if a <= ts < b)
            got = sum(p.observed_defrost_seconds for p in project([d], a, b))
            assert got == pytest.approx(expected, abs=1e-9), (a - T0, b - T0)


def test_short_stop_across_a_minute_boundary_leaves_no_zero_minute():
    def snapshot(t):
        return {**RUNNING, "main/Compressor_Freq": "0" if T0 + 40 <= t < T0 + 80 else "40"}

    rows = Driver(snapshot, end=T0 + 4 * M).pairs()
    assert all(v["compressor_freq"] > 0 for _, v in rows)  # a real 40 s stop, never observed
    tl = timeline(build_segments(rows), T0, T0 + 4 * M, T0 + 4 * M)
    [run] = compressor_runs(tl)
    assert run.minutes == 4
    s = summarize(tl, T0, T0 + 4 * M)
    assert (s.observed_starts, s.observed_stops) == (0, 0)


def test_a_full_zero_minute_is_an_observed_stop_and_start():
    def snapshot(t):
        return {**RUNNING, "main/Compressor_Freq": "0" if T0 + 60 <= t < T0 + 120 else "40"}

    rows = Driver(snapshot, end=T0 + 3 * M).pairs()
    assert [v["compressor_freq"] for _, v in rows] == [40.0, 0.0, 40.0]
    runs = compressor_runs(timeline(build_segments(rows), T0, T0 + 3 * M, T0 + 3 * M))
    assert [(r.end_observed, r.start_observed) for r in runs] == [(True, False), (False, True)]


def test_power_tail_after_a_co_run_is_idle_not_co():
    tail = {**IDLE_SNAPSHOT, "main/Heatpump_State": "1", "extra/Heat_Power_Consumption_Extra": "450"}

    def snapshot(t):
        return RUNNING if t < T0 + 2 * M else tail

    rows = Driver(snapshot, end=T0 + 4 * M).pairs()
    tl = timeline(build_segments(rows), T0, T0 + 4 * M, T0 + 4 * M)
    assert [(e.state, e.minutes) for e in activity_events(tl)] == [(Activity.CO, 2), (Activity.IDLE, 2)]


# ------------------------------------------------------------------ energy / COP ingredients

def _energy_rows(rng, count, dyadic):
    shapes = [CO, DHW, IDLE, OFF, UNKNOWN, DEFROST, TRANSITION, GAP]
    out = []
    for i in range(count):
        shape = rng.choice(shapes)
        if shape is None:
            continue

        def power():
            r = rng.random()
            if r < 0.1:
                return None  # unknown channel: unpaired minute
            if r < 0.2:
                return 0.0  # real zero, still paired
            return rng.randrange(0, 16000) / 4 if dyadic else rng.uniform(0, 5000)

        values = {**shape, **{k: power() for k in ("co_power_consumption", "co_power_production",
                                                   "dhw_power_consumption", "dhw_power_production")}}
        r = row(T0 + 7 * M + i * M, **values)
        out.append((r.ts, r.values))
    return out


def _history(rows):
    """The canonical history association: fold each UTC hour, then combine hours in order."""
    acc = {}
    for hour in sorted({ts // HOUR for ts, _ in rows}):
        acc = combine_maps(acc, fold_minutes([r for r in rows if r[0] // HOUR == hour], ENERGY_SERIES))
    return acc


def _energy_facts(stats):
    out = {k: (stats[k].n, energy_kwh(stats[k])) for k in ENERGY_SERIES[:4] if k in stats}
    for name, (i, o) in (("co", ("pair_co_in", "pair_co_out")), ("dhw", ("pair_dhw_in", "pair_dhw_out")),
                         ("total", ("pair_total_in", "pair_total_out"))):
        out[name] = cop(stats.get(i), stats.get(o))
    return out


def test_segment_ingredients_reproduce_history_energy_and_paired_cop_exactly():
    rows = _energy_rows(random.Random(7), 200, dyadic=True)
    rows += rows_of(T0 + 400 * M, {**CO, "co_power_consumption": 18.0, "co_power_production": 0.0,
                                   "dhw_power_consumption": 0.0, "dhw_power_production": 0.0})
    segments = build_segments(rows)
    assert fold_energy(segments) == fold_minutes(rows, ENERGY_SERIES) == _history(rows)
    assert _energy_facts(fold_energy(segments)) == _energy_facts(_history(rows))
    for hour in sorted({ts // HOUR for ts, _ in rows}):
        hour_rows = [r for r in rows if r[0] // HOUR == hour]
        assert fold_energy([s for s in segments if s.start // HOUR == hour]) == fold_minutes(
            hour_rows, ENERGY_SERIES)


def test_segment_ingredients_match_history_for_arbitrary_floats():
    rows = _energy_rows(random.Random(11), 300, dyadic=False)
    ours, history = fold_energy(build_segments(rows)), _history(rows)
    assert set(ours) == set(history)
    for k in ENERGY_SERIES:
        assert ours[k].n == history[k].n  # identical contributing and paired minutes
        assert math.isclose(ours[k].sum, history[k].sum, rel_tol=1e-12)  # association order only


def test_paired_cop_is_ratio_of_sums_not_average():
    rows = rows_of(T0, {**CO, "co_power_consumption": 1000.0, "co_power_production": 4000.0},
                   {**CO, "co_power_consumption": 18.0, "co_power_production": 0.0},
                   {**CO, "co_power_consumption": None, "co_power_production": 9000.0},
                   {**CO, "co_power_consumption": 0.0, "co_power_production": 0.0})
    [event] = activity_events(tl_of(rows, T0, T0 + 4 * M))
    result = cop(event.energy["pair_co_in"], event.energy["pair_co_out"])
    assert result == {"cop": 4000.0 / 1018.0, "paired_minutes": 3,
                      "input_kwh": 1018.0 / 60000, "output_kwh": 4000.0 / 60000}
    assert event.energy["co_power_production"].n == 4  # unpaired production still counts as energy
    assert "pair_total_in" not in event.energy  # DHW channels unknown: no total pair


def test_per_run_and_per_event_energy_fold_their_own_minutes():
    rows = _energy_rows(random.Random(3), 180, dyadic=True)
    tl = timeline(build_segments(rows), T0, T0 + 4 * HOUR, T0 + 4 * HOUR)
    for span in compressor_runs(tl) + activity_events(tl):
        own = [r for r in rows if span.start <= r[0] < span.end]
        assert span.energy == fold_minutes(own, ENERGY_SERIES)


def test_clipped_piece_carries_no_energy():
    tl = tl_of(rows_of(T0 + HOUR - 2 * M, CO, CO, CO, CO), T0 + HOUR - 2 * M, T0 + HOUR + 2 * M)
    [run] = compressor_runs(tl)
    assert run.energy is not None
    [p] = project([run], T0 + HOUR - M, T0 + HOUR + 2 * M)
    assert fold_energy(p.segments) is None  # a sub-segment edge needs raw minutes, never a guess
    [whole_hours] = project([run], T0, T0 + 2 * HOUR)
    assert fold_energy(whole_hours.segments) == run.energy


# ------------------------------------------------------------------ brute-force reference

def test_spans_and_summary_match_a_minute_by_minute_reference():
    """Independent per-minute definitions of every counted fact, over random evidence."""
    rng = random.Random(2026)
    shapes = [CO, DHW, IDLE, OFF, UNKNOWN, DEFROST, {**DEFROST, "compressor_freq": 0.0},
              {**CO, "defrosting_state": None}, GAP]
    for _ in range(300):
        count = rng.randrange(1, 150)
        start = T0 + rng.randrange(0, 120) * M
        rows = rows_of(start, *[rng.choice(shapes) for _ in range(count)])
        window_end = start + count * M
        closed_until = start + rng.randrange(0, count + 3) * M
        rows = [r for r in rows if r[0] < closed_until]
        tl = tl_of(rows, start, window_end, closed_until)
        a = start + rng.randrange(0, count + 1) * M
        b = a + rng.randrange(0, (window_end - a) // M + 1) * M
        s = summarize(tl, a, b)

        by_ts = dict(rows)
        frontier = min(window_end, closed_until)
        comp = {t: classify(v)[1] for t, v in by_ts.items()}
        ON, OFF_ = Compressor.ON, Compressor.OFF
        in_range = [t for t in range(a, b, M) if t < frontier]
        assert s.closed_minutes == len(in_range)
        assert s.gap_minutes == sum(1 for t in in_range if t not in by_ts)
        assert s.activity_minutes == {x.value: sum(1 for t in in_range if t in by_ts and
                                                   classify(by_ts[t])[0] is x) for x in Activity}
        assert s.observed_starts == sum(1 for t in in_range if comp.get(t) is ON and comp.get(t - M) is OFF_)
        assert s.observed_stops == sum(1 for t in in_range if comp.get(t) is ON and comp.get(t + M) is OFF_)
        runs = [t for t in range(start, window_end, M)
                if comp.get(t) is ON and (comp.get(t - M) is not ON or t == start)]
        run_ends = {t: next(u for u in range(t, window_end + M, M) if comp.get(u) is not ON or u == window_end)
                    for t in runs}
        assert s.compressor_runs_overlapping == sum(1 for t in runs if max(t, a) < min(run_ends[t], b))
        complete = tuple((run_ends[t] - t) // M for t in runs
                         if a <= t < b and comp.get(t - M) is OFF_ and comp.get(run_ends[t]) is OFF_)
        assert s.complete_run_minutes == complete
        offs = [t for t in range(start, window_end, M)
                if comp.get(t) is OFF_ and (comp.get(t - M) is not OFF_ or t == start)]
        off_ends = {t: next(u for u in range(t, window_end + M, M) if comp.get(u) is not OFF_ or u == window_end)
                    for t in offs}
        exact = tuple((off_ends[t] - t) // M for t in offs
                      if a <= t < b and comp.get(t - M) is ON and comp.get(off_ends[t]) is ON)
        assert s.exact_off_interval_minutes == exact


# ------------------------------------------------------------------ evidence-window dependence

def test_summary_names_its_evidence_window():
    tl = tl_of(rows_of(T0, OFF, CO, OFF), T0, T0 + 3 * M, closed_until=T0 + HOUR)
    s = summarize(tl, T0 + M, T0 + 2 * M)
    assert (s.start, s.end, s.evidence_start, s.evidence_end, s.closed_until) == (
        T0 + M, T0 + 2 * M, T0, T0 + 3 * M, T0 + HOUR)
    assert s.segment_rule_version == ACTIVITY_RULE_VERSION == 1


def test_adjacent_minutes_fix_observed_starts_and_stops_but_not_complete_runs():
    a, b = T0 + 10 * M, T0 + 20 * M
    shapes = [OFF] * 10 + [CO] * 3 + [OFF] * 3 + [CO] * 4 + [CO] * 10 + [OFF] * 3  # run 16..29
    rows = rows_of(T0, *shapes)
    exact = summarize(tl_of(rows, a, b), a, b)
    adjacent = summarize(tl_of(rows, a - M, b + M), a, b)
    wide = summarize(tl_of(rows, T0, T0 + len(shapes) * M), a, b)
    # Only the range itself: an edge proves nothing, the start at `a` is not observed.
    assert (exact.observed_starts, exact.observed_stops) == (1, 1)
    assert (adjacent.observed_starts, adjacent.observed_stops) == (2, 1)
    assert (wide.observed_starts, wide.observed_stops) == (2, 1)
    # The run starting at 16 continues past `b`; only wider evidence proves its duration.
    assert (exact.complete_run_minutes, adjacent.complete_run_minutes, wide.complete_run_minutes) == (
        (), (3,), (3, 14))
    for s in (exact, adjacent, wide):
        assert (s.activity_minutes, s.compressor_minutes, s.gap_minutes) == (
            wide.activity_minutes, wide.compressor_minutes, wide.gap_minutes)


def test_observed_starts_and_stops_are_invariant_beyond_adjacent_evidence():
    rng = random.Random(44)
    shapes = [CO, DHW, IDLE, OFF, UNKNOWN, DEFROST, {**DEFROST, "compressor_freq": 0.0}, GAP]
    for _ in range(300):
        count = rng.randrange(3, 120)
        rows = rows_of(T0, *[rng.choice(shapes) for _ in range(count)])
        whole_end = T0 + count * M
        closed_until = T0 + rng.randrange(0, count + 3) * M
        rows = [r for r in rows if r[0] < closed_until]
        a = T0 + rng.randrange(1, count - 1) * M
        b = a + rng.randrange(0, (whole_end - M - a) // M + 1) * M
        # Every evidence window containing [a - 1m, b + 1m): adjacent, random, widest.
        lows = {a - M, T0, T0 + rng.randrange(0, (a - T0) // M) * M}
        highs = {b + M, whole_end, T0 + rng.randrange((b + M - T0) // M, count + 1) * M}
        results = [summarize(tl_of(rows, lo, hi, closed_until), a, b) for lo in lows for hi in highs]
        exact = summarize(tl_of(rows, a, b, closed_until), a, b)
        first = results[0]
        for s in results:
            assert (s.observed_starts, s.observed_stops) == (first.observed_starts, first.observed_stops)
            assert (s.activity_minutes, s.compressor_minutes, s.gap_minutes, s.closed_minutes) == (
                exact.activity_minutes, exact.compressor_minutes, exact.gap_minutes, exact.closed_minutes)
            assert s.observed_defrost_seconds == pytest.approx(exact.observed_defrost_seconds, abs=1e-9)


# ------------------------------------------------------------------ fractional TOP0 through ingest

def test_fractional_heatpump_state_from_real_ingest_is_idle():
    """A valid TOP0 switch inside a minute stays fully known and, compressor off, is idle."""
    def snapshot(t):
        return {**IDLE_SNAPSHOT, "main/Heatpump_State": "1" if t < T0 + 90 else "0"}

    rows = Driver(snapshot, end=T0 + 3 * M).pairs()
    assert [(v["heatpump_state"], v["compressor_freq"], v["defrosting_state"]) for _, v in rows] == [
        (1.0, 0.0, 0.0), (0.5, 0.0, 0.0), (0.0, 0.0, 0.0)]
    assert [classify(v) for _, v in rows] == [
        (Activity.IDLE, Compressor.OFF), (Activity.IDLE, Compressor.OFF), (Activity.OFF, Compressor.OFF)]
    tl = timeline(build_segments(rows), T0, T0 + 3 * M, T0 + 3 * M)
    assert [((s.start - T0) // M, s.minutes, s.activity) for s in tl.segments] == [
        (0, 2, Activity.IDLE), (2, 1, Activity.OFF)]
    # Minute classifications, not per-state seconds: 90 s of idle are reported as two idle minutes.
    s = summarize(tl, T0, T0 + 3 * M)
    assert (s.activity_minutes["idle"], s.activity_minutes["off"]) == (2, 1)
