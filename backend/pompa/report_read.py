"""Stage 4D report extraction in one caller-independent storage snapshot.

The API resolves the calendar and samples the recorder frontier before entering here.
Only extraction and its per-hour consistency proof happen inside the session; the
accepted report domain composes already-loaded facts after it closes.
"""

from . import activity_history, history, report
from .activity import Timeline
from .aggregation import PAIRS, POWER_CHANNELS, RECORDED
from .minute import MINUTE, iso_utc
from .storage import Storage
from .timegrid import bucket_edges, floor_hour

REQUIRED_SERIES = (RECORDED, *POWER_CHANNELS, *(key for pair in PAIRS.values() for key in pair),
                   "outside_temp", "compressor_freq")


class ReportHistoryInconsistent(report.ReportInvariantError):
    """Canonical and activity recorded-minute counts disagree in one snapshot."""


def _check_recorded(partials: history.CanonicalPartials, timeline: Timeline,
                    start: int, end: int) -> None:
    edges = bucket_edges(start, end, "1h")
    hourly, _ = partials.fold(edges)
    activity_counts: dict[int, int] = {}
    for segment in timeline.segments:
        a, b = max(start, segment.start), min(end, segment.end)
        if a < b:
            hour = floor_hour(a)
            activity_counts[hour] = activity_counts.get(hour, 0) + (b - a) // MINUTE
    for (a, b), stats in zip(edges, hourly, strict=True):
        recorded = stats[RECORDED].n if RECORDED in stats else 0
        activity = activity_counts.get(floor_hour(a), 0)
        if recorded != activity:
            raise ReportHistoryInconsistent(
                f"report recorded-minute mismatch at {iso_utc(a)}–{iso_utc(b)}:"
                f" history={recorded}, activity={activity}")


def query(storage: Storage, period: report.ReportPeriod, now: float, closed_until: int) -> dict:
    limit = report.effective_to(period, closed_until)
    with storage.session() as session:
        if limit > period.start:
            partials = history.canonical_partials(
                session, period.start, limit, REQUIRED_SERIES, period.bucket)
            buckets, _ = partials.fold(period.edges)
            loaded = activity_history.load_timeline(
                session, period.start, limit, closed_until, left_floor=None, raw_edge=True)
            timeline = loaded.timeline
            _check_recorded(partials, timeline, period.start, limit)
        else:
            buckets = [{} for _ in period.edges]
            timeline = Timeline(period.start, period.start, closed_until, ())
    return report.compose_report(report.ReportInput(period, now, closed_until, buckets, timeline))
