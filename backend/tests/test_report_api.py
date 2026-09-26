"""Stage 4D-C-B public calendar forms, extraction integration and fail-closed errors."""

import pytest

from conftest import Api, FakeStorage, persist_canonical, recorded, row
from pompa import activity_history, report, report_read
from pompa.activity import ActivityRecordInvalid
from pompa.storage import StorageUnavailable
from test_activity_api import pre_4c_purge, put
from test_activity_durable import CO, DHW, OFF, UNKNOWN, defrost, purge_all, roll_all
from test_report import equal_energy, input_from_rows, literal_energy

M, H = 60, 3600


@pytest.mark.parametrize("kind,date,hours,buckets,first,last", [
    ("day", "2026-01-15", 24, 24, "2026-01-15", "2026-01-16"),
    ("day", "2026-03-29", 23, 23, "2026-03-29", "2026-03-30"),
    ("day", "2026-10-25", 25, 25, "2026-10-25", "2026-10-26"),
    ("week", "2026-03-29", 167, 7, "2026-03-23", "2026-03-30"),
    ("week", "2026-10-25", 169, 7, "2026-10-19", "2026-10-26"),
    ("week", "2027-01-01", 168, 7, "2026-12-28", "2027-01-04"),
    ("month", "2026-02-14", 672, 28, "2026-02-01", "2026-03-01"),
    ("month", "2028-02-14", 696, 29, "2028-02-01", "2028-03-01"),
    ("month", "2026-03-14", 743, 31, "2026-03-01", "2026-04-01"),
    ("month", "2026-10-14", 745, 31, "2026-10-01", "2026-11-01"),
])
def test_calendar_forms_through_http(kind, date, hours, buckets, first, last):
    period = report.resolve_period(kind, date=date)
    api = Api(start=period.end)
    body = api.body("/api/v1/report", period.end, period=kind, date=date)
    assert body["period"]["from_date"] == first
    assert body["period"]["to_date"] == last
    assert len(body["buckets"]) == buckets
    coverage = body["totals"]["coverage"]
    assert coverage["calendar_minutes"] == coverage["settled_minutes"] == coverage["gap_minutes"] == hours * 60
    assert coverage["recorded_minutes"] == 0
    assert body["totals"]["energy"]["consumption"]["observed_kwh"] is None
    assert body["totals"]["events"]["observed_starts"] == 0
    assert api.storage.sessions == 1


@pytest.mark.parametrize("end,days", [("2026-10-02", 1), ("2026-11-01", 31)])
def test_custom_http(end, days):
    body = Api(start=2_000_000_000).body("/api/v1/report", period="custom",
                                      **{"from": "2026-10-01", "to": end})
    assert len(body["buckets"]) == days
    assert body["period"]["kind"] == "custom"


@pytest.mark.parametrize("date", ["1969-12-31", "1970-01-01", "2101-01-01"])
def test_empty_history_has_no_installation_or_epoch_policy(any_storage, date):
    period = report.resolve_period("day", date=date)
    body = Api(storage=any_storage, start=period.end).body("/api/v1/report", period="day", date=date)
    coverage = body["totals"]["coverage"]
    assert coverage["recorded_minutes"] == 0
    assert coverage["gap_minutes"] == coverage["settled_minutes"] == 1440
    assert body["totals"]["technical"]["outside_temp"]["avg"] is None
    assert body["totals"]["compressor_runs_overlapping"] == 0


def test_fully_future_report_keeps_db_dependency_and_all_buckets():
    period = report.resolve_period("day", date="2027-01-15")
    api = Api(start=period.start - H)
    body = api.body("/api/v1/report", period="day", date="2027-01-15")
    assert len(body["buckets"]) == 24
    assert body["evidence"] == {"from": None, "to": None}
    assert body["totals"]["coverage"] == {
        "calendar_minutes": 1440, "settled_minutes": 0, "unsettled_minutes": 0,
        "future_minutes": 1440, "recorded_minutes": 0, "gap_minutes": 0, "coverage_percent": None}
    assert api.storage.sessions == 1
    api.storage.available = False
    assert api.get("/api/v1/report", period="day", date="2027-01-15").status_code == 503


@pytest.mark.parametrize("query", [
    "", "date=2027-01-15", "period=year&date=2027-01-15", "period=day",
    "period=day&date=2027-01-15&from=2027-01-15", "period=day&date=2027-01-15&to=2027-01-16",
    "period=day&period=month&date=2027-01-15", "period=day&date=2027-01-15&date=2027-01-16",
    "period=day&date=2027-01-15&foo=bar", "period=day&date=2027-01-15T00:00:00Z",
    "period=day&date=2027-02-30", "period=day&date=20270115", "period=day&date=",
    "period=custom&from=2027-01-15", "period=custom&from=2027-01-15&to=2027-01-16&date=2027-01-15",
    "period=custom&from=2027-01-15&to=2027-01-15", "period=custom&from=2027-01-16&to=2027-01-15",
    "period=custom&from=2027-01-01&to=2027-02-02",
    "period=custom&from=2027-01-01&from=2027-01-02&to=2027-01-03",
    "period=custom&from=2027-01-01&to=2027-01-03&to=2027-01-04",
])
def test_invalid_forms_fail_before_frontier_or_storage(query, monkeypatch):
    api = Api()
    def forbidden(_clock):
        raise AssertionError("invalid request sampled the recorder")
    monkeypatch.setattr(api.recorder, "settled_before", forbidden)
    response = api.client.get(f"/api/v1/report?{query}")
    assert response.status_code == 400
    assert "detail" in response.json()
    assert api.storage.sessions == 0


def test_unrepresentable_calendar_is_422():
    api = Api()
    response = api.get("/api/v1/report", period="day", date="9999-12-31")
    assert response.status_code == 422
    assert api.storage.sessions == 0


@pytest.mark.parametrize("error,status", [
    (report.ReportUnrepresentable("calendar"), 422),
    (activity_history.ActivityUnavailable(0), 422),
    (report.ReportActivityUnavailable("lost activity"), 422),
    (ActivityRecordInvalid("bad segment"), 500),
    (report.ReportInvariantError("technical identity"), 500),
    (report_read.ReportHistoryInconsistent("hour mismatch"), 500),
    (StorageUnavailable("database"), 503),
])
def test_internal_error_mapping(monkeypatch, error, status):
    def fail(*args):
        raise error
    monkeypatch.setattr(report_read, "query", fail)
    response = Api().get("/api/v1/report", period="day", date="2027-01-15")
    assert response.status_code == status
    assert set(response.json()) == {"detail"}


def test_internal_value_error_is_not_request_validation(monkeypatch):
    def fail(*args):
        raise ValueError("internal bug")
    monkeypatch.setattr(report_read, "query", fail)
    with pytest.raises(ValueError, match="internal bug"):
        Api().get("/api/v1/report", period="day", date="2027-01-15")


def test_real_extracted_facts_reach_composer_and_survive_roll_and_purge(any_storage):
    period = report.resolve_period("day", date="2027-01-15")
    shapes = [OFF, CO, defrost(0.5), DHW, OFF, None, UNKNOWN,
              {**CO, "co_power_production": None, "outside_temp": None}]
    put(any_storage, period.start + H, shapes, roll=False)
    api = Api(storage=any_storage, start=period.end)
    body = api.body("/api/v1/report", period="day", date="2027-01-15")
    with any_storage.session() as session:
        rows = session.read_minutes(period.start, period.end)
    expected = report.compose_report(input_from_rows(period, rows, now=period.end,
                                     closed_until=period.end, evidence_start=period.start - H,
                                     evidence_end=period.end))
    assert body == expected
    equal_energy(body["totals"]["energy"], literal_energy(rows))
    assert body["totals"]["coverage"]["recorded_minutes"] == 7
    assert body["totals"]["activity"]["heating"]["minutes"] == 3
    assert body["totals"]["events"]["observed_starts"] == 1
    assert body["totals"]["events"]["observed_stops"] == 1
    assert body["totals"]["events"]["defrost_events"] == 1
    assert body["totals"]["events"]["observed_defrost_seconds"] == 30
    assert body["totals"]["energy"]["channels"]["dhw_power_production"]["minutes"] == 6
    roll_all(any_storage)
    assert api.body("/api/v1/report", period="day", date="2027-01-15") == body
    put(any_storage, period.end + 3 * H, [OFF])  # advance beyond the raw reprocessing margin
    assert purge_all(any_storage) == 7
    assert api.body("/api/v1/report", period="day", date="2027-01-15") == body


@pytest.mark.parametrize("corrupt", [False, True])
def test_unavailable_and_corrupt_durable_refuse_whole_report(any_storage, corrupt):
    period = report.resolve_period("day", date="2027-01-15")
    put(any_storage, period.start + H, [CO, OFF])
    if corrupt:
        with any_storage.session() as session:
            records = session.read_activity_segments(period.start + H, period.start + 2 * H)
            session.replace_activity_hour(period.start + H,
                                          [(*records[0][:2], 2, *records[0][3:]), *records[1:]])
    else:
        pre_4c_purge(any_storage, period.start + H)
    response = Api(storage=any_storage, start=period.end).get(
        "/api/v1/report", period="day", date="2027-01-15")
    assert response.status_code == (500 if corrupt else 422)
    assert set(response.json()) == {"detail"}


@pytest.mark.parametrize("queue", ["_waiting", "_protected"])
def test_unacknowledged_tail_and_partial_current_minute_are_not_gaps(queue):
    period = report.resolve_period("day", date="2027-01-15")
    edge = period.start + H
    storage = FakeStorage()
    put(storage, edge, [CO] * 8, roll=False)  # even physically committed tail rows remain unsettled
    api = Api(storage=storage, start=edge + 5 * M)
    getattr(api.recorder, queue).append(recorded(row(edge + 2 * M, **CO)))
    body = api.body("/api/v1/report", edge + 5 * M + 17, period="day", date="2027-01-15")
    cov = body["buckets"][1]["coverage"]
    assert cov == {"calendar_minutes": 60, "settled_minutes": 2, "unsettled_minutes": 3,
                   "future_minutes": 55, "recorded_minutes": 2, "gap_minutes": 0,
                   "coverage_percent": 100.0}
    assert body["totals"]["energy"]["cop"]["total"]["paired_minutes"] == 2
