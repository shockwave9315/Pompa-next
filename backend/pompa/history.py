"""1-minute history composition from stored minutes (Stage 1 read path).

Pure function of the stored rows: no row → ``recorded_minutes = 0``; a stored
``NULL`` → ``recorded_minutes = 1`` with a null series value. Nothing is filled.
"""

from __future__ import annotations

from collections.abc import Sequence

from .catalog import METRICS_BY_KEY
from .minute import MINUTE, iso_utc


def build_1m(start: int, end: int, keys: Sequence[str],
             rows: Sequence[tuple[int, dict[str, float | None]]], now: float) -> dict:
    """``start``/``end`` are minute-aligned; ``rows`` are ``(ts, values)`` in ``[start, end)``."""
    by_ts = dict(rows)
    starts = range(start, end, MINUTE)

    buckets = []
    for ts in starts:
        recorded = 1 if ts in by_ts else 0
        expected = 1 if ts + MINUTE <= now else 0  # elapsed minutes only
        buckets.append({
            "start": iso_utc(ts),
            "end": iso_utc(ts + MINUTE),
            "expected_minutes": expected,
            "recorded_minutes": recorded,
            "coverage_percent": round(100.0 * recorded / expected, 1) if expected else None,
        })

    series = {}
    for key in keys:
        metric = METRICS_BY_KEY[key]
        values = [by_ts[ts][key] if ts in by_ts else None for ts in starts]
        entry = {"label": metric.label, "unit": metric.unit, "kind": metric.kind}
        # A 1-minute bucket holds at most one value, so it is its own avg/last, min and max.
        entry["avg" if metric.kind == "mean" else "last"] = values
        entry["min"] = values
        entry["max"] = values
        entry["minutes"] = [0 if v is None else 1 for v in values]
        series[key] = entry

    return {
        "from": iso_utc(start),
        "to": iso_utc(end),
        "bucket": "1m",
        "buckets": buckets,
        "series": series,
    }
