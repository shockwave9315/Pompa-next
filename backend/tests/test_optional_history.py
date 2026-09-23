"""Checkpoint D optional history uses the canonical bucket engine and persisted meanings."""

from dataclasses import replace
from datetime import date

import pytest

from conftest import Api, T0, FakeStorage, row
from pompa import history
from pompa.history_profile import HISTORY_PROFILES_BY_IDENTITY
from pompa.optional_minute import OptionalMinute
from pompa.optional_policy import resolve_series_id
from pompa.recorder import RecordedMinute, persist, purge_step, roll_next_hour
from pompa.timegrid import HOUR, Unrepresentable, local_midnight


def select(db, names, effective):
    with db.session() as s:
        head = s.lock_policy_head()
        ids = [resolve_series_id(s, HISTORY_PROFILES_BY_IDENTITY[name], effective) for name in names]
        rev = s.insert_revision(head, effective, effective)
        s.insert_revision_members(rev, ids)
        s.update_policy_head(rev)
    return rev


def pair(ts, **values):
    return RecordedMinute(row(ts, outside_temp=2.5), OptionalMinute(ts, values))


def roll_all(db, end):
    while roll_next_hour(db, end) is not None:
        pass


def query(db, start, end, bucket, *series):
    return history.query(db, start, end, bucket, series, end)


def test_selector_parser_and_default_contract():
    api = Api(start=T0)
    default = api.body("/api/v1/history", None, **{"from": "2027-01-15T08:00:00Z",
                                                  "to": "2027-01-15T08:01:00Z"})
    assert set(default["series"]) == set(history.HISTORY_SERIES)
    assert not any(name.startswith("optional:") for name in default["series"])
    bad = ["optional:TOP21", "optional:TOP21@0", "optional:@1", "optional:top21@1",
           "optional:TOP21@1x", "optional:TOP21@1,optional:TOP21@1"]
    for selector in bad:
        response = api.get("/api/v1/history", None, **{"from": "2027-01-15T08:00:00Z",
                                                        "to": "2027-01-15T08:01:00Z",
                                                        "series": selector})
        assert response.status_code == 400, selector
    assert api.get("/api/v1/history", None, **{"from": "2027-01-15T08:00:00Z",
                                                "to": "2027-01-15T08:01:00Z",
                                                "series": "optional:TOP21@1"}).status_code == 400


@pytest.mark.parametrize("version,grammar_valid,status", [
    ("1", True, 200),
    ("4294967295", True, 400),  # INT UNSIGNED ceiling, no persisted meaning
    ("4294967296", True, 400),  # 10 digits but beyond INT UNSIGNED, still an unknown meaning
    ("10000000000", False, 400),  # 11 digits
    ("9" * 5000, False, 400),
    ("0001", False, 400),
])
def test_optional_selector_version_boundary(version, grammar_valid, status):
    selector = f"optional:TOP21@{version}"
    assert bool(history.OPTIONAL_SELECTOR.fullmatch(selector)) is grammar_valid
    api = Api(start=T0)
    select(api.storage, ["TOP21"], T0)
    response = api.get("/api/v1/history", None, **{
        "from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:01:00Z",
        "series": selector})
    assert response.status_code == status


def test_series_discovery_is_persisted_and_mqtt_independent(mariadb):
    db = mariadb
    db.ensure_schema()
    api = Api(start=T0, storage=db)
    assert api.body("/api/v1/optional-history/series") == {"series": []}
    select(db, ["TOP21", "XTOP1"], T0)
    first = api.body("/api/v1/optional-history/series")["series"]
    assert [e["selector"] for e in first] == ["optional:TOP21@1", "optional:XTOP1@1"]
    assert [e["series_id"] for e in first] == sorted(e["series_id"] for e in first)
    assert first[0]["topic"] == "main/Outside_Pipe_Temp"
    assert first[1]["energy"] is True
    select(db, [], T0 + 60)
    assert api.body("/api/v1/optional-history/series")["series"] == first


def test_raw_optional_buckets_known_unknown_gap_zero_negative_last_and_energy(any_storage):
    db = any_storage
    select(db, ["TOP21", "TOP90", "XTOP1"], T0)
    persist(db, [pair(T0, TOP21=-2.5, TOP90=7, XTOP1=0.0), pair(T0 + 60),
                 pair(T0 + 180, TOP21=3.0, TOP90=8, XTOP1=120.0)])
    one = query(db, T0, T0 + 5 * 60, "1m", "optional:TOP21@1", "optional:TOP90@1",
                "optional:XTOP1@1")
    temp = one["series"]["optional:TOP21@1"]
    assert temp["selected_minutes"] == [1, 1, 0, 1, 0]
    assert temp["known_minutes"] == [1, 0, 0, 1, 0]
    assert temp["avg"] == [-2.5, None, None, 3.0, None]
    assert one["buckets"][2]["recorded_minutes"] == 0
    last = one["series"]["optional:TOP90@1"]
    assert last["last"] == [7.0, None, None, 8.0, None]
    power = one["series"]["optional:XTOP1@1"]
    assert power["avg"][0] == 0.0 and power["kwh"] == [0.0, None, None, 0.002, None]
    five = query(db, T0, T0 + 5 * 60, "5m", "optional:TOP21@1")
    assert five["series"]["optional:TOP21@1"]["selected_minutes"] == [3]
    assert five["series"]["optional:TOP21@1"]["known_minutes"] == [2]


def test_mixed_single_snapshot_and_optional_raw_rollup_exact_equivalence(any_storage):
    db = any_storage
    select(db, ["TOP21", "XTOP1"], T0)
    pairs = [pair(T0 + i * 60, **({"TOP21": 0.1 + i * 0.001, "XTOP1": 12.5 + i * 0.1}
                                 if i % 7 else {})) for i in range(3 * 60)]
    persist(db, pairs)
    roll_all(db, T0 + 3 * HOUR)
    names = ("outside_temp", "optional:TOP21@1", "optional:XTOP1@1")
    rolled = {bucket: query(db, T0, T0 + 3 * HOUR, bucket, *names)
              for bucket in ("1h", "1d", "total")}
    assert set(rolled["total"]["series"]) == set(names)
    # Force the same hour-piece engine to read raw for all hours, while raw still exists.
    if isinstance(db, FakeStorage):
        db.rollup = {}
    else:
        with db._connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM rollup_1h")
            conn.commit()
    for bucket in rolled:
        raw = query(db, T0, T0 + 3 * HOUR, bucket, *names)
        assert raw == rolled[bucket]
        for selector in names[1:]:
            a = raw["series"][selector]["avg"]
            b = rolled[bucket]["series"][selector]["avg"]
            assert [None if x is None else x.hex() for x in a] == [
                None if x is None else x.hex() for x in b]


def test_successful_optional_only_and_mixed_api_query(mariadb):
    db = mariadb
    db.ensure_schema()
    select(db, ["TOP21"], T0)
    persist(db, [pair(T0, TOP21=6.25)])
    api = Api(start=T0, storage=db)
    params = {"from": "2027-01-15T08:00:00Z", "to": "2027-01-15T08:01:00Z",
              "bucket": "1m"}
    optional = api.body("/api/v1/history", None, **{**params, "series": "optional:TOP21@1"})
    assert list(optional["series"]) == ["optional:TOP21@1"]
    assert optional["series"]["optional:TOP21@1"]["avg"] == [6.25]
    mixed = api.body("/api/v1/history", None,
                     **{**params, "series": "outside_temp,optional:TOP21@1"})
    assert list(mixed["series"]) == ["outside_temp", "optional:TOP21@1"]
    assert mixed["series"]["outside_temp"]["avg"] == [2.5]


def test_version_isolation_and_old_blocked_meaning_survives(mariadb, monkeypatch):
    db = mariadb
    db.ensure_schema()
    select(db, ["TOP21"], T0)
    persist(db, [pair(T0, TOP21=2.0)])
    profile_v2 = replace(HISTORY_PROFILES_BY_IDENTITY["TOP21"], profile_version=2,
                         label="Old v2 label", expected_topic="main/Another_Topic")
    with db.session() as s:
        base = s.lock_policy_head()
        v2 = resolve_series_id(s, profile_v2, T0 + 60)
        rev = s.insert_revision(base, T0 + 60, T0 + 60)
        s.insert_revision_members(rev, [v2])
        s.update_policy_head(rev)
        s.upsert_minutes([row(T0 + 60, outside_temp=2.5)])
        s.replace_optional_minute(T0 + 60, {str(v2): 5.0})
    monkeypatch.setattr("pompa.recorder.HISTORY_PROFILES_BY_IDENTITY", {})
    roll_all(db, T0 + HOUR)
    body = query(db, T0, T0 + HOUR, "1h", "optional:TOP21@1", "optional:TOP21@2")
    assert body["series"]["optional:TOP21@1"]["known_minutes"] == [1]
    assert body["series"]["optional:TOP21@2"]["known_minutes"] == [1]
    assert body["series"]["optional:TOP21@1"]["avg"] == [2.0]
    assert body["series"]["optional:TOP21@2"]["avg"] == [5.0]
    assert body["series"]["optional:TOP21@2"]["label"] == "Old v2 label"
    api = Api(start=T0, storage=db)
    assert len(api.body("/api/v1/optional-history/series")["series"]) == 2


def test_auto_promotes_after_shared_purge_and_explicit_minutes_refuse(mariadb):
    db = mariadb
    db.ensure_schema()
    select(db, ["TOP21"], T0)
    persist(db, [pair(T0, TOP21=4), pair(T0 + 3 * HOUR)])
    roll_all(db, T0 + 4 * HOUR)
    expected = query(db, T0, T0 + HOUR, "1h", "optional:TOP21@1")
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 1)[1] == 1
    auto = query(db, T0, T0 + HOUR, "auto", "optional:TOP21@1")
    assert auto["bucket"] == "1h"
    assert auto["series"] == expected["series"]
    for bucket in ("1m", "5m"):
        with pytest.raises(Unrepresentable):
            query(db, T0, T0 + HOUR, bucket, "optional:TOP21@1")


@pytest.mark.parametrize("day,hours", [(date(2027, 3, 28), 23), (date(2027, 10, 31), 25)])
def test_warsaw_optional_daily_counts_use_actual_dst_hours(mariadb, day, hours):
    db = mariadb
    db.ensure_schema()
    start = local_midnight(day)
    end = local_midnight(date.fromordinal(day.toordinal() + 1))
    assert end - start == hours * HOUR
    select(db, ["TOP21"], start)
    persist(db, [pair(start + i * HOUR, TOP21=float(i)) for i in range(hours)])
    roll_all(db, end)
    result = query(db, start, end, "1d", "optional:TOP21@1")
    assert len(result["buckets"]) == 1
    assert result["buckets"][0]["expected_minutes"] == hours * 60
    assert result["series"]["optional:TOP21@1"]["selected_minutes"] == [hours]
    assert result["series"]["optional:TOP21@1"]["known_minutes"] == [hours]


def test_exact_partial_edges_use_raw_and_purged_edge_refuses(mariadb):
    db = mariadb
    db.ensure_schema()
    select(db, ["TOP21"], T0)
    persist(db, [pair(T0 + i * 60, TOP21=float(i)) for i in range(4 * 60)])
    roll_all(db, T0 + 4 * HOUR)
    start, end = T0 + 17 * 60, T0 + 2 * HOUR + 43 * 60
    result = query(db, start, end, "total", "optional:TOP21@1")
    assert result["series"]["optional:TOP21@1"]["selected_minutes"] == [146]
    assert result["buckets"][0]["start"] == "2027-01-15T08:17:00Z"
    assert purge_step(db, T0 + 100 * HOUR, 1, None, 1)[1] == 60
    with pytest.raises(Unrepresentable):
        query(db, start, end, "total", "optional:TOP21@1")
