"""Stage 4D-B pure report algebra, checked against literal minute-level oracles."""

import ast
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from pompa import report
from pompa.activity import ACTIVITY_RULE_VERSION, Boundary, build_segments, timeline
from pompa.aggregation import PAIRS, POWER_CHANNELS, RECORDED, Stats, fold_minutes
from pompa.report import (ReportActivityUnavailable, ReportInput, ReportInvariantError,
                          compose_report, coverage, effective_to, resolve_period)
from pompa.timegrid import HOUR, local_midnight

M = 60
SERIES = (RECORDED, *POWER_CHANNELS, *(key for pair in PAIRS.values() for key in pair),
          "outside_temp", "compressor_freq")
POWER = dict(zip(POWER_CHANNELS, (900.0, 3600.0, 300.0, 900.0)))


def minute(ts, state="co", *, power=None, outside=4.0):
    """Construct literal Stage 4C input; the state label is an independent oracle tag."""
    templates = {
        "off": (0.0, 0.0, 0.0, 0.0),
        "idle": (0.0, 0.0, 1.0, 0.0),
        "co": (40.0, 0.0, 1.0, 0.0),
        "dhw": (40.0, 0.0, 1.0, 1.0),
        "transition": (40.0, 0.0, 1.0, 0.5),
        "defrost": (40.0, 0.5, 1.0, 0.0),
        "unknown": (None, 0.0, 1.0, 0.0),
    }
    freq, defrost, heatpump, valve = templates[state]
    return ts, {"compressor_freq": freq, "defrosting_state": defrost,
                "heatpump_state": heatpump, "three_way_valve": valve,
                "outside_temp": outside, **(POWER if power is None else power)}


def input_from_rows(period, rows, *, now=None, closed_until=None,
                    evidence_start=None, evidence_end=None):
    """Prepare already-loaded partials; never use report serializers to make expected facts."""
    rows = sorted(rows)
    closed_until = period.end if closed_until is None else closed_until
    now = closed_until + M if now is None else now
    evidence_start = min(period.start, rows[0][0]) if rows and evidence_start is None else (
        period.start if evidence_start is None else evidence_start)
    evidence_end = max(period.end, rows[-1][0] + M) if rows and evidence_end is None else (
        period.start if evidence_end is None and closed_until <= period.start else
        period.end if evidence_end is None else evidence_end)
    tl = timeline(build_segments(rows), evidence_start, evidence_end, closed_until)
    partials = tuple(fold_minutes(((ts, values) for ts, values in rows
                                   if a <= ts < b and ts < closed_until), SERIES)
                     for a, b in period.edges)
    return ReportInput(period, now, closed_until, partials, tl, ACTIVITY_RULE_VERSION)


def literal_energy(rows):
    """Independent minute-by-minute channel and paired oracle; no Stats/COP helpers."""
    channels = {}
    for key in POWER_CHANNELS:
        values = [v[key] for _, v in rows if v[key] is not None]
        channels[key] = {"minutes": len(values), "unknown_minutes": len(rows) - len(values),
                         "kwh": sum(values) / 60000 if values else None}
    aggregates = {}
    for name, keys in (("consumption", (POWER_CHANNELS[0], POWER_CHANNELS[2])),
                       ("production", (POWER_CHANNELS[1], POWER_CHANNELS[3]))):
        seen = [channels[key]["kwh"] for key in keys if channels[key]["minutes"]]
        aggregates[name] = {"observed_kwh": sum(seen) if seen else None,
                            "unknown_channel_minutes": sum(channels[key]["unknown_minutes"]
                                                           for key in keys)}
    pairs = {}
    for name, keys in (("co", POWER_CHANNELS[:2]), ("dhw", POWER_CHANNELS[2:]),
                       ("total", POWER_CHANNELS)):
        matching = [v for _, v in rows if all(v[key] is not None for key in keys)]
        inputs = sum(v[POWER_CHANNELS[0]] for v in matching) if name == "co" else (
            sum(v[POWER_CHANNELS[2]] for v in matching) if name == "dhw" else
            sum(v[POWER_CHANNELS[0]] + v[POWER_CHANNELS[2]] for v in matching))
        outputs = sum(v[POWER_CHANNELS[1]] for v in matching) if name == "co" else (
            sum(v[POWER_CHANNELS[3]] for v in matching) if name == "dhw" else
            sum(v[POWER_CHANNELS[1]] + v[POWER_CHANNELS[3]] for v in matching))
        pairs[name] = {"paired_minutes": len(matching),
                       "input_kwh": inputs / 60000 if matching else None,
                       "output_kwh": outputs / 60000 if matching else None,
                       "cop": outputs / inputs if inputs else None}
    return {"channels": channels, **aggregates, "cop": pairs}


def equal_energy(actual, expected):
    for key in POWER_CHANNELS:
        for name in ("minutes", "unknown_minutes", "kwh"):
            assert actual["channels"][key][name] == pytest.approx(expected["channels"][key][name])
    for aggregate in ("consumption", "production"):
        for name in ("observed_kwh", "unknown_channel_minutes"):
            assert actual[aggregate][name] == pytest.approx(expected[aggregate][name])
    for key in ("co", "dhw", "total"):
        for name in ("cop", "paired_minutes", "input_kwh", "output_kwh"):
            assert actual["cop"][key][name] == pytest.approx(expected["cop"][key][name])


@pytest.mark.parametrize("kind,anchor,count,hours,first,last", [
    ("day", "2026-03-29", 23, 23, "2026-03-29", "2026-03-30"),
    ("day", "2026-10-25", 25, 25, "2026-10-25", "2026-10-26"),
    ("week", "2026-03-29", 7, 167, "2026-03-23", "2026-03-30"),
    ("week", "2026-10-25", 7, 169, "2026-10-19", "2026-10-26"),
    ("week", "2027-01-01", 7, 168, "2026-12-28", "2027-01-04"),
    ("month", "2026-02-14", 28, 672, "2026-02-01", "2026-03-01"),
    ("month", "2028-02-14", 29, 696, "2028-02-01", "2028-03-01"),
    ("month", "2026-03-14", 31, 743, "2026-03-01", "2026-04-01"),
    ("month", "2026-10-14", 31, 745, "2026-10-01", "2026-11-01"),
])
def test_warsaw_calendar(kind, anchor, count, hours, first, last):
    period = resolve_period(kind, date=anchor)
    assert (period.from_date.isoformat(), period.to_date.isoformat()) == (first, last)
    assert len(period.edges) == count
    assert (period.end - period.start) // HOUR == hours
    assert all(a % HOUR == b % HOUR == 0 for a, b in period.edges)
    assert period.edges[0][0] == period.start and period.edges[-1][1] == period.end
    assert all(period.edges[i][1] == period.edges[i + 1][0] for i in range(count - 1))
    if kind != "day":
        assert [a for a, _ in period.edges] == [local_midnight(date.fromisoformat(first) + timedelta(days=i))
                                                  for i in range(count)]


def test_custom_and_strict_date_forms():
    period = resolve_period("custom", from_date="2026-10-01", to_date="2026-11-01")
    assert len(period.edges) == 31 and (period.end - period.start) // HOUR == 745
    for kwargs in ({"date": "2026-03-29T00:00:00"}, {"date": "20260329"},
                   {"date": "2026-02-30"}, {"date": "2026-01-01", "from_date": "2026-01-01"}):
        with pytest.raises(ValueError):
            resolve_period("day", **kwargs)
    for first, last in (("2026-10-01", "2026-10-01"), ("2026-10-02", "2026-10-01"),
                        ("2026-10-01", "2026-11-02")):
        with pytest.raises(ValueError):
            resolve_period("custom", from_date=first, to_date=last)
    with pytest.raises(ValueError):
        resolve_period("year", date="2026-01-01")


def test_effective_and_f2_coverage_partition():
    period = resolve_period("day", date="2026-09-25")
    a, b = period.start, period.end
    for frontier, expected in ((b + HOUR, b), (a + 90 * M, a + 90 * M), (a - HOUR, a)):
        assert effective_to(period, frontier) == expected
        assert a <= expected <= b
    cov = coverage(a, b, now=a + 121 * M + 15, closed_until=a + 90 * M,
                   recorded_minutes=60)
    assert (cov["settled_minutes"], cov["unsettled_minutes"], cov["future_minutes"]) == (90, 31, 1319)
    assert cov["gap_minutes"] == 30 and cov["coverage_percent"] == 66.7
    assert sum(cov[key] for key in ("settled_minutes", "unsettled_minutes", "future_minutes")) == 1440
    future = coverage(a, b, now=a - M + 30, closed_until=a - HOUR, recorded_minutes=0)
    assert future == dict(calendar_minutes=1440, settled_minutes=0, unsettled_minutes=0,
                          future_minutes=1440, recorded_minutes=0, gap_minutes=0,
                          coverage_percent=None)
    with pytest.raises(ReportInvariantError):
        coverage(a, b, now=a + M, closed_until=a + 2 * M, recorded_minutes=0)


def test_future_report_has_full_empty_fact_shape():
    period = resolve_period("day", date="2026-10-10")
    source = input_from_rows(period, [], now=period.start - M + 30,
                             closed_until=period.start - HOUR)
    result = compose_report(source)
    assert result["observation"]["effective_to"] == result["period"]["from"]
    assert result["evidence"] == {"from": None, "to": None}
    for facts in (result["totals"], *result["buckets"]):
        cov = facts["coverage"]
        assert cov["settled_minutes"] == cov["recorded_minutes"] == cov["gap_minutes"] == 0
        assert cov["unsettled_minutes"] == 0 and cov["future_minutes"] == cov["calendar_minutes"]
        assert cov["coverage_percent"] is None
        for channel in facts["energy"]["channels"].values():
            assert channel == {"kwh": None, "minutes": 0, "unknown_minutes": 0}
        assert facts["energy"]["consumption"] == {"observed_kwh": None, "unknown_channel_minutes": 0}
        assert facts["energy"]["production"] == {"observed_kwh": None, "unknown_channel_minutes": 0}
        assert facts["technical"]["outside_temp"] == {"avg": None, "min": None, "max": None,
                                                       "minutes": 0}
        assert facts["events"]["defrost_events"] == 0


def test_composed_current_report_keeps_unsettled_tail_out_of_gaps():
    period = resolve_period("day", date="2026-09-25")
    a = period.start
    closed = a + 2 * HOUR + 30 * M
    rows = [minute(a + i * M, "co") for i in range(120)]
    result = compose_report(input_from_rows(period, rows, now=closed + 20 * M + 31,
                                            closed_until=closed))
    cov = result["totals"]["coverage"]
    assert result["observation"]["closed_until"] == "2026-09-25T00:30:00Z"
    assert result["observation"]["effective_to"] == "2026-09-25T00:30:00Z"
    assert (cov["settled_minutes"], cov["recorded_minutes"], cov["gap_minutes"]) == (150, 120, 30)
    assert cov["unsettled_minutes"] == 20
    assert cov["future_minutes"] == 1440 - 170
    edge = result["buckets"][2]["coverage"]
    assert (edge["settled_minutes"], edge["unsettled_minutes"], edge["future_minutes"]) == (30, 20, 10)


def test_literal_knownness_and_cop_cases():
    period = resolve_period("day", date="2026-09-25")
    a = period.start
    cases = [
        (0, POWER_CHANNELS_DICT(0.0, 0.0, 0.0, 0.0)),
        (1, POWER_CHANNELS_DICT(18.0, 0.0, 10.0, 20.0)),
        (2, POWER_CHANNELS_DICT(None, 80.0, 0.0, 0.0)),
        (3, POWER_CHANNELS_DICT(50.0, None, None, 40.0)),
        (5, POWER_CHANNELS_DICT(None, None, 70.0, None)),
    ]
    rows = [minute(a + i * M, power=power) for i, power in cases]
    report_data = compose_report(input_from_rows(period, rows, now=period.end + M))
    actual = report_data["totals"]["energy"]
    equal_energy(actual, literal_energy(rows))
    assert report_data["totals"]["coverage"]["recorded_minutes"] == 5
    assert report_data["totals"]["coverage"]["gap_minutes"] == 1435
    assert actual["channels"][POWER_CHANNELS[0]]["minutes"] == 3
    assert actual["channels"][POWER_CHANNELS[0]]["unknown_minutes"] == 2
    assert actual["consumption"]["unknown_channel_minutes"] == 3
    assert actual["cop"]["total"]["paired_minutes"] == 2
    assert actual["cop"]["total"]["cop"] == pytest.approx(20 / 28)
    assert actual["channels"][POWER_CHANNELS[0]]["kwh"] == pytest.approx(68 / 60000)


def test_disjoint_observed_consumption_is_not_lost_to_pair_knownness():
    period = resolve_period("day", date="2026-09-25")
    a = period.start
    rows = [minute(a, power=POWER_CHANNELS_DICT(600.0, None, None, None)),
            minute(a + M, power=POWER_CHANNELS_DICT(None, None, 1200.0, None))]
    energy = compose_report(input_from_rows(period, rows, now=period.end + M))["totals"]["energy"]
    assert energy["consumption"] == {"observed_kwh": pytest.approx(1800 / 60000),
                                     "unknown_channel_minutes": 2}
    assert energy["production"] == {"observed_kwh": None, "unknown_channel_minutes": 4}
    assert energy["cop"]["total"] == {"cop": None, "paired_minutes": 0,
                                         "input_kwh": None, "output_kwh": None}


def test_known_zero_pair_has_evidence_and_null_cop_denominator():
    period = resolve_period("day", date="2026-09-25")
    rows = [minute(period.start, power=POWER_CHANNELS_DICT(0.0, 0.0, 0.0, 0.0))]
    energy = compose_report(input_from_rows(period, rows, now=period.end + M))["totals"]["energy"]
    for pair in energy["cop"].values():
        assert pair == {"cop": None, "paired_minutes": 1,
                        "input_kwh": 0.0, "output_kwh": 0.0}
    assert energy["consumption"] == {"observed_kwh": 0.0, "unknown_channel_minutes": 0}
    assert energy["production"] == {"observed_kwh": 0.0, "unknown_channel_minutes": 0}


def POWER_CHANNELS_DICT(co_in, co_out, dhw_in, dhw_out):
    return dict(zip(POWER_CHANNELS, (co_in, co_out, dhw_in, dhw_out)))


@pytest.mark.parametrize("seed", range(12))
def test_randomized_literal_energy_classes_heating_and_bucket_additivity(seed):
    rng = random.Random(seed)
    period = resolve_period("day", date="2026-09-25")
    labels = tuple(a.value for a in report.Activity)
    rows, tags = [], {}
    values = (None, 0.0, 18.0, 100.0, 900.0)
    for i in range(180):
        if rng.random() < 0.18:
            continue
        label = rng.choice(labels)
        power = dict(zip(POWER_CHANNELS, (rng.choice(values) for _ in POWER_CHANNELS)))
        ts = period.start + i * M
        row = minute(ts, label, power=power, outside=rng.choice((None, 0.0, -5.0, 9.0)))
        if label == "defrost":
            row[1]["defrosting_state"] = rng.choice((0.25, 0.5, 1.0))
            row[1]["compressor_freq"] = rng.choice((None, 0.0, 40.0))
        rows.append(row)
        tags[ts] = label
    result = compose_report(input_from_rows(period, rows, now=period.end + M))
    equal_energy(result["totals"]["energy"], literal_energy(rows))
    total_classes = result["totals"]["activity"]["classes"]
    for label in labels:
        selected = [row for row in rows if tags[row[0]] == label]
        assert total_classes[label]["minutes"] == len(selected)
        equal_energy(total_classes[label], literal_energy(selected))
    heating = [row for row in rows if tags[row[0]] in ("co", "dhw", "transition")]
    actual_heat = result["totals"]["activity"]["heating"]
    assert actual_heat["activities"] == ["co", "dhw", "transition"]
    assert actual_heat["minutes"] == len(heating)
    equal_energy(actual_heat, literal_energy(heating))
    for key in POWER_CHANNELS:
        assert sum(total_classes[label]["channels"][key]["minutes"] for label in labels) == (
            result["totals"]["energy"]["channels"][key]["minutes"])
        assert sum(total_classes[label]["channels"][key]["unknown_minutes"] for label in labels) == (
            result["totals"]["energy"]["channels"][key]["unknown_minutes"])
    for key in ("consumption", "production"):
        assert sum(total_classes[label][key]["unknown_channel_minutes"] for label in labels) == (
            result["totals"]["energy"][key]["unknown_channel_minutes"])
    for name in ("recorded_minutes", "gap_minutes", "settled_minutes", "unsettled_minutes",
                 "future_minutes"):
        assert sum(b["coverage"][name] for b in result["buckets"]) == result["totals"]["coverage"][name]
    for name in ("observed_starts", "observed_stops", "defrost_events", "observed_defrost_seconds"):
        assert sum(b["events"][name] for b in result["buckets"]) == pytest.approx(
            result["totals"]["events"][name])
    for label in labels:
        assert sum(b["events"]["activity_events"][label] for b in result["buckets"]) == (
            result["totals"]["events"]["activity_events"][label])
    for key in POWER_CHANNELS:
        channel = result["totals"]["energy"]["channels"][key]
        assert sum(b["energy"]["channels"][key]["minutes"] for b in result["buckets"]) == channel["minutes"]
        assert sum(b["energy"]["channels"][key]["unknown_minutes"] for b in result["buckets"]) == channel["unknown_minutes"]
        observed = [b["energy"]["channels"][key]["kwh"] for b in result["buckets"]
                    if b["energy"]["channels"][key]["kwh"] is not None]
        assert channel["kwh"] == pytest.approx(sum(observed) if observed else None)
    for name in ("complete_runs", "exact_off_intervals"):
        assert sum(b["events"][name]["count"] for b in result["buckets"]) == (
            result["totals"]["events"][name]["count"])
        assert sum(b["events"][name]["total_minutes"] for b in result["buckets"]) == (
            result["totals"]["events"][name]["total_minutes"])
    assert result["totals"]["events"]["observed_defrost_seconds"] == pytest.approx(
        sum(v["defrosting_state"] * M for _, v in rows if v["defrosting_state"] is not None
            and v["defrosting_state"] > 0))
    for (a, b), facts in zip(period.edges, result["buckets"], strict=True):
        selected = [row for row in rows if a <= row[0] < b]
        equal_energy(facts["energy"], literal_energy(selected))
        assert facts["coverage"]["recorded_minutes"] == len(selected)


@pytest.mark.parametrize("boundary", ("observed", "gap", "unknown", "outside_evidence"))
def test_generic_event_boundary_and_hour_crossing(boundary):
    period = resolve_period("day", date="2026-09-25")
    a = period.start + HOUR - M if boundary != "outside_evidence" else period.start
    length = 3 if boundary != "outside_evidence" else 61
    rows = [minute(a + i * M, "defrost") for i in range(length)]
    if boundary == "observed":
        rows.insert(0, minute(a - M, "off"))
    elif boundary == "unknown":
        unknown = minute(a - M, "unknown", power=POWER_CHANNELS_DICT(None, None, None, None))
        unknown[1]["defrosting_state"] = None
        rows.insert(0, unknown)
    rows.append(minute(a + length * M, "off"))
    source = input_from_rows(period, rows, now=period.end + M)
    assert report.defrosts(source.timeline)[0].start_boundary == Boundary(boundary)
    result = compose_report(source)
    first, next_bucket = result["buckets"][:2]
    assert first["events"]["defrost_events"] == first["events"]["activity_events"]["defrost"] == 1
    assert next_bucket["events"]["defrost_events"] == next_bucket["events"]["activity_events"]["defrost"] == 0
    assert sum(b["events"]["defrost_events"] for b in result["buckets"]) == 1
    assert first["events"]["observed_defrost_seconds"] == pytest.approx((length - (2 if length == 3 else 1)) * 30)
    assert next_bucket["events"]["observed_defrost_seconds"] == pytest.approx((2 if length == 3 else 1) * 30)
    assert result["totals"]["events"]["observed_defrost_seconds"] == pytest.approx(length * 30)
    assert result["totals"]["defrosts_overlapping"] == 1
    assert "defrosts_overlapping" not in first
    assert "compressor_runs_overlapping" not in next_bucket
    assert first["events"]["observed_starts"] == int(boundary == "observed")
    assert result["totals"]["events"]["observed_stops"] == 1
    assert result["totals"]["events"]["complete_runs"]["count"] == int(boundary == "observed")


@pytest.mark.parametrize("kind,anchor,next_anchor", [
    ("week", "2026-03-29", "2026-03-30"),
    ("month", "2026-03-15", "2026-04-15"),
])
def test_event_at_report_edge_counts_once_and_overlap_totals_only(kind, anchor, next_anchor):
    first_period = resolve_period(kind, date=anchor)
    second_period = resolve_period(kind, date=next_anchor)
    edge = first_period.end
    rows = [minute(edge - M, "defrost"), minute(edge, "defrost"),
            minute(edge + M, "defrost"), minute(edge + 2 * M, "off")]
    closed = second_period.end
    before = compose_report(input_from_rows(first_period, rows, now=closed + M,
                                            closed_until=closed))
    after = compose_report(input_from_rows(second_period, rows, now=closed + M,
                                           closed_until=closed))
    assert before["totals"]["events"]["defrost_events"] == 1
    assert after["totals"]["events"]["defrost_events"] == 0
    assert before["totals"]["defrosts_overlapping"] == after["totals"]["defrosts_overlapping"] == 1
    assert before["totals"]["events"]["observed_defrost_seconds"] == 30
    assert after["totals"]["events"]["observed_defrost_seconds"] == 60


@pytest.mark.parametrize("kind,anchor,next_anchor", [
    ("week", "2026-03-29", "2026-03-30"),
    ("month", "2026-03-15", "2026-04-15"),
])
def test_complete_run_crosses_report_edge_without_double_count(kind, anchor, next_anchor):
    before_period = resolve_period(kind, date=anchor)
    after_period = resolve_period(kind, date=next_anchor)
    edge = before_period.end
    rows = [minute(edge - 2 * M, "off"), minute(edge - M, "co"),
            minute(edge, "co"), minute(edge + M, "off")]
    closed = after_period.end
    before = compose_report(input_from_rows(before_period, rows, now=closed + M,
                                            closed_until=closed))
    after = compose_report(input_from_rows(after_period, rows, now=closed + M,
                                           closed_until=closed))
    assert before["totals"]["events"]["observed_starts"] == 1
    assert after["totals"]["events"]["observed_starts"] == 0
    assert before["totals"]["events"]["observed_stops"] == 0
    assert after["totals"]["events"]["observed_stops"] == 1
    assert before["totals"]["events"]["complete_runs"] == {
        "count": 1, "total_minutes": 2, "min_minutes": 2, "max_minutes": 2, "mean_minutes": 2.0}
    assert after["totals"]["events"]["complete_runs"]["count"] == 0
    assert before["totals"]["compressor_runs_overlapping"] == 1
    assert after["totals"]["compressor_runs_overlapping"] == 1
    assert before["totals"]["events"]["activity_events"]["co"] == 1
    assert after["totals"]["events"]["activity_events"]["co"] == 0


def test_exact_off_interval_crosses_warsaw_midnight_once():
    period = resolve_period("week", date="2026-03-29")
    edge = local_midnight(date(2026, 3, 27))
    rows = [minute(edge - 4 * M, "off"), minute(edge - 3 * M, "co"),
            minute(edge - 2 * M, "off"), minute(edge - M, "off"),
            minute(edge, "off"), minute(edge + M, "co"), minute(edge + 2 * M, "off")]
    result = compose_report(input_from_rows(period, rows, now=period.end + M))
    intervals = [b["events"]["exact_off_intervals"] for b in result["buckets"]]
    assert sum(item["count"] for item in intervals) == 1
    assert result["totals"]["events"]["exact_off_intervals"] == {
        "count": 1, "total_minutes": 3, "min_minutes": 3, "max_minutes": 3, "mean_minutes": 3.0}
    assert intervals[(date(2026, 3, 26) - period.from_date).days]["count"] == 1


def test_invariant_failures_and_activity_unavailable():
    period = resolve_period("day", date="2026-09-25")
    rows = [minute(period.start, "co")]
    source = input_from_rows(period, rows, now=period.end + M)
    bad = list(source.history_buckets)
    bad[0] = {**bad[0], POWER_CHANNELS[0]: Stats(2, 100.0, 50.0, 50.0, 50.0)}
    with pytest.raises(ReportInvariantError, match="known minutes"):
        compose_report(replace(source, history_buckets=tuple(bad)))
    bad[0] = {key: value for key, value in source.history_buckets[0].items() if key != "compressor_freq"}
    with pytest.raises(ReportInvariantError, match="compressor frequency"):
        compose_report(replace(source, history_buckets=tuple(bad)))
    missing_activity = timeline([], period.start, period.end, period.end)
    with pytest.raises(ReportInvariantError, match="do not partition"):
        compose_report(replace(source, timeline=missing_activity))
    malformed = replace(period, edges=((period.start, period.start + HOUR),
                                       (period.start + 2 * HOUR, period.end)))
    with pytest.raises(ReportInvariantError, match="bucket edges"):
        compose_report(replace(source, period=malformed, history_buckets=source.history_buckets[:2]))
    empty = input_from_rows(period, [], now=period.end + M)
    lost = timeline([], period.start, period.end, period.end,
                    unavailable=[(period.start, period.start + HOUR)])
    with pytest.raises(ReportActivityUnavailable):
        compose_report(replace(empty, timeline=lost))


def test_span_functions_are_called_once_for_many_buckets(monkeypatch):
    period = resolve_period("custom", from_date="2026-09-01", to_date="2026-10-02")
    rows = [minute(period.start + i * M, "co" if i % 2 else "off") for i in range(240)]
    calls = {}
    for name in ("activity_events", "defrosts", "compressor_runs", "compressor_off_intervals"):
        original = getattr(report, name)

        def counted(tl, *, _name=name, _original=original):
            calls[_name] = calls.get(_name, 0) + 1
            return _original(tl)

        monkeypatch.setattr(report, name, counted)
    compose_report(input_from_rows(period, rows, now=period.end + M))
    assert calls == dict.fromkeys(("activity_events", "defrosts", "compressor_runs",
                                  "compressor_off_intervals"), 1)


def test_technical_active_average_and_zero_on():
    period = resolve_period("day", date="2026-09-25")
    a = period.start
    rows = [minute(a, "off", outside=-5.0), minute(a + M, "co", outside=0.0),
            minute(a + 2 * M, "co", outside=10.0), minute(a + 3 * M, "unknown", outside=None)]
    result = compose_report(input_from_rows(period, rows, now=period.end + M))["totals"]["technical"]
    assert result["outside_temp"] == {"avg": pytest.approx(5 / 3), "min": -5.0,
                                      "max": 10.0, "minutes": 3}
    assert result["compressor_freq"] == {"avg": pytest.approx(80 / 3), "max": 40.0,
                                        "minutes": 3, "active_avg": 40.0}
    off_only = compose_report(input_from_rows(period, [minute(a, "off")],
                                              now=period.end + M))["totals"]["technical"]
    assert off_only["compressor_freq"]["active_avg"] is None


def test_report_module_has_no_storage_http_or_runtime_imports():
    path = Path(report.__file__)
    parsed = ast.parse(path.read_text())
    imports = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(("pompa." if node.level else "") + (node.module or ""))
    assert not imports & {"pompa.storage", "pymysql", "fastapi", "pompa.api", "pompa.recorder",
                          "os", "subprocess"}
    assert not any(isinstance(node, ast.ImportFrom) and any(alias.name == "summarize" for alias in node.names)
                   for node in ast.walk(parsed))
    assert "summarize" not in {node.id for node in ast.walk(parsed) if isinstance(node, ast.Name)}
    assert "summarize" not in {node.attr for node in ast.walk(parsed) if isinstance(node, ast.Attribute)}
