"""Stage 4C-C: activity read model behind ``/api/v1/activity`` and ``/api/v1/activity/live``.

Read-only; one storage session per request. ``docs/ARCHITECTURE.md`` §25.3.3 is the contract.

Each UTC hour of evidence has exactly one source, chosen from stored facts:

* durable ``activity_segment_1h`` rows, when present (never recomputed from raw);
* else ``build_segments`` of its raw ``sample_1m`` minutes (unrolled or awaiting backfill);
* else, when ``rollup_1h`` proves minutes were recorded, activity is *unavailable*
  (raw purged before Stage 4C-B);
* else nothing was recorded: ordinary gaps.

The loader starts from the requested range plus its adjacent minutes and widens only while
a span that intersects the range still ends at the edge of the examined evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .activity import (
    ACTIVITY_COLUMNS, ACTIVITY_RULE_VERSION, POWER_CHANNELS, Activity, ActivitySegment, Boundary, Gap,
    ObservedSpan, Unavailable, activity_events, build_segments, classify, compressor_off_intervals,
    compressor_runs, decode_segments, defrosts, summarize, timeline,
)
from .aggregation import PAIRS, RECORDED, Stats, cop, energy_kwh
from .minute import MINUTE, floor_minute, iso_utc
from .storage import Session, Storage
from .timegrid import DAY, HOUR, Unrepresentable, ceil_hour, floor_hour

MAX_RANGE_SECONDS = 31 * DAY + HOUR  # the longest calendar month, including an autumn DST hour
MAX_WIDENING_STEP = 7 * DAY


class ActivityUnavailable(Exception):
    """The requested range needs activity detail that was purged before durable activity existed.

    Canonical minutes provably existed (their ``rollup_1h`` row survives), so this is not
    "nothing recorded"; the minute order needed for activity is gone and is never inferred.
    """

    def __init__(self, hour_ts: int):
        super().__init__(
            f"activity detail is unavailable from {iso_utc(hour_ts)}: its canonical minutes were purged"
            " before durable activity existed; this is not a range without recorded data")
        self.hour_ts = hour_ts


class _Evidence:
    """Hour-by-hour activity evidence loaded from one session."""

    def __init__(self, session: Session, closed_until: int):
        self.session, self.closed_until = session, closed_until
        self.segments: dict[int, list[ActivitySegment]] = {}
        self.unavailable: set[int] = set()

    def load(self, lo: int, hi: int) -> None:
        if lo >= hi:
            return
        s = self.session
        durable: dict[int, list[ActivitySegment]] = {}
        for segment in decode_segments(s.read_activity_segments(lo, hi)):
            durable.setdefault(floor_hour(segment.start), []).append(segment)
        rolled = {hour for hour, *_ in s.read_rollup(lo, hi, [RECORDED])}
        missing = [h for h in range(lo, hi, HOUR) if h not in durable]
        raw: dict[int, list] = {}
        for a, b in _runs(missing):
            for ts, values in s.read_minutes(a, min(b, self.closed_until), ACTIVITY_COLUMNS):
                raw.setdefault(floor_hour(ts), []).append((ts, values))
        for hour in range(lo, hi, HOUR):
            if hour in durable:
                self.segments[hour] = durable[hour]
            elif hour in raw:
                self.segments[hour] = build_segments(raw[hour])
            elif hour in rolled:
                self.unavailable.add(hour)

    def timeline(self, lo: int, end: int):
        closed = self.closed_until
        pieces = [p for hour in sorted(self.segments) if lo <= hour < end
                  for seg in self.segments[hour] if (p := seg.clip(seg.start, closed)) is not None]
        lost = [(h, h + HOUR) for h in sorted(self.unavailable) if lo <= h < end]
        return timeline(pieces, lo, end, closed, lost)


def _runs(hours: list[int]) -> Iterable[tuple[int, int]]:
    """Contiguous ``[a, b)`` ranges of ascending whole hours."""
    start = previous = None
    for hour in hours:
        if previous is not None and hour != previous + HOUR:
            yield start, previous + HOUR
            start = None
        start = hour if start is None else start
        previous = hour
    if start is not None:
        yield start, previous + HOUR


def _all_spans(tl) -> list[ObservedSpan]:
    return activity_events(tl) + compressor_runs(tl) + compressor_off_intervals(tl) + defrosts(tl)


def _undecided(tl, a: int, b: int) -> tuple[bool, bool]:
    """Does a span intersecting ``[a, b)`` still end at the left/right edge of the evidence?"""
    left = right = False
    for span in _all_spans(tl):
        if span.start < b and span.end > a:
            left |= span.start_boundary is Boundary.OUTSIDE_EVIDENCE
            right |= span.end_boundary is Boundary.OUTSIDE_EVIDENCE
    return left, right


def query(storage: Storage, start: int, end: int, now: float) -> dict:
    """Activity facts for minute-aligned ``start < end``.

    Raises ``Unrepresentable`` (range too long), ``ActivityUnavailable`` (422s) and
    ``ActivityRecordInvalid`` (stored activity corrupt; never answered from raw instead).
    """
    if end - start > MAX_RANGE_SECONDS:
        raise Unrepresentable(f"an activity range may span at most {MAX_RANGE_SECONDS // DAY} days and one"
                              " hour; the range is not truncated")
    closed_until = floor_minute(now)
    cap = ceil_hour(closed_until)  # no closed minute lies at or beyond it
    with storage.session() as s:
        evidence = _Evidence(s, closed_until)
        lo = floor_hour(max(0, start - MINUTE))
        hi = max(lo, min(ceil_hour(end + MINUTE), cap))
        evidence.load(lo, hi)
        lost = [h for h in sorted(evidence.unavailable)
                if h < min(end, closed_until) and h + HOUR > start]
        if lost:
            raise ActivityUnavailable(lost[0])
        step = HOUR
        while True:
            tl = evidence.timeline(lo, max(hi, end))
            left, right = _undecided(tl, start, end)
            left, right = left and lo > 0, right and hi < cap
            if not (left or right):
                break
            if left:
                evidence.load(max(0, lo - step), lo)
                lo = max(0, lo - step)
            if right:
                evidence.load(hi, min(cap, hi + step))
                hi = min(cap, hi + step)
            step = min(2 * step, MAX_WIDENING_STEP)
    return _response(tl, start, end, now, closed_until, lo, hi)


# ------------------------------------------------------------------ serialization

def _energy(stats: Mapping[str, Stats] | None) -> tuple[dict | None, dict | None]:
    if stats is None:  # a piece clipped out of a stored segment carries no ingredients
        return None, None
    energy = {k: {"kwh": energy_kwh(stats.get(k)), "minutes": stats[k].n if k in stats else 0}
              for k in POWER_CHANNELS}
    ratios = {name: cop(stats.get(pair_in), stats.get(pair_out))
              for name, (pair_in, pair_out) in PAIRS.items()}
    return energy, ratios


def _span(span: ObservedSpan, a: int, b: int) -> dict:
    """The full observed span, and separately its overlap with the requested range."""
    overlap_start, overlap_end = max(span.start, a), min(span.end, b)
    return {
        "start": iso_utc(span.start),
        "end": iso_utc(span.end),
        "minutes": span.minutes,
        "overlap_start": iso_utc(overlap_start),
        "overlap_end": iso_utc(overlap_end),
        "overlap_minutes": (overlap_end - overlap_start) // MINUTE,
        "starts_before_range": span.start < a,
        "ends_after_range": span.end > b,
        "start_boundary": span.start_boundary.value,
        "end_boundary": span.end_boundary.value,
        "start_observed": span.start_observed,
        "end_observed": span.end_observed,
    }


def _with_energy(span: ObservedSpan) -> dict:
    energy, ratios = _energy(span.energy)
    return {"energy": energy, "cop": ratios}


def _composition(span: ObservedSpan) -> dict:
    counts = {a.value: 0 for a in Activity}
    for segment in span.segments:
        counts[segment.activity.value] += segment.minutes
    return counts


def _overlap_defrost_seconds(span: ObservedSpan, a: int, b: int) -> float:
    return sum(p.observed_defrost_seconds for seg in span.segments if (p := seg.clip(a, b)) is not None)


def _distribution(minutes: tuple[int, ...]) -> dict:
    return {
        "count": len(minutes),
        "minutes": list(minutes),
        "total_minutes": sum(minutes),
        "min_minutes": min(minutes, default=None),
        "max_minutes": max(minutes, default=None),
        "mean_minutes": sum(minutes) / len(minutes) if minutes else None,
    }


def _intersecting(spans: Iterable[ObservedSpan], a: int, b: int) -> list[ObservedSpan]:
    return [s for s in spans if s.start < b and s.end > a]


def _response(tl, a: int, b: int, now: float, closed_until: int, lo: int, hi: int) -> dict:
    summary = summarize(tl, a, b)
    items = []
    for event in _intersecting(activity_events(tl), a, b):
        items.append((event.start, {
            "type": "activity", "activity": event.state.value,
            "start": iso_utc(max(event.start, a)), "end": iso_utc(min(event.end, b)),
            "minutes": (min(event.end, b) - max(event.start, a)) // MINUTE,
            "event": {**_span(event, a, b), **_with_energy(event),
                      "observed_defrost_seconds": event.observed_defrost_seconds},
        }))
    for item in tl.items:
        if isinstance(item, Gap) and item.start < b and item.end > a:
            ga, gb = max(item.start, a), min(item.end, b)
            items.append((ga, {"type": "gap", "activity": None, "start": iso_utc(ga), "end": iso_utc(gb),
                               "minutes": (gb - ga) // MINUTE, "event": None}))
        elif isinstance(item, Unavailable) and item.start < b and item.end > a:
            raise AssertionError("activity-unavailable evidence inside the requested range")
    if closed_until < b:
        oa = max(a, closed_until)
        items.append((oa, {"type": "open", "activity": None, "start": iso_utc(oa), "end": iso_utc(b),
                           "minutes": (b - oa) // MINUTE, "event": None}))
    runs = [{**_span(r, a, b), **_with_energy(r), "activity_minutes": _composition(r),
             "observed_defrost_seconds": r.observed_defrost_seconds}
            for r in _intersecting(compressor_runs(tl), a, b)]
    offs = [{**_span(i, a, b), "exact": i.start_observed and i.end_observed}
            for i in _intersecting(compressor_off_intervals(tl), a, b)]
    frosts = [{**_span(d, a, b), "observed_defrost_seconds": d.observed_defrost_seconds,
               "overlap_observed_defrost_seconds": _overlap_defrost_seconds(d, a, b)}
              for d in _intersecting(defrosts(tl), a, b)]
    return {
        "from": iso_utc(a),
        "to": iso_utc(b),
        "now": iso_utc(now),
        "closed_until": iso_utc(closed_until),
        "segment_rule_version": summary.segment_rule_version,
        "evidence": {"from": iso_utc(lo), "to": iso_utc(max(hi, lo))},
        "summary": {
            "closed_minutes": summary.closed_minutes,
            "recorded_minutes": summary.recorded_minutes,
            "gap_minutes": summary.gap_minutes,
            "activity_minutes": dict(summary.activity_minutes),
            "compressor_minutes": dict(summary.compressor_minutes),
            "observed_starts": summary.observed_starts,
            "observed_stops": summary.observed_stops,
            "compressor_runs_overlapping": summary.compressor_runs_overlapping,
            "defrosts_overlapping": summary.defrosts_overlapping,
            "complete_runs": _distribution(summary.complete_run_minutes),
            "exact_off_intervals": _distribution(summary.exact_off_interval_minutes),
            "observed_defrost_seconds": summary.observed_defrost_seconds,
        },
        "timeline": [item for _, item in sorted(items, key=lambda pair: pair[0])],
        "compressor_runs": runs,
        "compressor_off_intervals": offs,
        "defrosts": frosts,
    }


# ------------------------------------------------------------------ live

def live_activity(live: Mapping) -> dict:
    """Current activity from one ``/api/v1/live`` observation (``Recorder.live``).

    Only ``mode == "live"`` values (confirmed, fresh, non-retained, current epoch) are
    current evidence; a retained or absent value is an unknown input, and the one
    classifier's NULL rules decide the result. No storage is read.
    """
    metrics = live["metrics"]
    values = {k: metrics[k]["value"] if metrics[k]["mode"] == "live" else None for k in ACTIVITY_COLUMNS}
    activity, compressor = classify(values)
    return {
        "now": live["now"],
        "rule_version": ACTIVITY_RULE_VERSION,
        "activity": activity.value,
        "compressor": compressor.value,
        "all_inputs_live": all(metrics[k]["mode"] == "live" for k in ACTIVITY_COLUMNS),
        "mqtt": live["mqtt"],
        "inputs": {k: {"value": metrics[k]["value"], "mode": metrics[k]["mode"],
                       "received_at": metrics[k]["received_at"], "used": values[k] is not None}
                   for k in ACTIVITY_COLUMNS},
    }
