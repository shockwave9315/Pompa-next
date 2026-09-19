"""Aggregation algebra: ``Stats``, derived minute series, energy, COP and coverage.

Pure functions only. The same fold builds hourly rollups and every query
bucket; there is no separate mathematics per bucket size.

Canonical association order: minutes are folded per UTC hour first and hour
partials are then combined in time order. ``combine`` is algebraically
associative, but floating-point addition is not; folding in this one order on
every path makes raw and rollup reads bit-identical rather than merely close.
"""

from __future__ import annotations

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
assert all(k in RECORDED_KEYS for k in (_CO_IN, _CO_OUT, _DHW_IN, _DHW_OUT))


def derive(values: Mapping[str, float | None]) -> dict[str, float | None]:
    """Expand one recorded minute into every series value (``None`` = absent).

    A paired series exists only when all of its power channels are known in the
    same minute, so paired input and output always share contributing minutes.
    """
    out: dict[str, float | None] = {k: values[k] for k in RECORDED_KEYS}
    out[RECORDED] = 1.0
    co_in, co_out, dhw_in, dhw_out = values[_CO_IN], values[_CO_OUT], values[_DHW_IN], values[_DHW_OUT]
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
