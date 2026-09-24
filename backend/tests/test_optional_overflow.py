"""Valid finite optional values whose aggregate sum cannot fit in DOUBLE."""

import math

import pytest

from conftest import Api, T0, row
from pompa import history
from pompa.aggregation import (OptionalHistoryInconsistent, OptionalStats, combine_optional,
                               fold_optional_minutes)
from pompa.history_profile import HISTORY_PROFILES_BY_IDENTITY
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator, iso_utc
from pompa.optional_minute import OptionalMinute
from pompa.optional_policy import resolve_series_id
from pompa.recorder import RecordedMinute, Recorder, PurgeRefused, persist, purge_step, roll_next_hour
from pompa.timegrid import HOUR, Unrepresentable


def select(db, names):
    with db.session() as s:
        head = s.lock_policy_head()
        ids = [resolve_series_id(s, HISTORY_PROFILES_BY_IDENTITY[name], T0) for name in names]
        revision = s.insert_revision(head, T0, T0)
        s.insert_revision_members(revision, ids)
        s.update_policy_head(revision)
    return dict(zip(names, ids))


def pair(ts, **values):
    return RecordedMinute(row(ts, outside_temp=1.0), OptionalMinute(ts, values))


def rolled(db, hour=T0):
    with db.session() as s:
        return {sid: (selected, known, total, low, high, last)
                for _h, sid, selected, known, total, low, high, last
                in s.read_optional_rollup(hour, hour + HOUR)}


def query(db, bucket, start=T0, end=T0 + HOUR, selector="optional:TOP90@1"):
    return history.query(db, start, end, bucket, [selector], end)["series"][selector]


def api_history(api, bucket, start=T0, end=T0 + HOUR, selector="optional:TOP90@1"):
    return api.get("/api/v1/history", None,
                   **{"from": iso_utc(start), "to": iso_utc(end),
                      "bucket": bucket, "series": selector})


def test_pure_optional_sum_overflow_is_sticky_and_last_never_sums():
    huge = OptionalStats(1, 1, 1e308, 1e308, 1e308, 1e308)
    overflow = combine_optional(huge, huge)
    assert overflow == OptionalStats(2, 2, None, 1e308, 1e308, 1e308)
    cancelled = combine_optional(overflow, OptionalStats(1, 1, -1e308,
                                                          -1e308, -1e308, -1e308))
    assert cancelled == OptionalStats(3, 3, None, -1e308, 1e308, -1e308)
    class Member:
        id = 1
        kind = "last"
    minutes = [T0 + i * 60 for i in range(60)]
    result = fold_optional_minutes(minutes, {ts: [Member()] for ts in minutes},
                                   {ts: {"1": 1e307} for ts in minutes})[1]
    assert result == OptionalStats(60, 60, None, 1e307, 1e307, 1e307)


def test_huge_last_raw_and_rolled_history_survives_shared_purge(mariadb):
    db = mariadb
    db.ensure_schema()
    sid = select(db, ["TOP90"])["TOP90"]
    persist(db, [pair(T0 + i * 60, TOP90=1e307) for i in range(18)])
    # Before rollup, hourly and total buckets read the same raw minute facts.
    for bucket in ("1m", "5m", "1h", "1d", "total"):
        entry = query(db, bucket)
        assert sum(entry["known_minutes"]) == 18
        assert all(value == 1e307 for value in entry["last"] if value is not None)
    assert roll_next_hour(db, T0 + HOUR) == T0
    with db.session() as s:
        assert s.rolled_until() == T0 + HOUR
    assert rolled(db)[sid] == (18, 18, None, 1e307, 1e307, 1e307)
    api = Api(start=T0, storage=db)
    for bucket in ("1m", "5m", "1h", "1d", "total"):
        assert api_history(api, bucket).status_code == 200
    # Advance the single frontier far enough that the first hour is purgeable.
    persist(db, [pair(T0 + 3 * HOUR)])
    assert roll_next_hour(db, T0 + 4 * HOUR) == T0 + 3 * HOUR
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 1)[1] == 18
    with db.session() as s:
        assert s.read_minutes(T0, T0 + HOUR) == []
        assert s.read_optional_minutes(T0, T0 + HOUR) == []
    for bucket in ("1h", "1d", "total"):
        entry = query(db, bucket)
        assert entry["selected_minutes"] == [18]
        assert entry["known_minutes"] == [18]
        assert entry["last"] == [1e307]
        assert entry["min"] == [1e307] and entry["max"] == [1e307]


def test_unrequested_huge_last_does_not_poison_other_query_but_bad_raw_still_fails(mariadb):
    db = mariadb
    db.ensure_schema()
    ids = select(db, ["TOP90", "TOP21"])
    persist(db, [pair(T0 + i * 60, TOP90=1e307, TOP21=2.5) for i in range(18)])
    assert query(db, "1h", selector="optional:TOP21@1")["avg"] == [2.5]
    with db.session() as s:
        s.replace_optional_minute(T0, {str(ids["TOP21"]): 2.5, "0": 1e307})
    with pytest.raises(OptionalHistoryInconsistent, match="invalid optional raw series key"):
        query(db, "1h", selector="optional:TOP21@1")


def test_late_huge_last_recorder_write_clears_protected_and_corrects_both_rollups(mariadb):
    db = mariadb
    db.ensure_schema()
    sid = select(db, ["TOP90"])["TOP90"]
    persist(db, [pair(T0 + i * 60, TOP90=1e307) for i in range(17)])
    assert roll_next_hour(db, T0 + HOUR) == T0
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0 + 17 * 60), db, 4)
    rec.on_connect(T0 + 17 * 60)
    rec.on_message("main/Main_Outlet_Temp", "35", False, T0 + 17 * 60)
    rec.on_message("main/Room_Heater_Operations_Hours", "1e307", False, T0 + 17 * 60)
    rec.tick(T0 + 18 * 60)
    assert rec._protected == [] and rec.rows_written == 1 and rec.rollup_error is None
    assert rolled(db)[sid] == (18, 18, None, 1e307, 1e307, 1e307)
    with db.session() as s:
        assert len(s.read_minutes(T0, T0 + 18 * 60)) == 18
        assert len(s.read_optional_minutes(T0, T0 + 18 * 60)) == 18
        assert s.read_rollup(T0, T0 + HOUR, ["recorded"])[0][2] == 18
    rec.tick(T0 + 19 * 60)
    assert rec._protected == [] and rec.rows_written == 2
    with db.session() as s:
        assert len(s.read_minutes(T0, T0 + 19 * 60)) == 19


def test_mean_hour_overflow_rolls_and_purges_but_avg_is_controlled_422(mariadb):
    db = mariadb
    db.ensure_schema()
    sid = select(db, ["TOP64"])["TOP64"]
    persist(db, [pair(T0 + i * 60, TOP64=1e307) for i in range(18)])
    api = Api(start=T0, storage=db)
    assert api_history(api, "1h", selector="optional:TOP64@1").status_code == 422
    assert roll_next_hour(db, T0 + HOUR) == T0
    assert rolled(db)[sid] == (18, 18, None, 1e307, 1e307, 1e307)
    with db.session() as s:
        assert s.rolled_until() == T0 + HOUR
    response = api_history(api, "1h", selector="optional:TOP64@1")
    assert response.status_code == 422 and "optional:TOP64@1" in response.json()["detail"]
    persist(db, [pair(T0 + 3 * HOUR)])
    roll_next_hour(db, T0 + 4 * HOUR)
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 1)[1] == 18
    assert api_history(api, "1h", selector="optional:TOP64@1").status_code == 422


def test_two_finite_hour_sums_overflow_only_at_bucket_composition(mariadb):
    db = mariadb
    db.ensure_schema()
    sid = select(db, ["TOP64"])["TOP64"]
    pairs = [pair(T0 + h * HOUR + i * 60, TOP64=1e307)
             for h in range(2) for i in range(9)]
    persist(db, pairs)
    api = Api(start=T0, storage=db)
    for bucket in ("1d", "total"):
        assert api_history(api, bucket, end=T0 + 2 * HOUR,
                           selector="optional:TOP64@1").status_code == 422
    assert roll_next_hour(db, T0 + 2 * HOUR) == T0
    assert roll_next_hour(db, T0 + 2 * HOUR) == T0 + HOUR
    hour_sum = rolled(db, T0)[sid][2]
    assert math.isfinite(hour_sum) and rolled(db, T0 + HOUR)[sid][2] == hour_sum
    assert query(db, "1h", T0, T0 + 2 * HOUR,
                 "optional:TOP64@1")["avg"] == [hour_sum / 9, hour_sum / 9]
    for bucket in ("1d", "total"):
        assert api_history(api, bucket, end=T0 + 2 * HOUR,
                           selector="optional:TOP64@1").status_code == 422


def test_energy_sum_overflow_is_controlled_422(mariadb):
    db = mariadb
    db.ensure_schema()
    sid = select(db, ["XTOP1"])["XTOP1"]
    persist(db, [pair(T0 + h * HOUR + i * 60, XTOP1=1e307)
                 for h in range(2) for i in range(9)])
    api = Api(start=T0, storage=db)
    assert api_history(api, "total", end=T0 + 2 * HOUR,
                       selector="optional:XTOP1@1").status_code == 422
    assert roll_next_hour(db, T0 + 2 * HOUR) == T0
    assert roll_next_hour(db, T0 + 2 * HOUR) == T0 + HOUR
    assert math.isfinite(rolled(db, T0)[sid][2])
    assert rolled(db, T0 + HOUR)[sid][2] == rolled(db, T0)[sid][2]
    assert api_history(api, "total", end=T0 + 2 * HOUR,
                       selector="optional:XTOP1@1").status_code == 422
    # The same durable unrepresentable marker is also safe under shared purge.
    persist(db, [pair(T0 + 4 * HOUR)])
    roll_next_hour(db, T0 + 5 * HOUR)
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 2)[1] == 18
    assert api_history(api, "total", end=T0 + 2 * HOUR,
                       selector="optional:XTOP1@1").status_code == 422


def test_last_rollup_with_non_null_sum_is_corrupt(mariadb):
    db = mariadb
    db.ensure_schema()
    sid = select(db, ["TOP90"])["TOP90"]
    persist(db, [pair(T0, TOP90=2.0)])
    roll_next_hour(db, T0 + HOUR)
    with db.session() as s:
        s.replace_optional_rollup_hour(T0, [(sid, 1, 1, 2.0, 2.0, 2.0, 2.0)])
    with pytest.raises(OptionalHistoryInconsistent, match="last-series sum"):
        query(db, "1h")
    persist(db, [pair(T0 + 3 * HOUR)])
    roll_next_hour(db, T0 + 4 * HOUR)
    with pytest.raises(PurgeRefused):
        purge_step(db, T0 + 100 * HOUR, 1, None, 1)


def test_history_only_maps_dedicated_request_errors_to_400(monkeypatch):
    api = Api(start=T0)
    assert api_history(api, "1m", selector="optional:TOP21@1").status_code == 400
    def internal_failure(*_args, **_kwargs):
        raise ValueError("internal invariant")
    monkeypatch.setattr(history, "query", internal_failure)
    with pytest.raises(ValueError, match="internal invariant"):
        api_history(api, "1m", selector="outside_temp")
