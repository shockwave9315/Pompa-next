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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from .aggregation import PAIRS, POWER_CHANNELS, Stats, combine_maps, fold_minutes
from .minute import MINUTE
from .timegrid import floor_hour

# The interpretation below is historical meaning once 4C-B persists it. Any change to what a
# minute classifies as, or to what a segment carries, needs a new version, never an edit.
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


def _boundary(tl: Timeline, neighbour: ActivitySegment | Gap | None, state_of, edge: int,
              after: bool) -> Boundary:
    if neighbour is None:
        return Boundary.OPEN if after and edge >= tl.closed_until else Boundary.OUTSIDE_EVIDENCE
    if isinstance(neighbour, Gap):
        return Boundary.GAP
    return Boundary.UNKNOWN if state_of(neighbour).value == "unknown" else Boundary.OBSERVED


def _spans(tl: Timeline, state_of, wanted) -> list[ObservedSpan]:
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
                                _boundary(tl, before, state_of, segments[0].start, False),
                                _boundary(tl, after, state_of, segments[-1].end, True),
                                tuple(segments)))
        i = j + 1
    return out


def activity_events(tl: Timeline) -> list[ActivityEvent]:
    """Every maximal same-activity span, including off, idle and unknown."""
    return _spans(tl, lambda s: s.activity, frozenset(Activity))


def defrosts(tl: Timeline) -> list[ActivityEvent]:
    """Individual defrosts: contiguous defrost activity; separate defrosts stay separate."""
    return _spans(tl, lambda s: s.activity, frozenset({Activity.DEFROST}))


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

    A zero-valued minute proves the compressor off for that whole minute, so an
    observed start happened within the run's first minute and an observed stop
    within its last one; each is counted in the range holding that minute.
    Complete runs and exact off intervals are attributed to the range holding
    their first minute.
    """

    rule_version: int
    start: int
    end: int
    closed_minutes: int
    recorded_minutes: int
    gap_minutes: int
    activity_minutes: Mapping[str, int]
    compressor_minutes: Mapping[str, int]
    observed_starts: int
    observed_stops: int
    compressor_runs: int
    complete_run_minutes: tuple[int, ...]
    exact_off_interval_minutes: tuple[int, ...]
    defrosts: int
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
        rule_version=ACTIVITY_RULE_VERSION,
        start=start,
        end=end,
        closed_minutes=closed,
        recorded_minutes=closed - gaps,
        gap_minutes=gaps,
        activity_minutes=activity,
        compressor_minutes=compressor,
        observed_starts=sum(1 for r in runs if r.start_observed and within(r.start)),
        observed_stops=sum(1 for r in runs if r.end_observed and within(r.end - MINUTE)),
        compressor_runs=len(project(runs, start, end)),
        complete_run_minutes=tuple(r.minutes for r in runs
                                   if r.start_observed and r.end_observed and within(r.start)),
        exact_off_interval_minutes=tuple(i.minutes for i in compressor_off_intervals(tl)
                                         if i.start_observed and i.end_observed and within(i.start)),
        defrosts=len(defrost_pieces),
        observed_defrost_seconds=sum(p.observed_defrost_seconds for p in defrost_pieces),
    )
