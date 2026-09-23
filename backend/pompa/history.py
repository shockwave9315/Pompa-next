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

A span that must be read from ``sample_1m`` is answered only when every hour
it overlaps can still be read truthfully. ``Session.first_purged_hour`` decides
that from the database alone; the wall clock and ``RETENTION_1M_DAYS`` are not
consulted, because they describe what purge may delete next, not what it
already deleted. A 422 therefore means the minutes provably existed and are
gone — never merely that the range is old, and never that it was never
recorded, which is an ordinary empty answer.

Missing minutes stay missing: nothing is filled, interpolated or extrapolated.
All reads happen in one storage session, i.e. one consistent snapshot.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
import re

from .aggregation import (
    MINUTES_PER_KWH_W, PAIRS, RECORDED, OptionalHistoryInconsistent, OptionalStats, Stats, combine_maps,
    combine_optional_maps, cop,
    coverage_percent, energy_kwh, fold_minutes, fold_optional_minutes, is_power, minute_columns,
)
from .catalog import METRICS, METRICS_BY_KEY, RECORDED_KEYS
from .minute import iso_utc
from .optional_policy import snapshot_timeline
from .storage import SeriesRow, Storage
from .timegrid import (
    BUCKETS, HOUR, LOCAL_TZ_NAME, MAX_BUCKETS, MINUTE_BUCKETS, Unrepresentable, auto_bucket,
    bucket_edges, ceil_hour, expected_minutes, floor_hour,
)

COP_SERIES: dict[str, str] = {f"cop_{name}": name for name in PAIRS}
COP_LABELS = {"cop_co": "COP CO", "cop_dhw": "COP CWU", "cop_total": "COP łącznie"}
HISTORY_SERIES: tuple[str, ...] = RECORDED_KEYS + tuple(COP_SERIES)
HOURLY_BUCKETS = ("1h", "1d", "total")
COP_FIELDS: tuple[str, ...] = ("cop", "paired_minutes", "input_kwh", "output_kwh")
OPTIONAL_SELECTOR = re.compile(r"^optional:([A-Z][A-Z0-9]*)@([1-9][0-9]*)$")


class HistoryRequestError(ValueError):
    """A requested historical selector is malformed or has no persisted meaning."""


def optional_selector(row: SeriesRow) -> str:
    return f"optional:{row.identity}@{row.profile_version}"


def optional_series_metadata(row: SeriesRow) -> dict:
    return {"selector": optional_selector(row), "series_id": row.id, "identity": row.identity,
            "topic": row.expected_topic, "profile_version": row.profile_version,
            "label": row.label, "unit": row.unit, "kind": row.kind,
            "semantic_type": row.semantic_type, "energy": row.energy}


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
        if OPTIONAL_SELECTOR.fullmatch(name):
            continue
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
          now: float) -> dict:
    """History for minute-aligned ``start < end``. Raises ``Unrepresentable`` (422)."""
    unknown = [s for s in series if s not in HISTORY_SERIES and not OPTIONAL_SELECTOR.fullmatch(s)]
    if unknown:
        raise HistoryRequestError(f"unknown series: {', '.join(unknown)}")
    needed = _needed(series)

    with storage.session() as s:
        optional_rows: dict[str, SeriesRow] = {}
        for selector in series:
            match = OPTIONAL_SELECTOR.fullmatch(selector)
            if match:
                found = s.find_optional_series(match[1], int(match[2]))
                if not found:
                    raise HistoryRequestError(f"unknown persisted optional series: {selector}")
                if len(found) != 1:
                    raise OptionalHistoryInconsistent(f"ambiguous persisted optional series: {selector}")
                if found[0].kind not in ("mean", "last"):
                    raise OptionalHistoryInconsistent(f"invalid persisted optional kind: {selector}")
                optional_rows[selector] = found[0]
        optional_ids = [row.id for row in optional_rows.values()]
        optional_by_id = {row.id: row for row in optional_rows.values()}
        rolled_until = s.rolled_until()
        if bucket == "auto":
            resolved = auto_bucket(start, end)
            if resolved in MINUTE_BUCKETS and s.first_purged_hour(start, end) is not None:
                resolved = "1h"  # the raw minutes are gone; whole rolled hours can still answer
        else:
            resolved = bucket
        edges = bucket_edges(start, end, resolved)

        # Complete rolled hours [roll_lo, roll_hi) come from rollup_1h, everything else from raw.
        roll_lo = roll_hi = ceil_hour(start)
        if resolved in HOURLY_BUCKETS and rolled_until is not None:
            roll_hi = max(roll_lo, min(floor_hour(end), rolled_until))
        raw_spans = [(start, end)] if roll_lo == roll_hi else [
            (a, b) for a, b in ((start, roll_lo), (roll_hi, end)) if a < b]
        for a, b in raw_spans:
            purged = s.first_purged_hour(a, b)
            if purged is not None:
                raise Unrepresentable(
                    f"bucket={resolved} needs the raw minutes of {iso_utc(a)}–{iso_utc(b)}, but the raw"
                    f" evidence of hour {iso_utc(purged)} was purged; the range is not rounded")

        columns = minute_columns(needed)
        raw = [r for a, b in raw_spans for r in s.read_minutes(a, b, columns)]
        raw_ts = [ts for ts, _ in raw]
        optional_raw = ([item for a, b in raw_spans for item in s.read_optional_minutes(a, b)]
                        if optional_ids else [])
        optional_raw_ts = [ts for ts, _ in optional_raw]
        timeline = (snapshot_timeline(s, s.read_policy_head(), raw_ts) if optional_ids else {})
        rolled: dict[int, dict[str, Stats]] = {}
        for h, name, n, v_sum, v_min, v_max, v_last in s.read_rollup(roll_lo, roll_hi, needed):
            rolled.setdefault(h, {})[name] = Stats(n, v_sum, v_min, v_max, v_last)
        optional_rolled: dict[int, dict[int, OptionalStats]] = {}
        if optional_ids:
            for h, sid, selected, known, v_sum, v_min, v_max, v_last in s.read_optional_rollup(
                    roll_lo, roll_hi, optional_ids):
                if selected <= 0 or (optional_by_id[sid].kind == "last" and v_sum is not None):
                    raise OptionalHistoryInconsistent(
                        f"optional rollup {h}/{sid} has invalid selected count or last-series sum")
                optional_rolled.setdefault(h, {})[sid] = OptionalStats(
                    selected, known, v_sum, v_min, v_max, v_last)

    per_bucket: list[dict[str, Stats]] = []
    optional_per_bucket: list[dict[int, OptionalStats]] = []
    for a, b in edges:
        acc: dict[str, Stats] = {}
        optional_acc: dict[int, OptionalStats] = {}
        for pa, pb in _hour_pieces(a, b):
            if roll_lo <= pa and pb <= roll_hi:
                part = rolled.get(pa, {})
                optional_part = optional_rolled.get(pa, {}) if optional_ids else {}
            else:
                minute_part = raw[bisect_left(raw_ts, pa):bisect_left(raw_ts, pb)]
                part = fold_minutes(minute_part, needed)
                optional_part = (fold_optional_minutes(
                    [ts for ts, _ in minute_part], timeline,
                    dict(optional_raw[bisect_left(optional_raw_ts, pa):bisect_left(optional_raw_ts, pb)]),
                    set(optional_ids))
                    if optional_ids else {})
            acc = combine_maps(acc, part)
            if optional_ids:
                optional_acc = combine_optional_maps(optional_acc, optional_part)
        per_bucket.append(acc)
        optional_per_bucket.append(optional_acc)

    return _response(start, end, bucket, resolved, edges, per_bucket, series, now,
                     optional_per_bucket, optional_rows)


_SERIES_VALUES = {  # one bucket-aligned array per history field of an ordinary metric
    "avg": lambda stats: [None if st is None else st.avg for st in stats],
    "last": lambda stats: [None if st is None else st.last for st in stats],
    "min": lambda stats: [None if st is None else st.min for st in stats],
    "max": lambda stats: [None if st is None else st.max for st in stats],
    "minutes": lambda stats: [0 if st is None else st.n for st in stats],
    "kwh": lambda stats: [energy_kwh(st) for st in stats],
}


def _optional_sum_required(stats: OptionalStats | None, selector: str, a: int, b: int
                           ) -> float | None:
    if stats is None or stats.known_minutes == 0:
        return None
    if stats.v_sum is None:
        raise Unrepresentable(
            f"{selector} aggregate sum is not representable for bucket {iso_utc(a)}–{iso_utc(b)}")
    return stats.v_sum


def _response(start, end, requested, resolved, edges, per_bucket, series, now,
              optional_per_bucket=None, optional_rows=None) -> dict:
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
        if optional_rows and name in optional_rows:
            row = optional_rows[name]
            stats = [acc.get(row.id) for acc in optional_per_bucket]
            first = "avg" if row.kind == "mean" else "last"
            entry = optional_series_metadata(row)
            entry.pop("selector")
            if row.kind == "mean":
                primary = [None if (total := _optional_sum_required(st, name, a, b)) is None
                           else total / st.known_minutes
                           for st, (a, b) in zip(stats, edges)]
            else:
                primary = [None if st is None else st.v_last for st in stats]
            entry.update({first: primary,
                          "min": [None if st is None else st.v_min for st in stats],
                          "max": [None if st is None else st.v_max for st in stats],
                          "selected_minutes": [0 if st is None else st.selected_minutes for st in stats],
                          "known_minutes": [0 if st is None else st.known_minutes for st in stats]})
            if row.energy:
                entry["kwh"] = [None if (total := _optional_sum_required(st, name, a, b)) is None
                                else total / MINUTES_PER_KWH_W
                                for st, (a, b) in zip(stats, edges)]
        elif name in COP_SERIES:
            fields = history_fields(name)
            pair_in, pair_out = PAIRS[COP_SERIES[name]]
            facts = [cop(acc.get(pair_in), acc.get(pair_out)) for acc in per_bucket]
            entry = {"label": COP_LABELS[name], "unit": None, "kind": "cop"}
            entry.update({f: [fact[f] for fact in facts] for f in fields})
        else:
            fields = history_fields(name)
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
