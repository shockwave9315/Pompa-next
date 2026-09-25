"""History engine: read-path composition, raw/rollup equivalence, energy, COP and coverage.

Tests taking ``any_storage`` run on FakeStorage and, with POMPA_TEST_DB_HOST, on MariaDB.
"""

from contextlib import contextmanager
from datetime import date
import json

import pytest

from conftest import T0, FakeStorage, minutes, row, sample
from conftest import persist_canonical
from pompa import activity_history, history
from pompa.aggregation import fold_minutes
from pompa.recorder import purge_step, roll_next_hour
from pompa.timegrid import Unrepresentable, bucket_edges, local_midnight

H = 3600
H0 = T0  # 2027-01-15T08:00:00Z
ALL = list(history.HISTORY_SERIES)


def q(storage, start, end, bucket, series=ALL, now=H0 + 1000 * H):
    return history.query(storage, start, end, bucket, list(series), now)


def roll_until(storage, closed_before):
    while roll_next_hour(storage, closed_before) is not None:
        pass


def drop_rollups(storage):
    if isinstance(storage, FakeStorage):
        storage.rollup = {}
    else:
        with storage._connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM rollup_1h")
            conn.commit()


def spy(storage, monkeypatch):
    """Records which raw spans and how many rollup rows each query read."""
    calls = {"raw": [], "rollup_rows": 0}
    original = storage.session

    @contextmanager
    def session():
        with original() as s:
            read_minutes, read_rollup = s.read_minutes, s.read_rollup

            def rm(a, b, *args):
                calls["raw"].append((a, b))
                return read_minutes(a, b, *args)

            def rr(*args):
                rows = read_rollup(*args)
                calls["rollup_rows"] += len(rows)
                return rows

            s.read_minutes, s.read_rollup = rm, rr
            yield s

    monkeypatch.setattr(storage, "session", session)
    return calls


def gappy(start, hours):
    """Minutes with scattered missing rows, NULL metrics and one 100-minute outage."""
    return [sample(start + 60 * i, i) for i in range(hours * 60) if i % 17 and not 130 <= i < 230]


@pytest.mark.parametrize("bucket", ["1m", "1h", "total"])
def test_canonical_partials_use_caller_session_and_match_whole_history_response(
        any_storage, monkeypatch, bucket):
    persist_canonical(any_storage, gappy(H0, 3))
    roll_until(any_storage, H0 + 2 * H)
    start, end, now = H0 + 7 * 60, H0 + 3 * H, H0 + 4 * H
    series = ["outside_temp", "co_power_consumption", "cop_co", "cop_total"]
    expected = q(any_storage, start, end, bucket, series, now)
    original_session = any_storage.session
    with original_session() as session:
        def nested_session():
            raise AssertionError("extraction opened a nested storage session")

        monkeypatch.setattr(any_storage, "session", nested_session)
        partials = history.canonical_partials(session, start, end, history._needed(series), bucket)
        edges = bucket_edges(start, end, bucket)
        canonical, optional = partials.fold(edges)
        actual = history._response(start, end, bucket, bucket, edges, canonical, series, now,
                                   optional, {})
        assert actual == expected
        assert json.dumps(actual, ensure_ascii=False, separators=(",", ":")) == json.dumps(
            expected, ensure_ascii=False, separators=(",", ":"))
        assert session.read_minutes(start, start + 60)  # the caller still owns a usable session


def test_history_and_activity_extractions_share_one_caller_session(any_storage, monkeypatch):
    persist_canonical(any_storage, [sample(H0, 0), sample(H0 + 60, 1)])
    original_session = any_storage.session
    with original_session() as session:
        monkeypatch.setattr(any_storage, "session", lambda: (_ for _ in ()).throw(
            AssertionError("extraction opened a nested storage session")))
        assert history.canonical_partials(session, H0, H0 + 2 * 60,
                                          history._needed(["cop_co"]), "1m").fold(
                                              [(H0, H0 + 2 * 60)])[0][0]["recorded"].n == 2
        loaded = activity_history.load_timeline(session, H0, H0 + 2 * 60, H0 + H)
        assert loaded.timeline is not None
        assert session.read_minutes(H0, H0 + 60)


# ------------------------------------------------------------------ raw == mixed


SHAPES = [
    ("1h", H0 + 7 * 60, H0 + 71 * H + 13 * 60),  # unaligned edges, spans rolled and unrolled hours
    ("total", H0 + 7 * 60, H0 + 71 * H + 13 * 60),
    ("1h", H0, H0 + 72 * H),
    ("total", H0 + 3 * H, H0 + 5 * H),  # exactly complete rolled hours
    ("total", H0 + 600, H0 + 1800),  # inside one hour
    ("1d", H0 - 5 * H + 60, H0 + 72 * H),
    ("total", H0 + 59 * H + 1200, H0 + 61 * H + 60),  # straddles rolled_until
]


@pytest.mark.parametrize("bucket,start,end", SHAPES)
def test_raw_only_and_mixed_reads_are_identical(any_storage, monkeypatch, bucket, start, end):
    persist_canonical(any_storage, gappy(H0, 72))
    roll_until(any_storage, H0 + 60 * H)  # the last 12 hours stay unrolled
    calls = spy(any_storage, monkeypatch)
    mixed = q(any_storage, start, end, bucket)
    complete_rolled_hours = max(0, min(end - end % H, H0 + 60 * H) - (start + (-start) % H)) // H
    assert (calls["rollup_rows"] > 0) == (complete_rolled_hours > 0)
    drop_rollups(any_storage)
    raw_only = q(any_storage, start, end, bucket)
    assert calls["raw"][-1] == (start, end)  # really raw-only this time
    assert mixed == raw_only  # every statistic, energy, COP and coverage value, bit for bit
    assert sum(b["recorded_minutes"] for b in mixed["buckets"]) > 0


def test_mixed_read_matches_an_independent_flat_fold(monkeypatch):
    storage = FakeStorage()
    rows = gappy(H0, 30)
    persist_canonical(storage, rows)
    roll_until(storage, H0 + 24 * H)
    start, end = H0 + 25 * 60, H0 + 27 * H + 50 * 60
    body = q(storage, start, end, "total")
    inside = [(r.ts, r.values) for r in rows if start <= r.ts < end]
    ref = fold_minutes(inside)  # one flat fold, no hourly grouping
    assert body["buckets"][0]["recorded_minutes"] == ref["recorded"].n == len(inside)
    temp = body["series"]["main_outlet_temp"]
    assert temp["minutes"] == [ref["main_outlet_temp"].n]
    assert (temp["min"], temp["max"]) == ([ref["main_outlet_temp"].min], [ref["main_outlet_temp"].max])
    assert temp["avg"][0] == pytest.approx(ref["main_outlet_temp"].avg, rel=1e-12)
    assert body["series"]["operating_mode"]["last"] == [ref["operating_mode"].last]
    cop = body["series"]["cop_total"]
    assert cop["paired_minutes"] == [ref["pair_total_in"].n]
    assert cop["cop"][0] == pytest.approx(ref["pair_total_out"].sum / ref["pair_total_in"].sum, rel=1e-12)


def test_dst_days_are_identical_on_raw_and_rollup_paths(any_storage, monkeypatch):
    first = local_midnight(date(2027, 10, 30))
    last = local_midnight(date(2027, 11, 2))
    persist_canonical(any_storage, gappy(first, (last - first) // H))
    roll_until(any_storage, last - 5 * H)
    frm, to = local_midnight(date(2027, 10, 30)), local_midnight(date(2027, 11, 2))
    mixed = q(any_storage, frm, to, "1d", now=to + H)
    assert [b["expected_minutes"] for b in mixed["buckets"]] == [1440, 1500, 1440]
    drop_rollups(any_storage)
    assert q(any_storage, frm, to, "1d", now=to + H) == mixed


# ------------------------------------------------------------------ hand-calculated buckets


def test_5m_buckets_hand_calculated(any_storage):
    persist_canonical(any_storage, [
        row(H0, outside_temp=1.0, operating_mode=4.0),
        row(H0 + 60, outside_temp=None, operating_mode=3.0),  # stored NULL
        # H0 + 120: no row
        row(H0 + 180, outside_temp=0.0, operating_mode=None),  # real zero; trailing NULL last
        row(H0 + 300, outside_temp=-3.0, operating_mode=1.0),
    ])
    body = q(any_storage, H0, H0 + 600, "5m", ["outside_temp", "operating_mode"], now=H0 + 480)
    assert [(b["start"], b["end"]) for b in body["buckets"]] == [
        ("2027-01-15T08:00:00Z", "2027-01-15T08:05:00Z"), ("2027-01-15T08:05:00Z", "2027-01-15T08:10:00Z")]
    assert [b["recorded_minutes"] for b in body["buckets"]] == [3, 1]
    assert [b["expected_minutes"] for b in body["buckets"]] == [5, 3]  # elapsed minutes 08:05..08:07 only
    assert [b["coverage_percent"] for b in body["buckets"]] == [60.0, 33.3]
    temp = body["series"]["outside_temp"]
    assert (temp["avg"], temp["min"], temp["max"], temp["minutes"]) == ([0.5, -3.0], [0.0, -3.0], [1.0, -3.0], [2, 1])
    mode = body["series"]["operating_mode"]
    assert (mode["last"], mode["min"], mode["max"], mode["minutes"]) == ([3.0, 1.0], [3.0, 1.0], [4.0, 1.0], [2, 1])


def test_energy_of_an_incomplete_hour_is_not_extrapolated(any_storage):
    # 30 recorded minutes at 1200 W in hour 0, then a stored NULL minute: 0.6 kWh, not 1.2 kWh.
    persist_canonical(any_storage, [row(H0 + 60 * i, co_power_consumption=1200.0) for i in range(30)]
            + [row(H0 + 30 * 60, co_power_consumption=None)])
    roll_until(any_storage, H0 + H)
    body = q(any_storage, H0, H0 + 2 * H, "1h", ["co_power_consumption"], now=H0 + 2 * H)
    power = body["series"]["co_power_consumption"]
    assert power["kwh"] == [0.6, None] and power["minutes"] == [30, 0]
    assert power["avg"] == [1200.0, None]
    assert [b["recorded_minutes"] for b in body["buckets"]] == [31, 0]
    assert [b["coverage_percent"] for b in body["buckets"]] == [51.7, 0.0]
    assert "kwh" not in q(any_storage, H0, H0 + H, "1h", ["outside_temp"])["series"]["outside_temp"]


def test_period_cop_hand_calculated(any_storage):
    def p(ts, ci, co, di, do):
        return row(ts, co_power_consumption=ci, co_power_production=co,
                   dhw_power_consumption=di, dhw_power_production=do)

    persist_canonical(any_storage, [
        p(H0, 1000.0, 4000.0, 0.0, 0.0),
        p(H0 + 60, 500.0, 1000.0, 18.0, 0.0),  # 18 W in / 0 W out lowers DHW and total COP
        p(H0 + 120, 0.0, 0.0, 0.0, 0.0),  # paired, adds no denominator
        p(H0 + 180, 1000.0, None, 600.0, 1800.0),  # CO unpaired, DHW paired
        p(H0 + H + 60, 400.0, 1600.0, None, None),  # next hour, CO only
    ])
    roll_until(any_storage, H0 + H)
    body = q(any_storage, H0, H0 + 2 * H, "total", ["cop_co", "cop_dhw", "cop_total"])
    s = body["series"]
    assert s["cop_co"] == {"label": "COP CO", "unit": None, "kind": "cop", "cop": [6600.0 / 1900.0],
                           "paired_minutes": [4], "input_kwh": [1900.0 / 60000], "output_kwh": [6600.0 / 60000]}
    assert s["cop_dhw"]["cop"] == [1800.0 / 618.0] and s["cop_dhw"]["paired_minutes"] == [4]
    assert s["cop_total"]["cop"] == [5000.0 / 1518.0] and s["cop_total"]["paired_minutes"] == [3]
    assert s["cop_total"]["input_kwh"] == [1518.0 / 60000]


def test_empty_buckets_are_structural(any_storage):
    body = q(any_storage, H0, H0 + 3 * H, "1h", ["outside_temp", "co_power_consumption", "cop_co"], now=H0 + 3 * H)
    assert [b["recorded_minutes"] for b in body["buckets"]] == [0, 0, 0]
    assert [b["expected_minutes"] for b in body["buckets"]] == [60, 60, 60]
    assert [b["coverage_percent"] for b in body["buckets"]] == [0.0, 0.0, 0.0]
    assert body["series"]["outside_temp"]["avg"] == [None] * 3
    assert body["series"]["co_power_consumption"]["kwh"] == [None] * 3
    assert body["series"]["cop_co"]["paired_minutes"] == [0] * 3 and body["series"]["cop_co"]["cop"] == [None] * 3


def test_stored_null_differs_from_missing_row_in_rolled_hours(any_storage):
    persist_canonical(any_storage, [row(H0 + 60 * i, outside_temp=None) for i in range(10)]
            + [row(H0 + 600 + 60 * i, outside_temp=2.0) for i in range(5)])
    roll_until(any_storage, H0 + H)
    body = q(any_storage, H0, H0 + H, "1h", ["outside_temp"], now=H0 + H)
    assert body["buckets"][0]["recorded_minutes"] == 15  # NULL minutes are recorded minutes
    assert body["series"]["outside_temp"]["minutes"] == [5]
    assert body["buckets"][0]["expected_minutes"] == 60  # the 45 absent rows stay absent


def test_partial_edge_hours_read_raw_minutes_only(any_storage, monkeypatch):
    persist_canonical(any_storage, minutes(H0, 5 * 60))
    roll_until(any_storage, H0 + 5 * H)
    calls = spy(any_storage, monkeypatch)
    body = q(any_storage, H0 + 10 * 60, H0 + 3 * H + 20 * 60, "1h", ["outside_temp"])
    assert calls["raw"] == [(H0 + 600, H0 + H), (H0 + 3 * H, H0 + 3 * H + 1200)]
    assert calls["rollup_rows"] == 2 * 2  # hours 1 and 2: recorded + outside_temp
    assert [(b["start"], b["end"]) for b in body["buckets"]] == [
        ("2027-01-15T08:10:00Z", "2027-01-15T09:00:00Z"), ("2027-01-15T09:00:00Z", "2027-01-15T10:00:00Z"),
        ("2027-01-15T10:00:00Z", "2027-01-15T11:00:00Z"), ("2027-01-15T11:00:00Z", "2027-01-15T11:20:00Z")]
    assert [b["expected_minutes"] for b in body["buckets"]] == [50, 60, 60, 20]
    assert [b["recorded_minutes"] for b in body["buckets"]] == [50, 60, 60, 20]


def test_minute_buckets_never_read_rollups(any_storage, monkeypatch):
    persist_canonical(any_storage, minutes(H0, 3 * 60))
    roll_until(any_storage, H0 + 3 * H)
    calls = spy(any_storage, monkeypatch)
    for bucket in ("1m", "5m"):
        q(any_storage, H0, H0 + 3 * H, bucket, ["outside_temp"])
    assert calls["rollup_rows"] == 0 and calls["raw"] == [(H0, H0 + 3 * H)] * 2


def test_expected_minutes_are_not_trimmed_to_recording_start(any_storage):
    persist_canonical(any_storage, minutes(H0 + 30 * 60, 30))  # recording starts halfway through the hour
    body = q(any_storage, H0, H0 + H, "1h", ["outside_temp"], now=H0 + H)
    assert body["buckets"][0] | {} == {"start": "2027-01-15T08:00:00Z", "end": "2027-01-15T09:00:00Z",
                                       "expected_minutes": 60, "recorded_minutes": 30, "coverage_percent": 50.0}


def test_future_bucket_has_no_expected_minutes(any_storage):
    body = q(any_storage, H0, H0 + 3 * H, "1h", ["outside_temp"], now=H0 + H + 90)
    assert [b["expected_minutes"] for b in body["buckets"]] == [60, 1, 0]
    assert [b["coverage_percent"] for b in body["buckets"]] == [0.0, 0.0, None]


# ------------------------------------------------------------------ purged raw and 422


GAP_HOUR = H0 + 30 * H  # never recorded at all, and below every purge cutoff used here
PURGED_UNTIL = H0 + 48 * H  # raw physically deleted below this


def purged_history(storage):
    """72 recorded hours with one never-recorded hour, rolled, then really purged.

    Returns the ``now`` the purge ran at. Raw below ``PURGED_UNTIL`` is gone
    from the database; ``GAP_HOUR`` is a natural hole that was never recorded
    and therefore has no rollup row either.
    """
    persist_canonical(storage, [r for r in minutes(H0, 72 * 60) if not GAP_HOUR <= r.ts < GAP_HOUR + H])
    roll_until(storage, H0 + 72 * H)
    now = H0 + 72 * H
    more = True
    while more:
        _, _, more = purge_step(storage, now, retention_days=1, pending_from=None, max_hours=24)
    return now


# 422 must follow the database, not the wall clock or the configured retention. These are the
# perturbations that used to move the predicted floor backwards and re-open purged ranges.
PERTURBED = pytest.mark.parametrize("shift", [0, -24 * H], ids=["clock_sane", "clock_back_24h"])


def test_purged_raw_is_422_for_minute_buckets(any_storage):
    now = purged_history(any_storage)
    for bucket in ("1m", "5m"):
        with pytest.raises(Unrepresentable, match="was purged"):
            q(any_storage, H0 + 24 * H, H0 + 25 * H, bucket, now=now)


@PERTURBED
def test_purged_raw_stays_422_after_a_backward_clock_step(any_storage, shift):
    now = purged_history(any_storage) + shift
    with pytest.raises(Unrepresentable, match="was purged"):
        q(any_storage, H0 + 24 * H, H0 + 25 * H, "1m", now=now)
    with pytest.raises(Unrepresentable, match="was purged"):  # partial 1h edge needs the same raw
        q(any_storage, H0 + 24 * H + 1800, H0 + 26 * H, "1h", now=now)


def test_purged_raw_stays_422_when_retention_is_raised_or_disabled(any_storage):
    """Retention is policy; it cannot resurrect minutes the database no longer has."""
    now = purged_history(any_storage)
    for bucket in ("1m", "auto"):
        with pytest.raises(Unrepresentable, match="was purged"):
            q(any_storage, H0 + 24 * H + 1800, H0 + 25 * H, bucket, now=now)
    with pytest.raises(Unrepresentable, match="was purged"):
        q(any_storage, H0 + 24 * H, H0 + 25 * H, "1m", now=now)


def test_partial_edge_inside_a_purged_hour_is_found(any_storage):
    """A request starting at 08:30 must still see the purged hour starting at 08:00."""
    now = purged_history(any_storage)
    start = H0 + 24 * H + 1800
    with any_storage.session() as s:
        assert s.first_purged_hour(start, start + 1800) == H0 + 24 * H
    for bucket in ("1h", "1d", "total"):
        with pytest.raises(Unrepresentable, match="was purged"):
            q(any_storage, start, H0 + 26 * H, bucket, now=now)


@PERTURBED
def test_whole_rolled_hours_over_purged_raw_are_served_from_rollup(any_storage, shift):
    now = purged_history(any_storage) + shift
    body = q(any_storage, H0 + 24 * H, H0 + 26 * H, "1h", ["outside_temp"], now=now)
    assert [b["recorded_minutes"] for b in body["buckets"]] == [60, 60]


@PERTURBED
def test_a_never_recorded_hour_below_the_cutoff_is_an_ordinary_empty_answer(any_storage, shift):
    """No raw and no rollup is not evidence of purge: it means the minutes never existed."""
    now = purged_history(any_storage) + shift
    body = q(any_storage, GAP_HOUR, GAP_HOUR + H, "1m", ["outside_temp"], now=now)
    assert body["bucket"] == "1m"
    assert [b["recorded_minutes"] for b in body["buckets"]] == [0] * 60
    assert [b["coverage_percent"] for b in body["buckets"]] == [0.0] * 60


def test_a_range_from_before_the_recorder_existed_is_not_422(any_storage):
    now = purged_history(any_storage)
    body = q(any_storage, H0 - 10 * H, H0 - 9 * H, "1m", ["outside_temp"], now=now)
    assert sum(b["recorded_minutes"] for b in body["buckets"]) == 0


def test_auto_promotes_to_1h_when_the_range_needs_purged_raw(any_storage):
    now = purged_history(any_storage)
    body = q(any_storage, H0 + 24 * H, H0 + 26 * H, "auto", ["outside_temp"], now=now)
    assert (body["bucket"], body["requested_bucket"]) == ("1h", "auto")
    assert body["series"]["outside_temp"]["minutes"] == [48, 48]  # from rollup_1h
    recent = q(any_storage, H0 + 60 * H, H0 + 61 * H, "auto", ["outside_temp"], now=now)
    assert recent["bucket"] == "1m"  # raw still there, no promotion
    with pytest.raises(Unrepresentable):  # promoted, but the partial edge still needs purged raw
        q(any_storage, H0 + 24 * H + 60, H0 + 26 * H, "auto", now=now)


def test_without_any_rollup_raw_is_always_servable(any_storage):
    persist_canonical(any_storage, minutes(H0, 60))
    body = q(any_storage, H0, H0 + H, "1m", ["outside_temp"], now=H0 + 800 * 86400)
    assert body["bucket"] == "1m" and sum(b["recorded_minutes"] for b in body["buckets"]) == 60


def test_unknown_series_is_rejected():
    with pytest.raises(ValueError):
        q(FakeStorage(), H0, H0 + H, "1h", ["nope"])
