"""Time alignment: UTC hours, Europe/Warsaw calendar days, buckets, prospective purge cutoff.

All instants are UTC Unix seconds. Sub-day buckets align to UTC; ``1d`` is a
real local calendar day converted to UTC through the timezone database, so DST
days are 23 or 25 hours long without any special-casing here.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .minute import MINUTE

HOUR = 3600
DAY = 86400
LOCAL_TZ_NAME = "Europe/Warsaw"
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

BUCKETS = ("auto", "1m", "5m", "1h", "1d", "total")
MINUTE_BUCKETS = ("1m", "5m")  # served from sample_1m alone
MAX_BUCKETS = 3000
_STEP = {"1m": MINUTE, "5m": 5 * MINUTE, "1h": HOUR}
AUTO_LIMITS = ((36 * HOUR, "1m"), (10 * DAY, "5m"), (120 * DAY, "1h"))
RAW_REPROCESS_MARGIN = 2 * HOUR  # purge never touches the two hours below rolled_until


class Unrepresentable(ValueError):
    """A well-formed request that history cannot answer exactly (HTTP 422)."""


def floor_hour(t: float) -> int:
    return math.floor(t / HOUR) * HOUR


def ceil_hour(t: float) -> int:
    return math.ceil(t / HOUR) * HOUR


def local_midnight(d: date) -> int:
    """UTC instant of local midnight starting calendar day ``d``."""
    return int(datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ).timestamp())


def local_date(t: float) -> date:
    return datetime.fromtimestamp(t, LOCAL_TZ).date()


def validate_local_days(first_year: int = 1970, last_year: int = 2100) -> None:
    """Startup check: every local day boundary is a whole UTC hour.

    Daily buckets are composed of whole ``rollup_1h`` hours only under this
    assumption, so a timezone database violating it must stop the process.
    """
    d, end = date(first_year, 1, 1), date(last_year + 1, 1, 1)
    while d < end:
        ts = local_midnight(d)
        if ts % HOUR:
            raise RuntimeError(f"{LOCAL_TZ_NAME} midnight of {d} is not a whole UTC hour ({ts})")
        d += timedelta(days=1)


def purge_cutoff(now: float, rolled_until: int | None, retention_days: int) -> int | None:
    """Instant below which purge may currently delete raw minutes, or ``None`` (none at all).

    This is prospective policy, not history. It follows the wall clock and the
    configured retention, so it moves backwards when either of them does, and
    it therefore never answers whether a minute has *already* been deleted —
    ``Session.first_purged_hour`` answers that from the database. Without any
    rollup nothing can be purged; ``retention_days = 0`` disables purge.
    """
    if retention_days == 0 or rolled_until is None:
        return None
    return min(floor_hour(now - retention_days * DAY), rolled_until - RAW_REPROCESS_MARGIN)


def auto_bucket(start: int, end: int) -> str:
    """``bucket=auto`` from range length alone.

    ``history`` promotes the raw-only results to ``1h`` when the range needs
    minutes that were purged; length is the only other input.
    """
    return next((b for limit, b in AUTO_LIMITS if end - start <= limit), "1d")


def bucket_edges(start: int, end: int, bucket: str, limit: int = MAX_BUCKETS) -> list[tuple[int, int]]:
    """Contiguous ``[a, b)`` buckets exactly covering ``[start, end)``; edge buckets are clipped.

    Raises ``Unrepresentable`` rather than truncating when more than ``limit``
    buckets would be needed.
    """
    if bucket == "total":
        return [(start, end)]
    if bucket in _STEP:
        step = _STEP[bucket]
        first = start - start % step
        count = -(-(end - first) // step)
        if count > limit:
            raise Unrepresentable(f"bucket={bucket} over this range needs {count} buckets; the limit is {limit}")
        return [(max(a, start), min(a + step, end)) for a in range(first, end, step)]
    if bucket == "1d":
        edges = []
        d = local_date(start)
        a = local_midnight(d)
        while a < end:
            if len(edges) == limit:
                raise Unrepresentable(f"bucket=1d over this range needs more than {limit} buckets")
            d += timedelta(days=1)
            b = local_midnight(d)
            edges.append((max(a, start), min(b, end)))
            a = b
        return edges
    raise ValueError(f"unknown bucket {bucket!r}")


def expected_minutes(a: int, b: int, now: float) -> int:
    """Elapsed minutes of ``[a, b)``: minutes ``m`` with ``m + 60 <= now``."""
    elapsed_end = min(b, math.floor(now / MINUTE) * MINUTE)
    return max(0, elapsed_end - a) // MINUTE
