"""HTTP contract: health, status facts, 1m history and error codes."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import T0, FakeStorage
from pompa.api import create_app
from pompa.catalog import RECORDED_KEYS
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder

Z = "2027-01-15T08:{:02d}:00Z"  # T0 + n minutes


def full(**values):
    return {k: values.get(k) for k in RECORDED_KEYS}


@pytest.fixture
def db():
    storage = FakeStorage()
    storage.rows = {
        T0: full(main_outlet_temp=35.0, co_power_consumption=0.0, operating_mode=4.0),
        # T0 + 60: no row (minute not recorded)
        T0 + 120: full(main_outlet_temp=None, co_power_consumption=900.0, operating_mode=3.0),
    }
    return storage


@pytest.fixture
def client(db):
    ingest = Ingest(600)
    recorder = Recorder(ingest, MinuteAccumulator(ingest, T0), db, 60)
    return TestClient(create_app(recorder, db, clock=lambda: T0 + 150))


def get_history(client, **params):
    return client.get("/api/v1/history", params=params)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_status_facts(client):
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"now", "mqtt", "recorder", "database", "sources"}
    assert body["now"] == "2027-01-15T08:02:30Z"
    assert body["database"] == {"available": True, "error": None,
                                "oldest_minute": Z.format(0), "newest_minute": Z.format(2),
                                "rolled_until": None, "raw_floor": None}
    assert body["mqtt"]["connected"] is False and body["mqtt"]["alive"] is False
    assert body["mqtt"]["stale_after_seconds"] == 600
    rec = body["recorder"]
    assert (rec["protected_rows"], rec["waiting_rows"], rec["dropped_rows"]) == (0, 0, 0)
    assert rec["waiting_capacity"] == 60 and rec["flush_in_progress"] is False
    assert rec["retention_1m_days"] == 365
    assert rec["rollup"] == {"last_rolled_hour": None, "last_rolled_at": None, "error": None, "error_at": None}
    assert rec["purge"] == {"last_run_at": None, "last_cutoff": None, "last_deleted_rows": 0,
                            "deleted_rows": 0, "error": None, "error_at": None}
    assert len(body["sources"]) == 25
    # Facts only: no verdict tokens anywhere.
    for word in ("healthy", "degraded", "ready", "partial", "ok"):
        assert f'"{word}"' not in r.text


def test_status_reports_database_unavailable(client, db):
    db.available = False
    body = client.get("/api/v1/status").json()
    assert body["database"] == {"available": False, "error": "fake outage",
                                "oldest_minute": None, "newest_minute": None, "rolled_until": None,
                                "raw_floor": None}


def test_history_exact_1m(client):
    r = get_history(client, **{"from": Z.format(0), "to": Z.format(3), "bucket": "1m",
                               "series": "main_outlet_temp,co_power_consumption,operating_mode"})
    assert r.status_code == 200
    body = r.json()
    assert (body["from"], body["to"], body["bucket"]) == (Z.format(0), Z.format(3), "1m")
    assert [b["start"] for b in body["buckets"]] == [Z.format(0), Z.format(1), Z.format(2)]
    assert [b["end"] for b in body["buckets"]] == [Z.format(1), Z.format(2), Z.format(3)]
    assert [b["recorded_minutes"] for b in body["buckets"]] == [1, 0, 1]
    # Clock at T0+150: the third minute has not elapsed yet.
    assert [b["expected_minutes"] for b in body["buckets"]] == [1, 1, 0]
    assert [b["coverage_percent"] for b in body["buckets"]] == [100.0, 0.0, None]

    outlet = body["series"]["main_outlet_temp"]
    assert outlet["kind"] == "mean" and outlet["unit"] == "°C"
    # Minute 1 has no row, minute 2 has a row with NULL: both null values,
    # told apart only by recorded_minutes.
    assert outlet["avg"] == [35.0, None, None]
    assert outlet["min"] == outlet["max"] == [35.0, None, None]
    assert outlet["minutes"] == [1, 0, 0]

    power = body["series"]["co_power_consumption"]
    assert power["avg"] == [0.0, None, 900.0]  # real zero stays zero
    assert power["minutes"] == [1, 0, 1]

    mode = body["series"]["operating_mode"]
    assert mode["kind"] == "last" and "avg" not in mode
    assert mode["last"] == [4.0, None, 3.0]


def test_history_defaults_to_all_recorded_series(client):
    body = get_history(client, **{"from": Z.format(0), "to": Z.format(1)}).json()
    assert list(body["series"]) == list(RECORDED_KEYS) + ["cop_co", "cop_dhw", "cop_total"]
    assert (body["bucket"], body["requested_bucket"]) == ("1m", "auto")


def test_history_accepts_explicit_offsets(client):
    body = get_history(client, **{"from": "2027-01-15T09:00:00+01:00", "to": "2027-01-15T10:01:00+02:00",
                                  "series": "operating_mode"}).json()
    assert body["from"] == Z.format(0) and body["to"] == Z.format(1)
    assert body["series"]["operating_mode"]["last"] == [4.0]


def test_history_empty_range_is_structural(client):
    body = get_history(client, **{"from": "2027-01-15T07:00:00Z", "to": "2027-01-15T07:02:00Z",
                                  "series": "outside_temp"}).json()
    assert [b["recorded_minutes"] for b in body["buckets"]] == [0, 0]
    assert body["series"]["outside_temp"]["avg"] == [None, None]


@pytest.mark.parametrize("params", [
    {},
    {"from": Z.format(0)},
    {"to": Z.format(1)},
    {"from": "yesterday", "to": Z.format(1)},
    {"from": "2027-01-15T08:00:00", "to": Z.format(1)},  # naive
    {"from": "2027-1-15", "to": "2027-01-16"},  # not a calendar date
    {"from": "2027-02-30", "to": "2027-03-01"},  # no such day
    {"from": "2027-01-15T08:00:00.5Z", "to": Z.format(1)},  # sub-second
    {"from": Z.format(1), "to": Z.format(1)},
    {"from": Z.format(2), "to": Z.format(1)},
    {"from": "2027-01-15T08:00:30Z", "to": Z.format(1)},  # not minute-aligned
    {"from": Z.format(0), "to": Z.format(1), "bucket": "7m"},
    {"from": Z.format(0), "to": Z.format(1), "series": "nope"},
    {"from": Z.format(0), "to": Z.format(1), "series": ","},
    {"from": Z.format(0), "to": Z.format(1), "series": "outside_temp,outside_temp"},
])
def test_history_bad_parameters_400(client, params):
    r = get_history(client, **params)
    assert r.status_code == 400, r.text


def test_history_bucket_limit_is_not_truncated(client):
    ok = get_history(client, **{"from": "2027-01-13T06:00:00Z", "to": "2027-01-15T08:00:00Z",
                                "bucket": "1m", "series": "outside_temp"})
    assert ok.status_code == 200 and len(ok.json()["buckets"]) == 3000
    too_many = get_history(client, **{"from": "2027-01-13T05:59:00Z", "to": "2027-01-15T08:00:00Z",
                                      "bucket": "1m"})
    assert too_many.status_code == 422 and "3001" in too_many.json()["detail"]


def test_history_outside_recordable_range_422(client):
    r = get_history(client, **{"from": "1969-12-31T23:00:00Z", "to": Z.format(1)})
    assert r.status_code == 422


def test_history_database_unavailable_503(client, db):
    db.available = False
    r = get_history(client, **{"from": Z.format(0), "to": Z.format(1)})
    assert r.status_code == 503
    assert client.get("/health").status_code == 200  # process liveness unaffected


def test_history_accepts_calendar_dates_as_local_midnight(client):
    body = get_history(client, **{"from": "2027-01-15", "to": "2027-01-16", "bucket": "1d",
                                  "series": "outside_temp"}).json()
    assert (body["from"], body["to"]) == ("2027-01-14T23:00:00Z", "2027-01-15T23:00:00Z")
    assert [(b["start"], b["end"]) for b in body["buckets"]] == [("2027-01-14T23:00:00Z", "2027-01-15T23:00:00Z")]
    assert body["buckets"][0]["expected_minutes"] == 542  # elapsed minutes only: the clock is at 08:02:30Z that day


@pytest.mark.parametrize("day,edges,expected", [
    ("2027-03-28", ("2027-03-27T23:00:00Z", "2027-03-28T22:00:00Z"), 1380),
    ("2027-10-31", ("2027-10-30T22:00:00Z", "2027-10-31T23:00:00Z"), 1500),
    ("2027-01-15", ("2027-01-14T23:00:00Z", "2027-01-15T23:00:00Z"), 1440),
])
def test_history_dst_days_through_the_api(db, day, edges, expected):
    later = TestClient(create_app(Recorder(Ingest(600), MinuteAccumulator(Ingest(600), T0), db, 60), db,
                                  clock=lambda: 1_900_000_000))  # 2030, long after every day under test
    body = later.get("/api/v1/history", params={
        "from": day, "to": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
        "bucket": "1d", "series": "outside_temp"}).json()
    assert [(b["start"], b["end"]) for b in body["buckets"]] == [edges]
    assert body["buckets"][0]["expected_minutes"] == expected


def test_history_cop_series_shape(client):
    body = get_history(client, **{"from": Z.format(0), "to": Z.format(3), "bucket": "total",
                                  "series": "cop_co"}).json()
    cop = body["series"]["cop_co"]
    assert set(cop) == {"label", "unit", "kind", "cop", "paired_minutes", "input_kwh", "output_kwh"}
    assert cop["kind"] == "cop" and cop["paired_minutes"] == [0] and cop["cop"] == [None]
