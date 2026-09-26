"""Stage 4D-D-A representation sequence and reproducible CT112 query-scale evidence.

Run the MariaDB measurements with ``pytest -s`` to see scale, SQL and request time.
Elapsed time is evidence only; there is no timing SLA/assertion.
"""

import json
import time
from collections import Counter

import pytest
from pymysql.connections import Connection
from pymysql.constants import COMMAND

from conftest import Api, row
from pompa import report
from pompa.recorder import backfill_activity_step, rebuild_hour
from pompa.storage import Session
from test_activity_api import put
from test_activity_durable import (CO, DHW, OFF, SCENARIO, delete_activity,
                                   defrost, purge_all, roll_all)

M, H = 60, 3600


def test_report_bytes_equal_raw_rolled_backfilled_and_purged(any_storage):
    period = report.resolve_period("day", date="2027-01-15")
    shapes = [*SCENARIO, {**CO, "three_way_valve": 0.5},
              {**CO, "co_power_production": None}, OFF]
    shapes = [None if shape is None else
              {**shape, "outside_temp": (None, 0.0, -5.5, 9.25)[i % 4],
               "compressor_freq": shape["compressor_freq"] if i % 17 else None}
              for i, shape in enumerate(shapes)]
    put(any_storage, period.start + H, shapes, roll=False)
    api = Api(storage=any_storage, start=period.end)

    def response():
        result = api.get("/api/v1/report", period="day", date="2027-01-15")
        assert result.status_code == 200, result.text
        return result

    raw = response()
    facts = raw.json()["totals"]
    assert all(item["minutes"] > 0 for item in facts["activity"]["classes"].values())
    assert facts["technical"]["outside_temp"]["min"] == -5.5
    assert facts["technical"]["outside_temp"]["max"] == 9.25
    assert facts["energy"]["channels"]["co_power_production"]["unknown_minutes"] > 0
    assert facts["energy"]["cop"]["total"]["paired_minutes"] > 0
    assert facts["events"]["observed_starts"] > 0
    assert facts["events"]["defrost_events"] > 0
    roll_all(any_storage)
    assert response().content == raw.content
    # Model a legitimate pre-4C rolled hour awaiting activity backfill. Raw stays intact.
    delete_activity(any_storage, period.start, period.end)
    assert response().content == raw.content  # rolled history + raw activity
    _, hours, done = backfill_activity_step(any_storage, period.start, 24)
    assert len(hours) == 3 and done
    assert response().content == raw.content  # same history + durable activity
    put(any_storage, period.end + 3 * H, [OFF])  # real purge's two-hour safety margin
    assert purge_all(any_storage) == sum(shape is not None for shape in shapes)
    assert response().content == raw.content


@pytest.mark.parametrize("flapping", [False, True], ids=["realistic-745h", "flapping-7d"])
def test_mariadb_max_report_query_scale(mariadb, monkeypatch, flapping):
    mariadb.ensure_schema()
    period = report.resolve_period("custom", from_date="2026-10-01", to_date="2026-11-01")
    assert (period.end - period.start) // H == 745
    closed = period.end - 30 * M
    edge = period.end - H
    hours = 7 * 24 if flapping else 744
    rows = []
    for h in range(hours):
        for i in range(60):
            if flapping:
                shape = OFF if i % 2 == 0 else CO
            else:
                shape = (OFF if h % 25 == 0 else DHW if h % 25 == 24 else CO)
                if h % 24 == 12 and 20 <= i < 25:
                    shape = defrost(0.5)
            rows.append(row(period.start + h * H + i * M, **shape,
                            outside_temp=-4.0 + (h % 16) * 0.5))
    rows.extend(row(edge + i * M, **CO, outside_temp=0.0) for i in range(60))
    with mariadb.session() as session:
        session.upsert_minutes(rows)
        for h in range(hours):
            rebuild_hour(session, period.start + h * H)
    with mariadb.session() as session:
        selected = len(session.read_rollup(period.start, edge,
                       ("recorded", *report.POWER_CHANNELS,
                        *(key for pair in report.PAIRS.values() for key in pair),
                        "outside_temp", "compressor_freq")))
        segments = len(session.read_activity_segments(period.start, edge))
    statements, ranges = [], Counter()
    original_execute = Connection._execute_command

    def execute(self, command, sql):
        if command == COMMAND.COM_QUERY:
            statements.append(sql.decode(self.encoding) if isinstance(sql, bytes) else sql)
        return original_execute(self, command, sql)

    for name in ("read_minutes", "read_rollup", "read_activity_segments", "first_purged_hour"):
        original = getattr(Session, name)
        def read(self, *args, _name=name, _original=original, **kwargs):
            ranges[_name] += 1
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(Session, name, read)
    monkeypatch.setattr(Connection, "_execute_command", execute)
    api = Api(storage=mariadb, start=closed)
    begin = time.perf_counter()
    response = api.get("/api/v1/report", closed + 17, period="custom",
                       **{"from": "2026-10-01", "to": "2026-11-01"})
    elapsed = time.perf_counter() - begin
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["buckets"]) == 31
    assert body["totals"]["coverage"]["recorded_minutes"] == hours * 60 + 30
    assert sum(q == "START TRANSACTION WITH CONSISTENT SNAPSHOT" for q in statements) == 1
    # Count SQL sent on the wire, including charset/autocommit/isolation setup and COMMIT.
    assert len(statements) <= 15
    assert sum(q.startswith("SELECT ") for q in statements) == 9
    assert ranges["read_minutes"] <= 3
    assert ranges["read_rollup"] == 2
    assert ranges["read_activity_segments"] == 1
    assert ranges["first_purged_hour"] == 2
    print("REPORT_SCALE " + json.dumps({
        "scenario": "flapping-7d" if flapping else "realistic-745h", "local_days": 31,
        "utc_hours": 745, "raw_rows": len(rows), "settled_recorded_minutes": hours * 60 + 30,
        "selected_rollup_rows": selected, "durable_segments": segments,
        "sql_statements": len(statements), "select_statements": 9, "range_reads": dict(ranges),
        "request_seconds": round(elapsed, 6)}))
