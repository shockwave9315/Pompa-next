"""Stage 4C activity domain truth: one interpretation of canonical minutes.

Pure functions only; no storage. ``docs/ARCHITECTURE.md`` §25.3 is the contract.

    canonical minute (ts, values)
      → classify: (Activity, Compressor) per recorded minute
      → build_segments: hour-local ordered ActivitySegments (what 4C-B persists)
      → timeline: segments plus explicit Gaps over an evidence window
      → activity_events / compressor_runs / compressor_off_intervals / defrosts
      → project onto a query range; summarize

Nothing is smoothed. A missing row is a ``Gap``, never an ``unknown`` minute,
and no span is ever bridged across a gap or an unknown minute. Every boundary
states what the adjacent minute proves (``Boundary``); a query range edge is
never evidence of a start, stop or continuation.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from .aggregation import PAIRS, POWER_CHANNELS, Stats, combine_maps, fold_minutes
from .minute import MINUTE
from .timegrid import HOUR, floor_hour

# Version of the persisted minute/segment interpretation: classification, the enum strings,
# segment grouping and ENERGY_SERIES. Once 4C-B persists segments, any change to them needs a
# new version, never an edit. Read-time projections and summaries are not versioned by it.
ACTIVITY_RULE_VERSION = 1

# Legacy-proven power evidence threshold, used only when the valve position is unknown.
POWER_ACTIVE_THRESHOLD_W = 100.0

# Additive energy/COP ingredients each segment carries: the canonical derived series.
ENERGY_SERIES: tuple[str, ...] = POWER_CHANNELS + tuple(s for pair in PAIRS.values() for s in pair)

_CO_SIDE = ("co_power_consumption", "co_power_production")
_DHW_SIDE = ("dhw_power_consumption", "dhw_power_production")
# The canonical columns a minute's interpretation and energy ingredients read.
ACTIVITY_COLUMNS: tuple[str, ...] = (
    "compressor_freq", "defrosting_state", "heatpump_state", "three_way_valve") + POWER_CHANNELS


class Activity(StrEnum):
    OFF = "off"
    IDLE = "idle"
    CO = "co"
    DHW = "dhw"
    TRANSITION = "transition"
    DEFROST = "defrost"
    UNKNOWN = "unknown"


class Compressor(StrEnum):
    OFF = "off"
    ON = "on"
    UNKNOWN = "unknown"


class Boundary(StrEnum):
    """What the minute adjacent to a span's edge proves about that edge."""

    OBSERVED = "observed"  # a consecutive recorded minute with a different, known state
    UNKNOWN = "unknown"  # a consecutive recorded minute whose state is unknown
    GAP = "gap"  # the consecutive minute closed without a recorded row
    OPEN = "open"  # the consecutive minute has not closed yet
    OUTSIDE_EVIDENCE = "outside_evidence"  # the consecutive minute was not examined


# ------------------------------------------------------------------ classification

def _power_side(values: Mapping[str, float | None], keys: tuple[str, str]) -> bool | None:
    """True: a channel proves activity; False: all channels known and below; None: unknown."""
    known = [values[k] for k in keys if values[k] is not None]
    if any(v > POWER_ACTIVE_THRESHOLD_W for v in known):
        return True
    return False if len(known) == len(keys) else None


def classify(values: Mapping[str, float | None]) -> tuple[Activity, Compressor]:
    """Interpret one recorded canonical minute. ``None`` is an unknown metric, never zero.

    Compressor state depends on ``compressor_freq`` alone. Activity precedence:
    unknown defrost → unknown; defrost > 0 → defrost; unknown compressor →
    unknown; stopped compressor → off/idle from ``heatpump_state``; running
    compressor → valve position, else >100 W CO/DHW power evidence.

    The result is one class per minute, not per-state seconds. A fractional
    ``heatpump_state`` is fully known and changed within the minute; with the
    compressor off it is ``idle``.
    """
    missing = [k for k in ACTIVITY_COLUMNS if k not in values]
    if missing:  # an unread column is not an unknown metric
        raise ValueError(f"minute lacks activity columns {missing}")
    freq = values["compressor_freq"]
    compressor = Compressor.UNKNOWN if freq is None else Compressor.ON if freq > 0 else Compressor.OFF
    defrost = values["defrosting_state"]
    if defrost is None:
        return Activity.UNKNOWN, compressor  # a defrost could not be excluded
    if defrost > 0:
        return Activity.DEFROST, compressor
    if compressor is Compressor.UNKNOWN:
        return Activity.UNKNOWN, compressor
    if compressor is Compressor.OFF:
        state = values["heatpump_state"]
        if state is None:
            return Activity.UNKNOWN, compressor
        return (Activity.OFF if state == 0 else Activity.IDLE), compressor
    valve = values["three_way_valve"]
    if valve is not None:
        # A fractional minute-mean valve is a transition; its intra-minute order is not known.
        activity = Activity.CO if valve == 0 else Activity.DHW if valve == 1 else Activity.TRANSITION
        return activity, compressor
    co, dhw = _power_side(values, _CO_SIDE), _power_side(values, _DHW_SIDE)
    if co and dhw:
        return Activity.TRANSITION, compressor
    if co and dhw is False:
        return Activity.CO, compressor
    if dhw and co is False:
        return Activity.DHW, compressor
    return Activity.UNKNOWN, compressor


# ------------------------------------------------------------------ segments

@dataclass(frozen=True, slots=True)
class ActivitySegment:
    """Consecutive recorded minutes of one UTC hour sharing one interpretation.

    ``defrost_fraction`` is the identical ``defrosting_state`` of every minute
    (``None`` = unknown), so a fractional boundary minute is never merged with
    full minutes and a segment can be clipped at any minute exactly.
    ``energy`` is the ``ENERGY_SERIES`` fold of exactly these minutes; it is
    ``None`` on a piece clipped out of a segment, which cannot carry it.
    """

    start: int
    minutes: int
    activity: Activity
    compressor: Compressor
    defrost_fraction: float | None
    energy: Mapping[str, Stats] | None

    @property
    def end(self) -> int:
        return self.start + self.minutes * MINUTE

    @property
    def observed_defrost_seconds(self) -> float:
        """Time-integrated observed defrost, not a physical transition second."""
        if self.activity is not Activity.DEFROST:
            return 0.0
        return self.defrost_fraction * MINUTE * self.minutes

    def clip(self, start: int, end: int) -> ActivitySegment | None:
        a, b = max(self.start, start), min(self.end, end)
        if a >= b:
            return None
        if (a, b) == (self.start, self.end):
            return self
        _check_minute(a, b)
        return replace(self, start=a, minutes=(b - a) // MINUTE, energy=None)


def _check_minute(*ts: int) -> None:
    for t in ts:
        if t % MINUTE:
            raise ValueError(f"unaligned minute instant {t}")


def build_segments(rows: Iterable[tuple[int, Mapping[str, float | None]]]) -> list[ActivitySegment]:
    """Recorded minutes ``(ts, values)`` in strictly ascending ``ts`` → ordered segments.

    A segment never crosses a UTC hour, a missing minute or a change of
    activity, compressor state or defrost fraction. Building each hour alone
    and concatenating gives the identical result.
    """
    out: list[ActivitySegment] = []
    group: list[tuple[int, Mapping[str, float | None]]] = []
    key = None
    previous = None

    def flush() -> None:
        activity, compressor, fraction = key
        out.append(ActivitySegment(group[0][0], len(group), activity, compressor, fraction,
                                   fold_minutes(group, ENERGY_SERIES)))

    for ts, values in rows:
        _check_minute(ts)
        if previous is not None and ts <= previous:
            raise ValueError(f"minutes must be ascending: {ts} after {previous}")
        activity, compressor = classify(values)
        minute_key = (activity, compressor, values["defrosting_state"])
        if group and (minute_key != key or ts != previous + MINUTE or floor_hour(ts) != floor_hour(previous)):
            flush()
            group = []
        group.append((ts, values))
        key, previous = minute_key, ts
    if group:
        flush()
    return out


def fold_energy(segments: Iterable[ActivitySegment]) -> dict[str, Stats] | None:
    """Chronological ``combine`` of segment energy ingredients: the input of ``energy_kwh``/``cop``.

    ``None`` when any piece was clipped out of a segment and so carries none.
    """
    acc: dict[str, Stats] = {}
    for segment in segments:
        if segment.energy is None:
            return None
        acc = combine_maps(acc, segment.energy)
    return acc


# ------------------------------------------------------------------ timeline

@dataclass(frozen=True, slots=True)
class Gap:
    """Closed minutes ``[start, end)`` without a recorded row: missing evidence, not unknown."""

    start: int
    end: int

    @property
    def minutes(self) -> int:
        return (self.end - self.start) // MINUTE


@dataclass(frozen=True, slots=True)
class Timeline:
    """Contiguous evidence over ``[start, min(end, closed_until))``.

    ``start``/``end`` describe the evidence actually examined. A span edge at
    either limit (other than an unclosed right edge, ``open``) is
    ``outside_evidence``: it proves no start, stop or continuation, so wider
    evidence can prove more boundary facts.

    Minutes at or after ``closed_until`` have not closed yet; they are neither
    gaps nor unknown and hold no items.
    """

    start: int
    end: int
    closed_until: int
    items: tuple[ActivitySegment | Gap, ...]

    @property
    def segments(self) -> tuple[ActivitySegment, ...]:
        return tuple(i for i in self.items if isinstance(i, ActivitySegment))


def timeline(segments: Iterable[ActivitySegment], start: int, end: int, closed_until: int) -> Timeline:
    """Place ascending segments on the evidence window ``[start, end)`` with explicit gaps.

    ``closed_until`` is the first minute that has not closed (for a wall clock
    ``now``: ``floor_minute(now)``). Segments may extend past the window and are
    clipped to it; a segment reaching past ``closed_until`` is impossible
    evidence and is rejected.
    """
    _check_minute(start, end, closed_until)
    if end < start:
        raise ValueError("timeline end precedes its start")
    limit = max(start, min(end, closed_until))
    items: list[ActivitySegment | Gap] = []
    cursor, previous_end = start, None
    for segment in segments:
        if previous_end is not None and segment.start < previous_end:
            raise ValueError("segments must be ascending and non-overlapping")
        previous_end = segment.end
        if segment.minutes <= 0:
            raise ValueError("a segment needs at least one minute")
        if segment.end > closed_until:
            raise ValueError(f"segment at {segment.start} reaches past closed minute {closed_until}")
        piece = segment.clip(start, end)
        if piece is None:
            continue
        if piece.start > cursor:
            items.append(Gap(cursor, piece.start))
        items.append(piece)
        cursor = piece.end
    if cursor < limit:
        items.append(Gap(cursor, limit))
    return Timeline(start, end, closed_until, tuple(items))


# ------------------------------------------------------------------ spans

@dataclass(frozen=True, slots=True)
class ObservedSpan:
    """Maximal consecutive recorded minutes in one state, never bridged across a gap or unknown.

    ``state`` is an ``Activity`` for activity events (a defrost event has
    ``Activity.DEFROST``) or a ``Compressor`` for compressor runs and off
    intervals. Duration is minute-resolution observed evidence.
    """

    state: Activity | Compressor
    start: int
    end: int
    start_boundary: Boundary
    end_boundary: Boundary
    segments: tuple[ActivitySegment, ...]

    @property
    def minutes(self) -> int:
        return (self.end - self.start) // MINUTE

    @property
    def start_observed(self) -> bool:
        return self.start_boundary is Boundary.OBSERVED

    @property
    def end_observed(self) -> bool:
        return self.end_boundary is Boundary.OBSERVED

    @property
    def energy(self) -> dict[str, Stats] | None:
        """Chronological fold of the segments' canonical energy ingredients."""
        return fold_energy(self.segments)

    @property
    def observed_defrost_seconds(self) -> float:
        return sum(s.observed_defrost_seconds for s in self.segments)


# Names for the three uses of one span shape; a run is not a claim about the physical compressor.
ActivityEvent = ObservedSpan
ObservedCompressorRun = ObservedSpan
CompressorOffInterval = ObservedSpan


def _boundary(tl: Timeline, neighbour: ActivitySegment | Gap | None, unknown, edge: int,
              after: bool) -> Boundary:
    if neighbour is None:
        return Boundary.OPEN if after and edge >= tl.closed_until else Boundary.OUTSIDE_EVIDENCE
    if isinstance(neighbour, Gap):
        return Boundary.GAP
    return Boundary.UNKNOWN if unknown(neighbour) else Boundary.OBSERVED


def _spans(tl: Timeline, state_of, wanted, unknown=None) -> list[ObservedSpan]:
    """``unknown(neighbour)``: the adjacent segment cannot prove the span's edge.

    By default that is an unknown value of the span's own state.
    """
    if unknown is None:
        def unknown(segment):
            return state_of(segment).value == "unknown"

    items, out, i = tl.items, [], 0
    while i < len(items):
        item = items[i]
        if isinstance(item, Gap) or state_of(item) not in wanted:
            i += 1
            continue
        state, j = state_of(item), i
        while j + 1 < len(items) and isinstance(items[j + 1], ActivitySegment) \
                and state_of(items[j + 1]) == state:
            j += 1
        segments = items[i:j + 1]
        before = items[i - 1] if i else None
        after = items[j + 1] if j + 1 < len(items) else None
        out.append(ObservedSpan(state, segments[0].start, segments[-1].end,
                                _boundary(tl, before, unknown, segments[0].start, False),
                                _boundary(tl, after, unknown, segments[-1].end, True),
                                tuple(segments)))
        i = j + 1
    return out


def activity_events(tl: Timeline) -> list[ActivityEvent]:
    """Every maximal same-activity span, including off, idle and unknown."""
    return _spans(tl, lambda s: s.activity, frozenset(Activity))


def defrosts(tl: Timeline) -> list[ActivityEvent]:
    """Individual defrosts: contiguous defrost activity; separate defrosts stay separate.

    Edges are judged by the defrost signal itself: an adjacent known
    ``defrosting_state == 0`` proves the edge even when that minute's overall
    activity is unknown (e.g. unknown compressor or valve); only an adjacent
    ``defrosting_state NULL`` is unknown. ``activity_events`` instead reports
    the activity change, so its defrost events keep activity-based edges.
    """
    return _spans(tl, lambda s: s.activity, frozenset({Activity.DEFROST}),
                  unknown=lambda s: s.defrost_fraction is None)


def compressor_runs(tl: Timeline) -> list[ObservedCompressorRun]:
    """Observed compressor runs ("cycles"), independent of activity and of the operations counter.

    ``start_observed`` only when the immediately preceding minute is recorded
    with the compressor observed off; ``end_observed`` symmetrically.
    """
    return _spans(tl, lambda s: s.compressor, frozenset({Compressor.ON}))


def compressor_off_intervals(tl: Timeline) -> list[CompressorOffInterval]:
    """Compressor-off spans; exact between runs only when both boundaries are observed."""
    return _spans(tl, lambda s: s.compressor, frozenset({Compressor.OFF}))


# ------------------------------------------------------------------ range projection

@dataclass(frozen=True, slots=True)
class Projection:
    """A span clipped to a query range.

    ``starts_before_range``/``ends_after_range`` are true only when recorded
    minutes of the same span exist outside the range; the range edge alone is
    never evidence of continuation.
    """

    span: ObservedSpan
    start: int
    end: int
    segments: tuple[ActivitySegment, ...]

    @property
    def starts_before_range(self) -> bool:
        return self.span.start < self.start

    @property
    def ends_after_range(self) -> bool:
        return self.span.end > self.end

    @property
    def minutes(self) -> int:
        return (self.end - self.start) // MINUTE

    @property
    def observed_defrost_seconds(self) -> float:
        return sum(s.observed_defrost_seconds for s in self.segments)


def project(spans: Iterable[ObservedSpan], start: int, end: int) -> list[Projection]:
    _check_minute(start, end)
    out = []
    for span in spans:
        a, b = max(span.start, start), min(span.end, end)
        if a < b:
            pieces = tuple(p for p in (s.clip(start, end) for s in span.segments) if p is not None)
            out.append(Projection(span, a, b, pieces))
    return out


# ------------------------------------------------------------------ factual summary

@dataclass(frozen=True, slots=True)
class ActivitySummary:
    """Counts and durations over ``[start, end)``; facts only, no verdicts or thresholds.

    Minute counts are per-minute classifications, not exact per-state seconds;
    only defrost keeps an exact time-integrated duration. Compressor minutes are
    observed minute-resolution evidence, not exact physical runtime.

    A zero-valued minute proves the compressor off for that whole minute, so an
    observed start happened within the run's first minute and an observed stop
    within its last one; each is counted in the range holding that minute.
    Complete runs and exact off intervals are attributed to the range holding
    their first minute. ``*_overlapping`` counts every span intersecting the
    range: a span crossing a range edge counts in both adjacent ranges, so these
    counts are not additive across ranges; observed starts/stops are.

    Boundary-sensitive facts depend on the examined evidence window
    ``[evidence_start, evidence_end)``, not only on ``[start, end)``. Observed
    starts and stops are fixed once it contains the minute before ``start``
    and the minute at ``end``; complete runs and exact off intervals may need
    evidence arbitrarily far beyond the range, because a span can continue.
    ``segment_rule_version`` names the minute/segment interpretation the
    summary was read from; the summary itself is not versioned.
    """

    segment_rule_version: int
    start: int
    end: int
    evidence_start: int
    evidence_end: int
    closed_until: int
    closed_minutes: int
    recorded_minutes: int
    gap_minutes: int
    activity_minutes: Mapping[str, int]
    compressor_minutes: Mapping[str, int]
    observed_starts: int
    observed_stops: int
    compressor_runs_overlapping: int
    complete_run_minutes: tuple[int, ...]
    exact_off_interval_minutes: tuple[int, ...]
    defrosts_overlapping: int
    observed_defrost_seconds: float


def summarize(tl: Timeline, start: int, end: int) -> ActivitySummary:
    _check_minute(start, end)
    if not tl.start <= start <= end <= tl.end:
        raise ValueError("summary range must lie inside the timeline's evidence window")
    closed = max(0, min(end, tl.closed_until) - start) // MINUTE
    activity = {a.value: 0 for a in Activity}
    compressor = {c.value: 0 for c in Compressor}
    gaps = 0
    for item in tl.items:
        if isinstance(item, Gap):
            gaps += max(0, min(item.end, end) - max(item.start, start)) // MINUTE
            continue
        piece = item.clip(start, end)
        if piece is not None:
            activity[piece.activity.value] += piece.minutes
            compressor[piece.compressor.value] += piece.minutes
    runs = compressor_runs(tl)

    def within(t: int) -> bool:
        return start <= t < end

    defrost_pieces = project(defrosts(tl), start, end)
    return ActivitySummary(
        segment_rule_version=ACTIVITY_RULE_VERSION,
        start=start,
        end=end,
        evidence_start=tl.start,
        evidence_end=tl.end,
        closed_until=tl.closed_until,
        closed_minutes=closed,
        recorded_minutes=closed - gaps,
        gap_minutes=gaps,
        activity_minutes=activity,
        compressor_minutes=compressor,
        observed_starts=sum(1 for r in runs if r.start_observed and within(r.start)),
        observed_stops=sum(1 for r in runs if r.end_observed and within(r.end - MINUTE)),
        compressor_runs_overlapping=len(project(runs, start, end)),
        complete_run_minutes=tuple(r.minutes for r in runs
                                   if r.start_observed and r.end_observed and within(r.start)),
        exact_off_interval_minutes=tuple(i.minutes for i in compressor_off_intervals(tl)
                                         if i.start_observed and i.end_observed and within(i.start)),
        defrosts_overlapping=len(defrost_pieces),
        observed_defrost_seconds=sum(p.observed_defrost_seconds for p in defrost_pieces),
    )


# ------------------------------------------------------------------ persisted segment records

class ActivityRecordInvalid(ValueError):
    """A persisted activity segment cannot describe version-1 activity truth; never coerced."""


# (start_ts, minutes, rule_version, activity, compressor, defrost_fraction, energy_json):
# one ``activity_segment_1h`` row. Stage 4C-B, docs/ARCHITECTURE.md §25.3.2.
SegmentRecord = tuple[int, int, int, str, str, float | None, str]

_ACTIVITY_FOR_COMPRESSOR = {
    Compressor.OFF: frozenset({Activity.OFF, Activity.IDLE, Activity.DEFROST, Activity.UNKNOWN}),
    Compressor.ON: frozenset({Activity.CO, Activity.DHW, Activity.TRANSITION, Activity.DEFROST,
                              Activity.UNKNOWN}),
    Compressor.UNKNOWN: frozenset({Activity.DEFROST, Activity.UNKNOWN}),
}


def encode_energy(energy: Mapping[str, Stats]) -> str:
    """Deterministic JSON: ``{series: [n, sum, min, max, last]}``, sorted, shortest-repr floats."""
    return json.dumps({k: [s.n, s.sum, s.min, s.max, s.last] for k, s in energy.items()},
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def segment_record(segment: ActivitySegment) -> SegmentRecord:
    """The persisted form of one complete hour-local segment under ``ACTIVITY_RULE_VERSION``."""
    if segment.energy is None:
        raise ValueError("a clipped segment piece cannot be persisted")
    record = (segment.start, segment.minutes, ACTIVITY_RULE_VERSION, segment.activity.value,
              segment.compressor.value, segment.defrost_fraction, encode_energy(segment.energy))
    decode_segment(record)  # never write what could not be read back
    return record


def _number(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActivityRecordInvalid(f"{what} is not a number")
    try:
        number = float(value)
    except OverflowError:
        raise ActivityRecordInvalid(f"{what} is not finite") from None
    if not math.isfinite(number):
        raise ActivityRecordInvalid(f"{what} is not finite")
    return number


def _decode_energy(text, minutes: int, where: str) -> dict[str, Stats]:
    try:
        document = json.loads(text) if isinstance(text, (str, bytes)) else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise ActivityRecordInvalid(f"{where}: energy is not a JSON object")
    energy: dict[str, Stats] = {}
    for key, value in document.items():
        if key not in ENERGY_SERIES:
            raise ActivityRecordInvalid(f"{where}: unknown energy series {key!r}")
        if not isinstance(value, list) or len(value) != 5:
            raise ActivityRecordInvalid(f"{where}: {key} is not [n, sum, min, max, last]")
        n = value[0]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= minutes:
            raise ActivityRecordInvalid(f"{where}: {key} has invalid n")
        v_sum, v_min, v_max, v_last = (_number(v, f"{where}: {key}") for v in value[1:])
        if not 0 <= v_min <= v_last <= v_max or v_sum < 0:  # canonical power is never negative
            raise ActivityRecordInvalid(f"{where}: {key} statistics are inconsistent")
        energy[key] = Stats(n, v_sum, v_min, v_max, v_last)
    for pair_in, pair_out in PAIRS.values():
        a, b = energy.get(pair_in), energy.get(pair_out)
        if (a is None) != (b is None) or (a is not None and a.n != b.n):
            raise ActivityRecordInvalid(f"{where}: {pair_in}/{pair_out} are not paired")
    for name, channels in (("co", POWER_CHANNELS[:2]), ("dhw", POWER_CHANNELS[2:])):
        pair = energy.get(PAIRS[name][0])
        if pair is not None and any(k not in energy or energy[k].n < pair.n for k in channels):
            raise ActivityRecordInvalid(f"{where}: paired {name} minutes exceed its channels")
    total = energy.get(PAIRS["total"][0])
    if total is not None and any(energy.get(PAIRS[p][0]) is None or energy[PAIRS[p][0]].n < total.n
                                 for p in ("co", "dhw")):
        raise ActivityRecordInvalid(f"{where}: paired total minutes exceed CO/DHW pairs")
    return energy


def decode_segment(record) -> ActivitySegment:
    """Validate one persisted row into its version-1 ``ActivitySegment``; fail closed."""
    try:
        start, minutes, version, activity, compressor, fraction, energy = record
    except (TypeError, ValueError):
        raise ActivityRecordInvalid("activity record does not have seven fields") from None
    where = f"activity segment at {start!r}"
    if version != ACTIVITY_RULE_VERSION:
        raise ActivityRecordInvalid(f"{where}: unknown activity rule version {version!r}")
    if (any(isinstance(v, bool) or not isinstance(v, int) for v in (start, minutes))
            or start < 0 or start % MINUTE or not 1 <= minutes <= HOUR // MINUTE
            or start % HOUR + minutes * MINUTE > HOUR):
        raise ActivityRecordInvalid(f"{where}: not whole minutes inside one UTC hour")
    try:
        activity, compressor = Activity(activity), Compressor(compressor)
    except ValueError:
        raise ActivityRecordInvalid(f"{where}: unknown activity or compressor state") from None
    if activity not in _ACTIVITY_FOR_COMPRESSOR[compressor]:
        raise ActivityRecordInvalid(f"{where}: {activity} cannot have compressor {compressor}")
    if fraction is not None:
        fraction = _number(fraction, f"{where}: defrost fraction")
        if not 0 <= fraction <= 1:
            raise ActivityRecordInvalid(f"{where}: defrost fraction outside [0, 1]")
    if (activity is Activity.DEFROST) != (fraction is not None and fraction > 0) \
            or (fraction is None and activity is not Activity.UNKNOWN):
        raise ActivityRecordInvalid(f"{where}: defrost fraction contradicts activity {activity}")
    return ActivitySegment(start, minutes, activity, compressor, fraction,
                           _decode_energy(energy, minutes, where))


def decode_segments(records: Iterable) -> list[ActivitySegment]:
    """Decode ascending, non-overlapping persisted rows; any doubt raises ``ActivityRecordInvalid``."""
    out: list[ActivitySegment] = []
    for record in records:
        segment = decode_segment(record)
        if out and segment.start < out[-1].end:
            raise ActivityRecordInvalid(f"activity segment at {segment.start} overlaps its predecessor")
        out.append(segment)
    return out
