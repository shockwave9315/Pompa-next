"""History engine: exact ``[from, to)`` buckets composed from ``sample_1m`` and ``rollup_1h``.

Read paths are fixed by the bucket, never by the data:

* ``1m``, ``5m``: ``sample_1m`` only.
* ``1h``, ``1d``, ``total``: ``rollup_1h`` for complete UTC hours below
  ``rolled_until``; ``sample_1m`` for partial edge hours and for hours at or
  above ``rolled_until``.

Every bucket is the time-ordered combine of per-UTC-hour partials, each the
fold of that hour's minutes inside the bucket (a complete rolled hour's
partial is its rollup row). Buckets never straddle an hour except ``1d`` and
``total``, whose boundaries are whole hours or the exact request edges. The
raw-only and mixed paths therefore perform identical floating-point work.

Missing minutes stay missing: nothing is filled, interpolated or extrapolated.
All reads happen in one storage session, i.e. one consistent snapshot.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence

from .aggregation import (
    PAIRS, RECORDED, Stats, combine_maps, cop, coverage_percent, energy_kwh, fold_minutes, is_power,
    minute_columns,
)
from .catalog import METRICS, METRICS_BY_KEY, RECORDED_KEYS
from .minute import iso_utc
from .storage import Storage
from .timegrid import (
    BUCKETS, HOUR, LOCAL_TZ_NAME, MAX_BUCKETS, Unrepresentable, bucket_edges, ceil_hour, choose_auto,
    expected_minutes, floor_hour, raw_floor,
)

COP_SERIES: dict[str, str] = {f"cop_{name}": name for name in PAIRS}
COP_LABELS = {"cop_co": "COP CO", "cop_dhw": "COP CWU", "cop_total": "COP łącznie"}
HISTORY_SERIES: tuple[str, ...] = RECORDED_KEYS + tuple(COP_SERIES)
HOURLY_BUCKETS = ("1h", "1d", "total")
COP_FIELDS: tuple[str, ...] = ("cop", "paired_minutes", "input_kwh", "output_kwh")


def history_fields(name: str) -> tuple[str, ...]:
    """The value fields ``/api/v1/history`` returns for one series.

    Both the history response and the ``/api/v1/metrics`` catalog are built
    from this one function, so the published metadata cannot drift from the
    data.
    """
    if name in COP_SERIES:
        return COP_FIELDS
    metric = METRICS_BY_KEY[name]
    first = "avg" if metric.kind == "mean" else "last"
    return (first, "min", "max", "minutes") + (("kwh",) if is_power(name) else ())


def catalog() -> dict:
    """The frontend-safe metric and COP catalog behind ``/api/v1/metrics``.

    Derived entirely from the metric catalog, the history field lists and the
    time grid constants: no second hand-maintained list of keys, labels, units,
    groups, kinds or buckets exists.
    """
    return {
        "timezone": LOCAL_TZ_NAME,
        "history": {"buckets": list(BUCKETS), "max_buckets": MAX_BUCKETS},
        "metrics": [
            {
                "key": m.key,
                "label": m.label,
                "unit": m.unit,
                "group": m.group,
                "kind": m.kind,
                "history_fields": list(history_fields(m.key)),
                "energy": is_power(m.key),
            }
            # Every canonical metric, in catalog order: exactly what /live reports.
            # All of them are recorded today, so all of them have history fields.
            for m in METRICS
        ],
        "cop": [
            {
                "key": name,
                "label": COP_LABELS[name],
                "unit": None,
                "kind": "cop",
                "history_fields": list(history_fields(name)),
            }
            for name in COP_SERIES
        ],
    }


def _needed(series: Sequence[str]) -> tuple[str, ...]:
    needed = {RECORDED}
    for name in series:
        if name in COP_SERIES:
            needed.update(PAIRS[COP_SERIES[name]])
        else:
            needed.add(name)
    return tuple(sorted(needed))


def _hour_pieces(a: int, b: int):
    h = floor_hour(a)
    while h < b:
        yield max(a, h), min(b, h + HOUR)
        h += HOUR


def query(storage: Storage, start: int, end: int, bucket: str, series: Sequence[str],
          now: float, retention_days: int) -> dict:
    """History for minute-aligned ``start < end``. Raises ``Unrepresentable`` (422)."""
    unknown = [s for s in series if s not in HISTORY_SERIES]
    if unknown:
        raise ValueError(f"unknown series: {', '.join(unknown)}")
    needed = _needed(series)

    with storage.session() as s:
        rolled_until = s.rolled_until()
        floor = raw_floor(now, rolled_until, retention_days)
        resolved = choose_auto(start, end, floor) if bucket == "auto" else bucket
        edges = bucket_edges(start, end, resolved)

        # Complete rolled hours [roll_lo, roll_hi) come from rollup_1h, everything else from raw.
        roll_lo = roll_hi = ceil_hour(start)
        if resolved in HOURLY_BUCKETS and rolled_until is not None:
            roll_hi = max(roll_lo, min(floor_hour(end), rolled_until))
        raw_spans = [(start, end)] if roll_lo == roll_hi else [
            (a, b) for a, b in ((start, roll_lo), (roll_hi, end)) if a < b]
        for a, _ in raw_spans:
            if floor is not None and a < floor:
                raise Unrepresentable(
                    f"bucket={resolved} needs raw minutes from {iso_utc(a)}, but raw minutes are retained"
                    f" only from {iso_utc(floor)}; the range is not rounded")

        columns = minute_columns(needed)
        raw = [r for a, b in raw_spans for r in s.read_minutes(a, b, columns)]
        rolled: dict[int, dict[str, Stats]] = {}
        for h, name, n, v_sum, v_min, v_max, v_last in s.read_rollup(roll_lo, roll_hi, needed):
            rolled.setdefault(h, {})[name] = Stats(n, v_sum, v_min, v_max, v_last)

    raw_ts = [ts for ts, _ in raw]
    per_bucket: list[dict[str, Stats]] = []
    for a, b in edges:
        acc: dict[str, Stats] = {}
        for pa, pb in _hour_pieces(a, b):
            if roll_lo <= pa and pb <= roll_hi:
                part = rolled.get(pa, {})
            else:
                part = fold_minutes(raw[bisect_left(raw_ts, pa):bisect_left(raw_ts, pb)], needed)
            acc = combine_maps(acc, part)
        per_bucket.append(acc)

    return _response(start, end, bucket, resolved, edges, per_bucket, series, now)


_SERIES_VALUES = {  # one bucket-aligned array per history field of an ordinary metric
    "avg": lambda stats: [None if st is None else st.avg for st in stats],
    "last": lambda stats: [None if st is None else st.last for st in stats],
    "min": lambda stats: [None if st is None else st.min for st in stats],
    "max": lambda stats: [None if st is None else st.max for st in stats],
    "minutes": lambda stats: [0 if st is None else st.n for st in stats],
    "kwh": lambda stats: [energy_kwh(st) for st in stats],
}


def _response(start, end, requested, resolved, edges, per_bucket, series, now) -> dict:
    buckets = []
    for (a, b), acc in zip(edges, per_bucket):
        expected = expected_minutes(a, b, now)
        recorded = acc[RECORDED].n if RECORDED in acc else 0
        buckets.append({
            "start": iso_utc(a),
            "end": iso_utc(b),
            "expected_minutes": expected,
            "recorded_minutes": recorded,
            "coverage_percent": coverage_percent(recorded, expected),
        })

    out = {}
    for name in series:
        fields = history_fields(name)
        if name in COP_SERIES:
            pair_in, pair_out = PAIRS[COP_SERIES[name]]
            facts = [cop(acc.get(pair_in), acc.get(pair_out)) for acc in per_bucket]
            entry = {"label": COP_LABELS[name], "unit": None, "kind": "cop"}
            entry.update({f: [fact[f] for fact in facts] for f in fields})
        else:
            metric = METRICS_BY_KEY[name]
            stats = [acc.get(name) for acc in per_bucket]
            entry = {"label": metric.label, "unit": metric.unit, "kind": metric.kind}
            entry.update({f: _SERIES_VALUES[f](stats) for f in fields})
        out[name] = entry

    return {
        "from": iso_utc(start),
        "to": iso_utc(end),
        "bucket": resolved,
        "requested_bucket": requested,
        "buckets": buckets,
        "series": out,
    }
