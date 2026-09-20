"""Bucket alignment, Europe/Warsaw calendar days and DST, auto selection, purge cutoff."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pompa import timegrid
from pompa.timegrid import (
    DAY, HOUR, MAX_BUCKETS, Unrepresentable, auto_bucket, bucket_edges, ceil_hour, expected_minutes,
    floor_hour, local_date, local_midnight, purge_cutoff, validate_local_days,
)

T0 = 1_800_000_000  # 2027-01-15T08:00:00Z


def utc(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ alignment


def test_floor_and_ceil_hour():
    assert floor_hour(T0 + 1) == T0 and ceil_hour(T0 + 1) == T0 + HOUR
    assert floor_hour(T0) == ceil_hour(T0) == T0


def test_expected_minutes_counts_elapsed_minutes_only():
    assert expected_minutes(T0, T0 + HOUR, now=T0 + 2 * HOUR) == 60
    assert expected_minutes(T0, T0 + HOUR, now=T0) == 0
    assert expected_minutes(T0, T0 + HOUR, now=T0 + 119) == 1  # minute 0 elapsed, minute 1 not
    assert expected_minutes(T0, T0 + HOUR, now=T0 + 120) == 2
    assert expected_minutes(T0 + 600, T0 + 900, now=T0 + 10 * DAY) == 5


# ------------------------------------------------------------------ buckets


def test_utc_buckets_clip_the_requested_edges():
    assert bucket_edges(T0 + 130, T0 + 620, "5m") == [
        (T0 + 130, T0 + 300), (T0 + 300, T0 + 600), (T0 + 600, T0 + 620)]
    assert bucket_edges(T0 + 130, T0 + 620, "total") == [(T0 + 130, T0 + 620)]
    assert bucket_edges(T0 + 60, T0 + 120, "1m") == [(T0 + 60, T0 + 120)]
    assert bucket_edges(T0 - 1800, T0 + 1800, "1h") == [(T0 - 1800, T0), (T0, T0 + 1800)]


def test_buckets_are_contiguous_and_cover_the_range_exactly():
    for bucket in ("1m", "5m", "1h", "1d", "total"):
        span = DAY if bucket in ("1m", "5m") else 3 * DAY
        edges = bucket_edges(T0 + 137 * 60, T0 + 137 * 60 + span, bucket)
        assert edges[0][0] == T0 + 137 * 60 and edges[-1][1] == T0 + 137 * 60 + span
        assert all(a < b for a, b in edges)
        assert all(edges[i][1] == edges[i + 1][0] for i in range(len(edges) - 1))


@pytest.mark.parametrize("bucket,over,under", [
    ("1m", MAX_BUCKETS + 1, MAX_BUCKETS), ("5m", 5 * (MAX_BUCKETS + 1), 5 * MAX_BUCKETS),
    ("1h", 60 * (MAX_BUCKETS + 1), 60 * MAX_BUCKETS)])
def test_bucket_limit_is_refused_not_truncated(bucket, over, under):
    assert len(bucket_edges(T0, T0 + under * 60, bucket)) == MAX_BUCKETS
    with pytest.raises(Unrepresentable):
        bucket_edges(T0, T0 + over * 60, bucket)


def test_daily_bucket_limit():
    start = local_midnight(date(2000, 1, 1))
    assert len(bucket_edges(start, start + MAX_BUCKETS * DAY, "1d")) == MAX_BUCKETS
    with pytest.raises(Unrepresentable):
        bucket_edges(start, start + (MAX_BUCKETS + 40) * DAY, "1d")


# ------------------------------------------------------------------ Europe/Warsaw calendar


def test_local_midnight_and_date():
    assert iso(local_midnight(date(2027, 1, 15))) == "2027-01-14T23:00:00Z"  # CET
    assert iso(local_midnight(date(2027, 7, 1))) == "2027-06-30T22:00:00Z"  # CEST
    assert local_date(utc("2027-01-14T23:00:00")) == date(2027, 1, 15)
    assert local_date(utc("2027-01-14T22:59:00")) == date(2027, 1, 14)


def test_ordinary_day_is_1440_minutes():
    start, end = local_midnight(date(2027, 1, 15)), local_midnight(date(2027, 1, 16))
    edges = bucket_edges(start, end, "1d")
    assert [(iso(a), iso(b)) for a, b in edges] == [("2027-01-14T23:00:00Z", "2027-01-15T23:00:00Z")]
    assert expected_minutes(*edges[0], now=end) == 1440


def test_spring_dst_day_is_1380_minutes():
    start, end = local_midnight(date(2027, 3, 28)), local_midnight(date(2027, 3, 29))
    edges = bucket_edges(start, end, "1d")
    assert [(iso(a), iso(b)) for a, b in edges] == [("2027-03-27T23:00:00Z", "2027-03-28T22:00:00Z")]
    assert end - start == 23 * HOUR
    assert expected_minutes(*edges[0], now=end) == 1380


def test_autumn_dst_day_is_1500_minutes():
    start, end = local_midnight(date(2027, 10, 31)), local_midnight(date(2027, 11, 1))
    edges = bucket_edges(start, end, "1d")
    assert [(iso(a), iso(b)) for a, b in edges] == [("2027-10-30T22:00:00Z", "2027-10-31T23:00:00Z")]
    assert end - start == 25 * HOUR
    assert expected_minutes(*edges[0], now=end) == 1500


@pytest.mark.parametrize("first,last,lengths", [
    (date(2027, 3, 26), date(2027, 3, 31), [1440, 1440, 1380, 1440, 1440]),
    (date(2027, 10, 29), date(2027, 11, 3), [1440, 1440, 1500, 1440, 1440]),
])
def test_dst_weeks_stay_contiguous_and_non_overlapping(first, last, lengths):
    start, end = local_midnight(first), local_midnight(last)
    edges = bucket_edges(start, end, "1d")
    assert [(b - a) // 60 for a, b in edges] == lengths
    assert [expected_minutes(a, b, now=end) for a, b in edges] == lengths
    assert sum(b - a for a, b in edges) == end - start
    assert all(edges[i][1] == edges[i + 1][0] for i in range(len(edges) - 1))
    assert all(a % HOUR == 0 and b % HOUR == 0 for a, b in edges)  # whole UTC hours: rollups suffice


def test_daily_buckets_clip_partial_first_and_last_days():
    start = local_midnight(date(2027, 3, 28)) + 5 * HOUR
    end = local_midnight(date(2027, 3, 30)) + 90 * 60
    edges = bucket_edges(start, end, "1d")
    assert edges[0] == (start, local_midnight(date(2027, 3, 29)))
    assert edges[-1] == (local_midnight(date(2027, 3, 30)), end)
    assert len(edges) == 3


def test_startup_validation_accepts_warsaw_and_rejects_half_hour_zones(monkeypatch):
    validate_local_days()
    monkeypatch.setattr(timegrid, "LOCAL_TZ", ZoneInfo("Asia/Kolkata"))
    with pytest.raises(RuntimeError, match="not a whole UTC hour"):
        validate_local_days()


# ------------------------------------------------------------------ auto and purge cutoff


@pytest.mark.parametrize("length,bucket", [
    (HOUR, "1m"), (36 * HOUR, "1m"), (36 * HOUR + 60, "5m"), (10 * DAY, "5m"), (10 * DAY + 60, "1h"),
    (120 * DAY, "1h"), (120 * DAY + 60, "1d"), (3000 * DAY, "1d")])
def test_auto_uses_range_length(length, bucket):
    assert auto_bucket(T0, T0 + length) == bucket


def test_purge_cutoff_is_prospective_policy():
    """What purge may delete next. What it already deleted is ``Session.first_purged_hour``."""
    now = T0 + 400 * DAY
    assert purge_cutoff(now, rolled_until=None, retention_days=365) is None  # nothing rolled yet
    assert purge_cutoff(now, rolled_until=T0, retention_days=0) is None  # purge disabled
    assert purge_cutoff(now, rolled_until=now, retention_days=365) == floor_hour(now - 365 * DAY)
    # A rollup lagging behind retention holds the cutoff at its own reprocessing margin.
    assert purge_cutoff(now, rolled_until=T0 + HOUR, retention_days=365) == T0 - HOUR
