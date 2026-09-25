"""Pure Stage 4D report projection over already-loaded history and Stage 4C evidence.

The caller supplies canonical Stats for each calendar bucket and one widened
Stage 4C Timeline. This module selects no source and performs no I/O. Calendar
edges are whole UTC hours, so hour-local activity segments never need their
energy ingredients prorated at a report boundary.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as Date, timedelta

from .activity import (ACTIVITY_RULE_VERSION, Activity, ActivitySegment, Boundary, Compressor,
                       Timeline, Unavailable, activity_events, compressor_off_intervals,
                       compressor_runs, defrosts)
from .aggregation import PAIRS, POWER_CHANNELS, RECORDED, Stats, combine_maps, cop, coverage_percent, energy_kwh
from .minute import MINUTE, iso_utc
from .timegrid import HOUR, LOCAL_TZ_NAME, bucket_edges, local_midnight

HEATING_ACTIVITIES = (Activity.CO, Activity.DHW, Activity.TRANSITION)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_CONSUMPTION = ("co_power_consumption", "dhw_power_consumption")
_PRODUCTION = ("co_power_production", "dhw_power_production")


class ReportInvariantError(ValueError):
    """Already-loaded facts cannot describe one truthful report."""


class ReportActivityUnavailable(ValueError):
    """Recorded history in the requested range lacks Stage 4C activity evidence."""


class ReportUnrepresentable(ValueError):
    """A valid calendar period cannot be represented by the Warsaw hour grid."""


def _date(value: str) -> Date:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ValueError("report date must be YYYY-MM-DD without a time component")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid report calendar date") from None


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    kind: str
    from_date: Date
    to_date: Date
    start: int
    end: int
    bucket: str
    edges: tuple[tuple[int, int], ...]


def resolve_period(kind: str, *, date: str | None = None,
                   from_date: str | None = None, to_date: str | None = None) -> ReportPeriod:
    """Resolve exactly one frozen Warsaw local-date form to UTC calendar buckets."""
    if kind == "custom":
        if date is not None or from_date is None or to_date is None:
            raise ValueError("custom report requires from and to dates only")
        first, last = _date(from_date), _date(to_date)
        if not 0 < (last - first).days <= 31:
            raise ValueError("custom report must span 1..31 local days")
    elif kind in ("day", "week", "month"):
        if date is None or from_date is not None or to_date is not None:
            raise ValueError(f"{kind} report requires an anchor date only")
        anchor = _date(date)
        try:
            if kind == "day":
                first, last = anchor, anchor + timedelta(days=1)
            elif kind == "week":
                first = anchor - timedelta(days=anchor.weekday())
                last = first + timedelta(days=7)
            else:
                first = anchor.replace(day=1)
                last = Date(first.year + (1 if first.month == 12 else 0),
                            1 if first.month == 12 else first.month + 1, 1)
        except (OverflowError, OSError, ValueError) as exc:
            raise ReportUnrepresentable("report period exceeds the calendar conversion range") from exc
    else:
        raise ValueError(f"unknown report period {kind!r}")

    bucket = "1h" if kind == "day" else "1d"
    try:
        start, end = local_midnight(first), local_midnight(last)
        edges = tuple(bucket_edges(start, end, bucket))
    except (OverflowError, OSError, ValueError) as exc:
        raise ReportUnrepresentable("report calendar conversion is unrepresentable") from exc
    if start % HOUR or end % HOUR or any(a % HOUR or b % HOUR for a, b in edges):
        raise ReportUnrepresentable("Warsaw report boundaries are not whole UTC hours")
    if (not edges or edges[0][0] != start
            or edges[-1][1] != end or any(a >= b for a, b in edges)
            or any(edges[i][1] != edges[i + 1][0] for i in range(len(edges) - 1))):
        raise ReportInvariantError("report calendar buckets are not contiguous whole UTC hours")
    return ReportPeriod(kind, first, last, start, end, bucket, edges)


def effective_to(period: ReportPeriod, closed_until: int) -> int:
    """Settled extraction endpoint; never mutates the Stage 4C frontier."""
    return min(period.end, max(period.start, closed_until))


def _minutes(a: int, b: int) -> int:
    return max(0, b - a) // MINUTE


def coverage(start: int, end: int, *, now: float, closed_until: int,
             recorded_minutes: int) -> dict:
    """Factual settled/unsettled/future partition of one whole-minute interval."""
    if (not math.isfinite(now) or start % MINUTE or end % MINUTE or closed_until % MINUTE
            or start >= end or closed_until > math.floor(now / MINUTE) * MINUTE):
        raise ReportInvariantError("invalid report coverage interval or observation frontier")
    frontier = math.floor(now / MINUTE) * MINUTE
    settled = _minutes(start, min(end, closed_until))
    unsettled = _minutes(max(start, closed_until), min(end, frontier))
    future = _minutes(max(start, frontier), end)
    calendar = _minutes(start, end)
    if not 0 <= recorded_minutes <= settled or settled + unsettled + future != calendar:
        raise ReportInvariantError("report coverage counts disagree")
    return {
        "calendar_minutes": calendar,
        "settled_minutes": settled,
        "unsettled_minutes": unsettled,
        "future_minutes": future,
        "recorded_minutes": recorded_minutes,
        "gap_minutes": settled - recorded_minutes,
        "coverage_percent": coverage_percent(recorded_minutes, settled),
    }


def _known(stats: Stats | None, recorded: int, series: str) -> int:
    if stats is None:
        return 0
    n = stats.n
    if n <= 0 or n > recorded:
        raise ReportInvariantError(f"{series} known minutes exceed recorded minutes")
    if (not all(math.isfinite(v) for v in (stats.sum, stats.min, stats.max, stats.last))
            or not stats.min <= stats.last <= stats.max):
        raise ReportInvariantError(f"{series} has invalid statistics")
    return n


def _channel(stats: Stats | None, recorded: int, series: str) -> dict:
    minutes = _known(stats, recorded, series)
    return {"kwh": energy_kwh(stats), "minutes": minutes,
            "unknown_minutes": recorded - minutes}


def _aggregate(channels: Mapping[str, dict], keys: tuple[str, str]) -> dict:
    known = [channels[key]["kwh"] for key in keys if channels[key]["minutes"]]
    return {"observed_kwh": sum(known) if known else None,
            "unknown_channel_minutes": sum(channels[key]["unknown_minutes"] for key in keys)}


def _energy(stats: Mapping[str, Stats], recorded: int) -> dict:
    channels = {key: _channel(stats.get(key), recorded, key) for key in POWER_CHANNELS}
    pairs = {}
    for name, (input_key, output_key) in PAIRS.items():
        pair_in, pair_out = stats.get(input_key), stats.get(output_key)
        _known(pair_in, recorded, input_key)
        _known(pair_out, recorded, output_key)
        try:
            pairs[name] = cop(pair_in, pair_out)
        except ValueError as exc:
            raise ReportInvariantError(str(exc)) from exc
        if pair_in is not None:
            required = POWER_CHANNELS if name == "total" else (POWER_CHANNELS[:2] if name == "co"
                                                               else POWER_CHANNELS[2:])
            if any(channels[key]["minutes"] < pair_in.n for key in required):
                raise ReportInvariantError(f"{name} paired minutes exceed channel known minutes")
    return {"channels": channels,
            "consumption": _aggregate(channels, _CONSUMPTION),
            "production": _aggregate(channels, _PRODUCTION),
            "cop": pairs}


def _class_energy(minutes: int, stats: Mapping[str, Stats]) -> dict:
    return {"minutes": minutes, **_energy(stats, minutes)}


def _technical(stats: Mapping[str, Stats], compressor_minutes: Mapping[str, int],
               recorded: int) -> dict:
    outside = stats.get("outside_temp")
    freq = stats.get("compressor_freq")
    outside_n = _known(outside, recorded, "outside_temp")
    freq_n = _known(freq, recorded, "compressor_freq")
    on = compressor_minutes[Compressor.ON.value]
    off = compressor_minutes[Compressor.OFF.value]
    if freq_n != on + off:
        raise ReportInvariantError("compressor frequency known minutes disagree with compressor states")
    return {
        "outside_temp": {"avg": None if outside is None else outside.avg,
                         "min": None if outside is None else outside.min,
                         "max": None if outside is None else outside.max,
                         "minutes": outside_n},
        "compressor_freq": {"avg": None if freq is None else freq.avg,
                            "max": None if freq is None else freq.max,
                            "minutes": freq_n,
                            "active_avg": None if on == 0 else freq.sum / on},
    }


@dataclass(slots=True)
class _Events:
    observed_starts: int = 0
    observed_stops: int = 0
    complete_runs: list[int] = field(default_factory=list)
    exact_off_intervals: list[int] = field(default_factory=list)
    defrost_events: int = 0
    activity_events: dict[str, int] = field(default_factory=lambda: {a.value: 0 for a in Activity})
    observed_defrost_seconds: float = 0.0

    def add(self, other: _Events) -> None:
        self.observed_starts += other.observed_starts
        self.observed_stops += other.observed_stops
        self.complete_runs.extend(other.complete_runs)
        self.exact_off_intervals.extend(other.exact_off_intervals)
        self.defrost_events += other.defrost_events
        for key, value in other.activity_events.items():
            self.activity_events[key] += value
        self.observed_defrost_seconds += other.observed_defrost_seconds


def _duration(values: Sequence[int]) -> dict:
    return {"count": len(values), "total_minutes": sum(values),
            "min_minutes": min(values) if values else None,
            "max_minutes": max(values) if values else None,
            "mean_minutes": sum(values) / len(values) if values else None}


def _events(events: _Events) -> dict:
    return {"observed_starts": events.observed_starts,
            "observed_stops": events.observed_stops,
            "complete_runs": _duration(events.complete_runs),
            "exact_off_intervals": _duration(events.exact_off_intervals),
            "defrost_events": events.defrost_events,
            "activity_events": dict(events.activity_events),
            "observed_defrost_seconds": events.observed_defrost_seconds}


@dataclass(slots=True)
class _FactsInput:
    stats: dict[str, Stats] = field(default_factory=dict)
    class_stats: dict[Activity, dict[str, Stats]] = field(default_factory=lambda: {a: {} for a in Activity})
    class_minutes: dict[Activity, int] = field(default_factory=lambda: {a: 0 for a in Activity})
    heating_stats: dict[str, Stats] = field(default_factory=dict)
    heating_minutes: int = 0
    compressor_minutes: dict[str, int] = field(default_factory=lambda: {c.value: 0 for c in Compressor})
    events: _Events = field(default_factory=_Events)

    def add_segment(self, segment: ActivitySegment) -> None:
        if segment.energy is None:
            raise ReportInvariantError("report segment lacks complete energy ingredients")
        self.class_minutes[segment.activity] += segment.minutes
        self.class_stats[segment.activity] = combine_maps(self.class_stats[segment.activity], segment.energy)
        self.compressor_minutes[segment.compressor.value] += segment.minutes
        if segment.activity in HEATING_ACTIVITIES:
            self.heating_minutes += segment.minutes
            self.heating_stats = combine_maps(self.heating_stats, segment.energy)
        self.events.observed_defrost_seconds += segment.observed_defrost_seconds

    def add_bucket(self, other: _FactsInput) -> None:
        self.stats = combine_maps(self.stats, other.stats)
        for activity in Activity:
            self.class_minutes[activity] += other.class_minutes[activity]
            self.class_stats[activity] = combine_maps(self.class_stats[activity], other.class_stats[activity])
        self.heating_minutes += other.heating_minutes
        self.heating_stats = combine_maps(self.heating_stats, other.heating_stats)
        for key, value in other.compressor_minutes.items():
            self.compressor_minutes[key] += value
        self.events.add(other.events)


def _facts(start: int, end: int, source: _FactsInput, *, now: float, closed_until: int) -> dict:
    rec_stats = source.stats.get(RECORDED)
    recorded = 0 if rec_stats is None else rec_stats.n
    if rec_stats is not None and (recorded <= 0 or rec_stats.sum != recorded
                                  or rec_stats.min != 1 or rec_stats.max != 1 or rec_stats.last != 1):
        raise ReportInvariantError("recorded series is not one per canonical minute")
    cov = coverage(start, end, now=now, closed_until=closed_until, recorded_minutes=recorded)
    if sum(source.class_minutes.values()) != recorded or sum(source.compressor_minutes.values()) != recorded:
        raise ReportInvariantError("activity or compressor minutes do not partition recorded history")
    energy = _energy(source.stats, recorded)
    classes = {activity.value: _class_energy(source.class_minutes[activity], source.class_stats[activity])
               for activity in Activity}
    for key in POWER_CHANNELS:
        if sum(item["channels"][key]["minutes"] for item in classes.values()) != energy["channels"][key]["minutes"]:
            raise ReportInvariantError(f"class known-minute partition disagrees for {key}")
    for name, (pair_in, pair_out) in PAIRS.items():
        if sum((source.class_stats[a].get(pair_in).n if pair_in in source.class_stats[a] else 0)
               for a in Activity) != energy["cop"][name]["paired_minutes"]:
            raise ReportInvariantError(f"class paired-minute partition disagrees for {name}")
    activity = {"compressor_minutes": dict(source.compressor_minutes),
                "classes": classes,
                "heating": {"activities": [a.value for a in HEATING_ACTIVITIES],
                            **_class_energy(source.heating_minutes, source.heating_stats)}}
    return {"coverage": cov, "energy": energy, "activity": activity,
            "events": _events(source.events),
            "technical": _technical(source.stats, source.compressor_minutes, recorded)}


@dataclass(frozen=True, slots=True)
class ReportInput:
    """Already-loaded canonical bucket partials and one widened Stage 4C timeline."""

    period: ReportPeriod
    now: float
    closed_until: int
    history_buckets: Sequence[Mapping[str, Stats]]
    timeline: Timeline
    segment_rule_version: int = ACTIVITY_RULE_VERSION


def compose_report(source: ReportInput) -> dict:
    """Compose totals and calendar buckets without persistence, HTTP or repeated span scans.

    Segment and span attribution uses binary search over ordered bucket starts:
    O((segments + spans) log buckets + buckets). Stage 4C span extraction runs once.
    """
    period = source.period
    if (not period.edges or period.edges[0][0] != period.start or period.edges[-1][1] != period.end
            or any(a >= b or a % HOUR or b % HOUR for a, b in period.edges)
            or any(period.edges[i][1] != period.edges[i + 1][0]
                   for i in range(len(period.edges) - 1))):
        raise ReportInvariantError("report bucket edges are not an ordered whole-hour partition")
    if len(source.history_buckets) != len(period.edges):
        raise ReportInvariantError("history partials do not match calendar buckets")
    if source.segment_rule_version != ACTIVITY_RULE_VERSION:
        raise ReportInvariantError("unsupported activity segment rule version")
    if source.timeline.closed_until != source.closed_until:
        raise ReportInvariantError("activity and observation frontiers disagree")
    limit = effective_to(period, source.closed_until)
    if limit > period.start and (source.timeline.start > period.start or source.timeline.end < limit):
        raise ReportInvariantError("activity evidence does not cover the settled report range")
    starts = tuple(a for a, _ in period.edges)
    buckets = [_FactsInput(stats=dict(partial)) for partial in source.history_buckets]

    def index(ts: int) -> int | None:
        if not period.start <= ts < period.end:
            return None
        i = bisect_right(starts, ts) - 1
        if i < 0 or ts >= period.edges[i][1]:
            raise ReportInvariantError("report bucket attribution failed")
        return i

    for item in source.timeline.items:
        if item.end <= period.start or item.start >= limit:
            continue
        if isinstance(item, Unavailable):
            raise ReportActivityUnavailable("report intersects activity-unavailable history")
        if not isinstance(item, ActivitySegment):
            continue
        i = index(item.start)
        if item.start < period.start or item.end > limit or item.end > period.edges[i][1]:
            raise ReportInvariantError("report activity segment crosses a settled bucket edge")
        buckets[i].add_segment(item)

    # Compute each span family once. No per-bucket summarize or timeline scan.
    activities = activity_events(source.timeline)
    defrost_spans = defrosts(source.timeline)
    runs = compressor_runs(source.timeline)
    off_intervals = compressor_off_intervals(source.timeline)
    # A report edge is not evidence of a span edge. Timeline has no proven absolute
    # left floor, so an intersecting span needs decisive evidence on both sides.
    for spans in (activities, defrost_spans, runs, off_intervals):
        for span in spans:
            if span.start >= limit or span.end <= period.start:
                continue
            if span.end_boundary is Boundary.OUTSIDE_EVIDENCE:
                raise ReportInvariantError("intersecting report span ends outside examined evidence")
            if span.start_boundary is Boundary.OUTSIDE_EVIDENCE:
                raise ReportInvariantError("intersecting report span starts outside examined evidence")
    overlap_runs = sum(span.start < limit and span.end > period.start for span in runs)
    overlap_defrosts = sum(span.start < limit and span.end > period.start for span in defrost_spans)
    for span in activities:
        i = index(span.start)
        if i is not None and span.start < limit:
            buckets[i].events.activity_events[span.state.value] += 1
    for span in defrost_spans:
        i = index(span.start)
        if i is not None and span.start < limit:
            buckets[i].events.defrost_events += 1
    for span in runs:
        i = index(span.start)
        if i is not None and span.start < limit:
            if span.start_observed:
                buckets[i].events.observed_starts += 1
            if span.start_observed and span.end_observed:
                buckets[i].events.complete_runs.append(span.minutes)
        stop = index(span.end - MINUTE)
        if span.end_observed and stop is not None and span.end - MINUTE < limit:
            buckets[stop].events.observed_stops += 1
    for span in off_intervals:
        i = index(span.start)
        if i is not None and span.start < limit and span.start_observed and span.end_observed:
            buckets[i].events.exact_off_intervals.append(span.minutes)

    total = _FactsInput()
    rendered = []
    for (a, b), source_bucket in zip(period.edges, buckets, strict=True):
        facts = _facts(a, b, source_bucket, now=source.now, closed_until=source.closed_until)
        rendered.append({"start": iso_utc(a), "end": iso_utc(b), **facts})
        total.add_bucket(source_bucket)
    totals = _facts(period.start, period.end, total, now=source.now, closed_until=source.closed_until)
    totals["compressor_runs_overlapping"] = overlap_runs
    totals["defrosts_overlapping"] = overlap_defrosts
    return {
        "period": {"kind": period.kind, "from_date": period.from_date.isoformat(),
                   "to_date": period.to_date.isoformat(), "from": iso_utc(period.start),
                   "to": iso_utc(period.end), "timezone": LOCAL_TZ_NAME, "bucket": period.bucket},
        "observation": {"now": iso_utc(source.now), "closed_until": iso_utc(source.closed_until),
                        "effective_to": iso_utc(limit)},
        "segment_rule_version": source.segment_rule_version,
        "evidence": {"from": iso_utc(source.timeline.start) if limit > period.start else None,
                     "to": iso_utc(source.timeline.end) if limit > period.start else None},
        "totals": totals,
        "buckets": rendered,
    }
