"""Aggregation algebra: ``Stats``, derived minute series, energy, COP and coverage.

Pure functions only. The same fold builds hourly rollups and every query
bucket; there is no separate mathematics per bucket size.

Canonical association order: minutes are folded per UTC hour first and hour
partials are then combined in time order. ``combine`` is algebraically
associative, but floating-point addition is not; folding in this one order on
every path makes raw and rollup reads bit-identical rather than merely close.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .catalog import METRICS_BY_KEY, RECORDED_KEYS

MINUTES_PER_KWH_W = 60000  # Σ minute-average W / 60000 = kWh


@dataclass(frozen=True, slots=True)
class Stats:
    """Summary of at least one known value; "no values" is ``None``, never a Stats."""

    n: int
    sum: float
    min: float
    max: float
    last: float

    @staticmethod
    def of(v: float) -> Stats:
        return Stats(1, v, v, v, v)

    @property
    def avg(self) -> float:
        return self.sum / self.n


def combine(a: Stats | None, b: Stats | None) -> Stats | None:
    """``a`` then ``b`` in time order. Associative, not commutative (``last``)."""
    if a is None:
        return b
    if b is None:
        return a
    return Stats(a.n + b.n, a.sum + b.sum, min(a.min, b.min), max(a.max, b.max), b.last)


def fold(values: Iterable[float | None]) -> Stats | None:
    """Chronologically ordered values; ``None`` contributes nothing."""
    acc: Stats | None = None
    for v in values:
        if v is not None:
            acc = combine(acc, Stats.of(v))
    return acc


# ------------------------------------------------------------------ derived series

RECORDED = "recorded"
PAIRS: dict[str, tuple[str, str]] = {  # COP name -> (paired input series, paired output series)
    "co": ("pair_co_in", "pair_co_out"),
    "dhw": ("pair_dhw_in", "pair_dhw_out"),
    "total": ("pair_total_in", "pair_total_out"),
}
DERIVED_KEYS: tuple[str, ...] = (RECORDED,) + tuple(s for pair in PAIRS.values() for s in pair)
# Every series stored in rollup_1h: catalog metrics plus derived series.
SERIES: tuple[str, ...] = RECORDED_KEYS + DERIVED_KEYS

_CO_IN, _CO_OUT = "co_power_consumption", "co_power_production"
_DHW_IN, _DHW_OUT = "dhw_power_consumption", "dhw_power_production"
POWER_CHANNELS: tuple[str, ...] = (_CO_IN, _CO_OUT, _DHW_IN, _DHW_OUT)
assert all(k in RECORDED_KEYS for k in POWER_CHANNELS)


def minute_columns(series: Sequence[str]) -> tuple[str, ...]:
    """The ``sample_1m`` columns a fold of ``series`` needs: its metrics plus paired power."""
    columns = {s for s in series if s in RECORDED_KEYS}
    if any(s.startswith("pair_") for s in series):
        columns.update(POWER_CHANNELS)
    return tuple(k for k in RECORDED_KEYS if k in columns)


def derive(values: Mapping[str, float | None]) -> dict[str, float | None]:
    """Expand one recorded minute into every series value (``None`` = absent).

    A paired series exists only when all of its power channels are known in the
    same minute, so paired input and output always share contributing minutes.
    A metric the caller did not read is absent, exactly like an unknown one, so
    only the series a query actually folds may be taken from the result.
    """
    out: dict[str, float | None] = {k: values.get(k) for k in RECORDED_KEYS}
    out[RECORDED] = 1.0
    co_in, co_out = values.get(_CO_IN), values.get(_CO_OUT)
    dhw_in, dhw_out = values.get(_DHW_IN), values.get(_DHW_OUT)
    co = co_in is not None and co_out is not None
    dhw = dhw_in is not None and dhw_out is not None
    out["pair_co_in"], out["pair_co_out"] = (co_in, co_out) if co else (None, None)
    out["pair_dhw_in"], out["pair_dhw_out"] = (dhw_in, dhw_out) if dhw else (None, None)
    if co and dhw:
        out["pair_total_in"], out["pair_total_out"] = co_in + dhw_in, co_out + dhw_out
    else:
        out["pair_total_in"] = out["pair_total_out"] = None
    return out


def fold_minutes(rows: Iterable[tuple[int, Mapping[str, float | None]]],
                 series: Sequence[str] = SERIES) -> dict[str, Stats]:
    """Fold stored minutes ``(ts, values)`` in strictly ascending ``ts``.

    Returns only series with at least one known value. Used unchanged for
    rollup rows and for raw query pieces.
    """
    acc: dict[str, Stats] = {}
    previous = None
    for ts, values in rows:
        if previous is not None and ts <= previous:
            raise ValueError(f"minutes must be folded in ascending order: {ts} after {previous}")
        previous = ts
        derived = derive(values)
        for s in series:
            v = derived[s]
            if v is not None:
                acc[s] = combine(acc.get(s), Stats.of(v))
    return acc


def combine_maps(a: Mapping[str, Stats], b: Mapping[str, Stats]) -> dict[str, Stats]:
    """Per-series ``combine(a, b)``; ``a`` precedes ``b`` in time."""
    out = dict(a)
    for s, stats in b.items():
        out[s] = combine(out.get(s), stats)
    return out


class OptionalHistoryInconsistent(RuntimeError):
    """Stored optional raw/rollup facts cannot describe one truthful history."""


@dataclass(frozen=True, slots=True)
class OptionalStats:
    selected_minutes: int
    known_minutes: int
    values: Stats | None

    def __post_init__(self) -> None:
        if not 0 <= self.known_minutes <= self.selected_minutes:
            raise OptionalHistoryInconsistent("optional selected/known minute counts disagree")
        if (self.values is None) != (self.known_minutes == 0):
            raise OptionalHistoryInconsistent("optional known count and value statistics disagree")
        if self.values is not None:
            if self.values.n != self.known_minutes or not all(math.isfinite(v) for v in (
                    self.values.sum, self.values.min, self.values.max, self.values.last)):
                raise OptionalHistoryInconsistent("optional value statistics are inconsistent or non-finite")


def combine_optional(a: OptionalStats | None, b: OptionalStats | None) -> OptionalStats | None:
    """Combine chronological optional facts using the existing Stats association."""
    if a is None:
        return b
    if b is None:
        return a
    return OptionalStats(a.selected_minutes + b.selected_minutes,
                         a.known_minutes + b.known_minutes, combine(a.values, b.values))


def combine_optional_maps(a: Mapping[int, OptionalStats], b: Mapping[int, OptionalStats]
                          ) -> dict[int, OptionalStats]:
    out = dict(a)
    for sid, stats in b.items():
        out[sid] = combine_optional(out.get(sid), stats)
    return out


def fold_optional_minutes(minutes: Sequence[int], selected: Mapping[int, Sequence],
                          raw: Mapping[int, Mapping[str, object]]) -> dict[int, OptionalStats]:
    """Fold ordered canonical minutes, persisted policy members and optional JSON facts.

    Validates every raw key/value, including keys outside a caller's requested series.
    """
    result: dict[int, OptionalStats] = {}
    minute_set = set(minutes)
    if any(ts not in minute_set for ts in raw):
        raise OptionalHistoryInconsistent("optional raw exists without a canonical minute")
    previous = None
    for ts in minutes:
        if previous is not None and ts <= previous:
            raise ValueError("optional minutes must be folded in ascending order")
        previous = ts
        members = {member.id for member in selected[ts]}
        values: dict[int, float] = {}
        document = raw.get(ts, {})
        if not isinstance(document, Mapping):
            raise OptionalHistoryInconsistent(f"optional raw is not a JSON object at minute {ts}")
        for key, value in document.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or int(key) <= 0:
                raise OptionalHistoryInconsistent(f"invalid optional raw series key at minute {ts}")
            sid = int(key)
            if str(sid) != key:
                raise OptionalHistoryInconsistent(f"noncanonical optional raw series key at minute {ts}")
            if sid not in members:
                raise OptionalHistoryInconsistent(f"unselected optional raw series {sid} at minute {ts}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise OptionalHistoryInconsistent(f"non-finite or nonnumeric optional raw value at minute {ts}")
            values[sid] = float(value)
        for sid in members:
            value = values.get(sid)
            part = OptionalStats(1, int(value is not None), None if value is None else Stats.of(value))
            result[sid] = combine_optional(result.get(sid), part)
    return result


# ------------------------------------------------------------------ energy, COP, coverage

def energy_kwh(stats: Stats | None) -> float | None:
    """Energy of the known minutes only; never extrapolated over missing ones."""
    return None if stats is None else stats.sum / MINUTES_PER_KWH_W


def is_power(key: str) -> bool:
    m = METRICS_BY_KEY[key]
    return m.group == "power" and m.unit == "W"


def cop(pair_in: Stats | None, pair_out: Stats | None) -> dict:
    """Period COP = Σ paired out / Σ paired in; never an average of minute COPs."""
    if (pair_in is None) != (pair_out is None) or (pair_in and pair_in.n != pair_out.n):
        raise ValueError("paired series must share contributing minutes")
    if pair_in is None:
        return {"cop": None, "paired_minutes": 0, "input_kwh": None, "output_kwh": None}
    return {
        "cop": pair_out.sum / pair_in.sum if pair_in.sum > 0 else None,
        "paired_minutes": pair_in.n,
        "input_kwh": energy_kwh(pair_in),
        "output_kwh": energy_kwh(pair_out),
    }


def coverage_percent(recorded_minutes: int, expected_minutes: int) -> float | None:
    return None if expected_minutes == 0 else round(100.0 * recorded_minutes / expected_minutes, 1)
