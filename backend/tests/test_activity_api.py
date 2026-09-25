"""Stage 4C-C: activity read model, evidence loading and ``/api/v1/activity`` (+ ``/live``).

``any_storage`` tests run on FakeStorage and, with ``POMPA_TEST_DB_HOST``, real MariaDB.
Expected values are literals or independent raw references, never the serializer under test.
"""

from datetime import date, datetime, timezone
import json

import pytest

import conftest
from conftest import IDLE as IDLE_SNAPSHOT, RUNNING, T0, Api, persist_canonical
from pompa import activity_history
from pompa.activity import (
    ENERGY_SERIES, ActivityRecordInvalid, build_segments, compressor_runs, decode_segment, segment_record,
    timeline,
)
from pompa.activity_history import ActivityUnavailable
from pompa.aggregation import cop, energy_kwh, fold_minutes
from pompa.minute import iso_utc
from pompa.recorder import backfill_activity_step, roll_next_hour
from pompa.timegrid import Unrepresentable, local_midnight
from test_activity_durable import (
    CO, DHW, END, GAP, IDLE, NOW, OFF, SCENARIO, TAIL, UNKNOWN, defrost, delete_activity, purge_all,
    _variants, raw, roll_all, rows_of,
)

M, H, DAY = 60, 3600, 86400


def put(storage, start, shapes, *, roll=True):
    persist_canonical(storage, rows_of(start, shapes))
    if roll:
        roll_all(storage)


def q(storage, a, b, now=NOW):
    return activity_history.query(storage, a, b, now)


def test_loaded_timeline_uses_caller_session_and_matches_whole_activity_response(
        any_storage, monkeypatch):
    put(any_storage, T0, SCENARIO + TAIL)
    start, end = RANGES[0]
    expected = q(any_storage, start, end)
    original_session = any_storage.session
    with original_session() as session:
        def nested_session():
            raise AssertionError("timeline extraction opened a nested storage session")

        monkeypatch.setattr(any_storage, "session", nested_session)
        loaded = activity_history.load_timeline(session, start, end, NOW, left_floor=0)
        actual = activity_history._response(loaded.timeline, start, end, NOW, NOW,
                                            loaded.evidence_from, loaded.evidence_to)
        assert actual == expected
        assert json.dumps(actual, ensure_ascii=False, separators=(",", ":")) == json.dumps(
            expected, ensure_ascii=False, separators=(",", ":"))
        assert session.read_minutes(start, start + M)


def test_generic_timeline_can_cover_pre_unix_gaps_without_changing_activity_clamp():
    storage = conftest.FakeStorage()
    with storage.session() as session:
        generic = activity_history.load_timeline(session, -H, 0, H)
        public_window = activity_history.load_timeline(session, -H, 0, H, left_floor=0)
    assert generic.evidence_from == -2 * H
    assert generic.timeline.start == -2 * H
    assert generic.timeline.items[0].start == -2 * H
    assert public_window.evidence_from == 0


def runs(body):
    return [(r["start"], r["end"], r["start_boundary"], r["end_boundary"]) for r in body["compressor_runs"]]


def z(t):
    return iso_utc(t)


def pre_4c_purge(storage, hour_ts):
    """Make one rolled hour look purged before 4C-B: rollup kept, raw and segments gone."""
    delete_activity(storage, hour_ts, hour_ts + H)
    if isinstance(storage, conftest.FakeStorage):
        for t in [t for t in storage.rows if hour_ts <= t < hour_ts + H]:
            del storage.rows[t]
        return
    with storage._connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sample_1m WHERE ts >= %s AND ts < %s", (hour_ts, hour_ts + H))
        conn.commit()


# ------------------------------------------------------------------ read source

RANGES = [(T0 + 3 * M, T0 + H + M), (T0, T0 + 2 * H), (T0 + 57 * M, T0 + H + 2 * M),
          (T0 + 20 * M, T0 + 22 * M), (T0 + 30 * M, T0 + 32 * M), (T0 + H, T0 + H + M)]


def test_raw_mixed_durable_pending_and_purged_sources_give_identical_responses(any_storage):
    put(any_storage, T0, SCENARIO + TAIL, roll=False)  # raw only: nothing rolled
    reference = [q(any_storage, a, b) for a, b in RANGES]
    assert roll_next_hour(any_storage, END) == T0  # hour 0 durable, hour 1+ raw
    assert [q(any_storage, a, b) for a, b in RANGES] == reference
    roll_all(any_storage)
    assert [q(any_storage, a, b) for a, b in RANGES] == reference  # all durable
    delete_activity(any_storage)  # rolled, awaiting backfill: derived from raw again
    assert [q(any_storage, a, b) for a, b in RANGES] == reference
    while not backfill_activity_step(any_storage, 0, 24)[2]:
        pass
    assert purge_all(any_storage) > 0
    assert raw(any_storage, T0, T0 + 2 * H) == []
    assert [q(any_storage, a, b) for a, b in RANGES] == reference  # durable only, after purge


def test_durable_rows_are_the_source_even_when_raw_still_exists(any_storage):
    put(any_storage, T0, [CO] * 5 + [OFF] * 5)
    put(any_storage, T0 + H, [OFF])
    before = q(any_storage, T0, T0 + 10 * M)
    with any_storage.session() as s:  # raw changed behind the durable rows' back
        s.upsert_minutes(rows_of(T0 + 2 * M, [DHW]))
    assert q(any_storage, T0, T0 + 10 * M) == before


def test_corrupt_durable_rows_fail_closed_without_raw_fallback(any_storage):
    put(any_storage, T0, [CO] * 5 + [OFF] * 5)
    put(any_storage, T0 + H, [OFF])
    with any_storage.session() as s:
        records = s.read_activity_segments(T0, T0 + H)
        s.replace_activity_hour(T0, [(*records[0][:2], 2, *records[0][3:]), *records[1:]])
    with pytest.raises(ActivityRecordInvalid):
        q(any_storage, T0, T0 + 10 * M)
    api = Api(storage=any_storage, start=NOW)
    r = api.get("/api/v1/activity", t=NOW, **{"from": z(T0), "to": z(T0 + 10 * M)})
    assert r.status_code == 500 and "inconsistent" in r.json()["detail"]


def test_never_recorded_hour_is_a_gap_not_unavailable(any_storage):
    put(any_storage, T0, [CO] * 60)
    put(any_storage, T0 + 2 * H, [CO] * 60)  # hour 1 never recorded
    body = q(any_storage, T0, T0 + 3 * H)
    assert [(i["type"], i["start"], i["minutes"]) for i in body["timeline"]] == [
        ("activity", z(T0), 60), ("gap", z(T0 + H), 60), ("activity", z(T0 + 2 * H), 60)]
    # The loader widened into the unrecorded neighbours: both outer edges are proven gaps too.
    assert runs(body) == [(z(T0), z(T0 + H), "gap", "gap"), (z(T0 + 2 * H), z(T0 + 3 * H), "gap", "gap")]
    assert body["summary"]["gap_minutes"] == 60


def test_unavailable_inside_the_range_is_refused(any_storage):
    put(any_storage, T0, [CO] * 60)
    put(any_storage, T0 + H, [CO] * 30 + [OFF] * 30)
    put(any_storage, T0 + 2 * H, [OFF])
    pre_4c_purge(any_storage, T0)
    with pytest.raises(ActivityUnavailable) as e:
        q(any_storage, T0 + 30 * M, T0 + H + 10 * M)
    assert e.value.hour_ts == T0
    r = Api(storage=any_storage, start=NOW).get("/api/v1/activity", t=NOW,
                                     **{"from": z(T0 + 59 * M), "to": z(T0 + H + M)})
    assert r.status_code == 422
    assert "purged before durable activity existed" in r.json()["detail"]
    assert "not a range without recorded data" in r.json()["detail"]


def test_unavailable_just_before_the_range_limits_start_proof(any_storage):
    put(any_storage, T0, [CO] * 60)
    put(any_storage, T0 + H, [CO] * 30 + [OFF] * 30)
    put(any_storage, T0 + 2 * H, [OFF])
    pre_4c_purge(any_storage, T0)
    body = q(any_storage, T0 + H, T0 + H + 10 * M)
    [run] = body["compressor_runs"]
    assert (run["start"], run["start_boundary"], run["start_observed"], run["starts_before_range"]) == (
        z(T0 + H), "unavailable", False, False)
    assert body["summary"]["observed_starts"] == 0
    assert [i["type"] for i in body["timeline"]] == ["activity"]  # nothing unavailable inside


def test_unavailable_just_after_the_range_limits_end_proof(any_storage):
    put(any_storage, T0, [OFF] * 30 + [CO] * 30)
    put(any_storage, T0 + H, [CO] * 60)
    put(any_storage, T0 + 2 * H, [OFF])
    pre_4c_purge(any_storage, T0 + H)  # not a real purge order: isolates the end-boundary fact
    body = q(any_storage, T0 + 40 * M, T0 + H)
    [run] = body["compressor_runs"]
    assert (run["start"], run["end"], run["start_observed"], run["end_boundary"], run["end_observed"]) == (
        z(T0 + 30 * M), z(T0 + H), True, "unavailable", False)
    assert body["summary"]["complete_runs"]["count"] == 0


def test_open_edge_and_unclosed_minutes(any_storage):
    put(any_storage, T0, [OFF] * 5 + [CO] * 5, roll=False)
    now = T0 + 10 * M + 30  # minute 10 has not closed
    body = q(any_storage, T0, T0 + H, now)
    assert body["closed_until"] == z(T0 + 10 * M)
    assert [(i["type"], i["start"], i["minutes"]) for i in body["timeline"]] == [
        ("activity", z(T0), 5), ("activity", z(T0 + 5 * M), 5), ("open", z(T0 + 10 * M), 50)]
    [run] = body["compressor_runs"]
    assert (run["end_boundary"], run["end_observed"], run["ends_after_range"]) == ("open", False, False)
    s = body["summary"]
    assert (s["closed_minutes"], s["recorded_minutes"], s["gap_minutes"]) == (10, 10, 0)
    later = q(any_storage, T0 + 20 * M, T0 + 30 * M, now)  # entirely unclosed
    assert [(i["type"], i["minutes"]) for i in later["timeline"]] == [("open", 10)]
    assert later["summary"]["closed_minutes"] == 0 and later["compressor_runs"] == []


# ------------------------------------------------------------------ evidence loading

def test_observed_start_and_stop_at_the_range_edges_come_from_adjacent_minutes(any_storage):
    put(any_storage, T0, [OFF] * 10 + [CO] * 10 + [OFF] * 10)
    put(any_storage, T0 + H, [OFF])
    body = q(any_storage, T0 + 10 * M, T0 + 20 * M)
    s = body["summary"]
    assert (s["observed_starts"], s["observed_stops"], s["complete_runs"]["minutes"]) == (1, 1, [10])
    [run] = body["compressor_runs"]
    assert (run["starts_before_range"], run["ends_after_range"]) == (False, False)


def test_long_run_is_one_full_observed_run_from_a_one_hour_query(any_storage):
    put(any_storage, T0, [OFF] * 30 + [CO] * (50 * 60) + [OFF] * 90)  # a 50-hour run
    raw_rows = raw(any_storage)
    body = q(any_storage, T0 + 24 * H, T0 + 25 * H)
    [run] = body["compressor_runs"]
    assert (run["start"], run["end"], run["minutes"]) == (z(T0 + 30 * M), z(T0 + 30 * M + 50 * H), 3000)
    assert (run["overlap_start"], run["overlap_end"], run["overlap_minutes"]) == (
        z(T0 + 24 * H), z(T0 + 25 * H), 60)
    assert (run["starts_before_range"], run["ends_after_range"], run["start_observed"], run["end_observed"]) == (
        True, True, True, True)
    own = [r for r in raw_rows if T0 + 30 * M <= r[0] < T0 + 30 * M + 50 * H]
    reference = fold_minutes(own, ENERGY_SERIES)
    assert run["energy"]["co_power_consumption"] == {
        "kwh": energy_kwh(reference["co_power_consumption"]), "minutes": 3000}  # full span, not prorated
    assert run["cop"]["co"] == cop(reference["pair_co_in"], reference["pair_co_out"])
    assert body["summary"]["observed_starts"] == 0 and body["summary"]["compressor_runs_overlapping"] == 1
    lo, hi = (datetime.fromisoformat(body["evidence"][k]).timestamp() for k in ("from", "to"))
    assert lo <= T0 and hi >= T0 + 51 * H  # widened exactly as far as the run required ...
    assert hi - lo <= 64 * H + 2 * H  # ... by bounded doubling, not the whole history


def test_decisive_neighbours_keep_evidence_at_the_minimal_window(any_storage):
    put(any_storage, T0, ([CO] * 30 + [OFF] * 30) * 24 * 5)  # five days of hourly cycles
    body = q(any_storage, T0 + 50 * H, T0 + 50 * H + 20 * M)  # spans decided inside the adjacent hour
    assert body["evidence"] == {"from": z(T0 + 49 * H), "to": z(T0 + 51 * H)}
    # A range whose touching spans reach the window edges widens once per side, no further.
    body = q(any_storage, T0 + 50 * H + 10 * M, T0 + 50 * H + 40 * M)
    assert body["evidence"] == {"from": z(T0 + 49 * H), "to": z(T0 + 52 * H)}


def test_response_is_independent_of_how_source_hours_are_stored(any_storage):
    shapes = [OFF] * 20 + [CO] * 200 + [UNKNOWN] + [DHW] * 30 + [OFF] * 49
    put(any_storage, T0, shapes, roll=False)
    reference = q(any_storage, T0 + 2 * H, T0 + 3 * H)
    for hour in range(5):  # roll hour by hour: every durable/raw partition in turn
        roll_next_hour(any_storage, END)
        assert q(any_storage, T0 + 2 * H, T0 + 3 * H) == reference


# ------------------------------------------------------------------ cycles

def test_cycle_matrix(any_storage):
    shapes = ([OFF, CO, CO, OFF, CO, CO, OFF]  # two runs separated by one OFF minute
              + [CO, DHW, DHW, CO, OFF]  # CO -> DHW keeps one run
              + [CO, defrost(1.0), defrost(0.5), CO, OFF]  # defrost inside a run does not split it
              + [UNKNOWN, CO, CO, UNKNOWN, OFF]  # unknown on both sides
              + [GAP, CO, CO, GAP, OFF]  # true gap on both sides
              + [{**CO, "compressor_freq": 0.2}, {**CO, "compressor_freq": 13.0}, OFF])  # short stops unseen
    put(any_storage, T0, shapes)
    put(any_storage, T0 + H, [OFF])
    body = q(any_storage, T0, T0 + len(shapes) * M)
    got = [((datetime.fromisoformat(r["start"]).timestamp() - T0) // M, r["minutes"], r["start_boundary"],
            r["end_boundary"]) for r in body["compressor_runs"]]
    assert got == [(1, 2, "observed", "observed"), (4, 2, "observed", "observed"),
                   (7, 4, "observed", "observed"), (12, 4, "observed", "observed"),
                   (18, 2, "unknown", "unknown"), (23, 2, "gap", "gap"), (27, 2, "observed", "observed")]
    assert body["compressor_runs"][2]["activity_minutes"] == {
        "off": 0, "idle": 0, "co": 2, "dhw": 2, "transition": 0, "defrost": 0, "unknown": 0}
    assert body["compressor_runs"][3]["observed_defrost_seconds"] == 90.0
    offs = [(o["minutes"], o["exact"]) for o in body["compressor_off_intervals"]]
    assert offs[:3] == [(1, False), (1, True), (1, True)]  # the first starts at the window edge
    s = body["summary"]
    assert s["observed_starts"] == 5 and s["observed_stops"] == 5
    assert s["complete_runs"] == {"count": 5, "minutes": [2, 2, 4, 4, 2], "total_minutes": 14,
                                  "min_minutes": 2, "max_minutes": 4, "mean_minutes": 2.8}


def test_per_run_energy_and_cop_equal_the_raw_reference(any_storage):
    put(any_storage, T0, SCENARIO + TAIL)
    raw_rows = raw(any_storage, T0, T0 + 2 * H)
    body = q(any_storage, T0, T0 + 2 * H)
    assert len(body["compressor_runs"]) == 4
    for run in body["compressor_runs"]:
        a, b = (datetime.fromisoformat(run[k]).timestamp() for k in ("start", "end"))
        reference = fold_minutes([r for r in raw_rows if a <= r[0] < b], ENERGY_SERIES)
        for channel in ENERGY_SERIES[:4]:
            assert run["energy"][channel] == {"kwh": energy_kwh(reference.get(channel)),
                                              "minutes": reference[channel].n if channel in reference else 0}
        for name, (i, o) in (("co", ("pair_co_in", "pair_co_out")), ("dhw", ("pair_dhw_in", "pair_dhw_out")),
                             ("total", ("pair_total_in", "pair_total_out"))):
            assert run["cop"][name] == cop(reference.get(i), reference.get(o))


def test_cross_hour_run_is_one_run_in_every_query(any_storage):
    put(any_storage, T0, [OFF] * 58 + [CO] * 5 + [OFF] * 57)
    for a, b in [(T0, T0 + H), (T0 + H, T0 + 2 * H), (T0 + 59 * M, T0 + H + M), (T0, T0 + 2 * H)]:
        [run] = q(any_storage, a, b)["compressor_runs"]
        assert (run["start"], run["end"], run["minutes"]) == (z(T0 + 58 * M), z(T0 + H + 3 * M), 5)


# ------------------------------------------------------------------ defrost

def test_defrost_facts(any_storage):
    shapes = ([CO] * 57 + [defrost(0.583333), defrost(1.0), defrost(1.0), defrost(0.166667)]  # across hour
              + [{**CO, "compressor_freq": None}]  # activity unknown, defrost signal 0: proven end
              + [CO, defrost(1.0), {**CO, "defrosting_state": None}]  # unknown defrost signal
              + [CO] * 10 + [OFF] * 50)
    put(any_storage, T0, shapes)
    body = q(any_storage, T0 + 58 * M, T0 + H + M)  # clips the first defrost on both sides
    first = body["defrosts"][0]
    assert (first["start"], first["end"], first["minutes"]) == (z(T0 + 57 * M), z(T0 + H + M), 4)
    assert (first["overlap_start"], first["overlap_end"], first["overlap_minutes"]) == (
        z(T0 + 58 * M), z(T0 + H + M), 3)
    assert (first["start_boundary"], first["end_boundary"]) == ("observed", "observed")
    assert first["observed_defrost_seconds"] == pytest.approx(165.0, abs=1e-3)
    assert first["overlap_observed_defrost_seconds"] == pytest.approx(130.00002, abs=1e-6)
    assert (first["starts_before_range"], first["ends_after_range"]) == (True, False)
    second = q(any_storage, T0 + H + 2 * M, T0 + H + 4 * M)["defrosts"][0]
    assert (second["start_boundary"], second["end_boundary"]) == ("observed", "unknown")
    put(any_storage, T0 + 2 * H, [OFF] * 180)
    before = q(any_storage, T0 + 58 * M, T0 + H + M)
    purge_all(any_storage)
    assert raw(any_storage, T0, T0 + 2 * H) == []
    assert q(any_storage, T0 + 58 * M, T0 + H + M) == before  # durable-only integrated seconds


# ------------------------------------------------------------------ midnight and DST

def test_run_and_defrost_across_warsaw_midnight_through_the_api(any_storage):
    midnight = local_midnight(date(2027, 1, 16))
    start = midnight - 5 * M
    put(any_storage, start, [OFF, OFF, CO, defrost(1.0), defrost(0.5), CO, CO, OFF] + [OFF] * 60)
    api = Api(storage=any_storage, start=NOW)
    day1 = api.body("/api/v1/activity", t=NOW, **{"from": "2027-01-15", "to": "2027-01-16"})
    day2 = api.body("/api/v1/activity", t=NOW, **{"from": "2027-01-16", "to": "2027-01-17"})
    both = api.body("/api/v1/activity", t=NOW, **{"from": "2027-01-15", "to": "2027-01-17"})
    for body in (day1, day2, both):
        [run] = body["compressor_runs"]
        assert (run["start"], run["end"], run["minutes"]) == (z(midnight - 3 * M), z(midnight + 2 * M), 5)
        assert body["summary"]["compressor_runs_overlapping"] == 1  # not additive across days
    assert day1["compressor_runs"][0]["ends_after_range"] and day2["compressor_runs"][0]["starts_before_range"]
    for body in (day1, both):  # the defrost ends exactly at midnight: it does not continue
        [frost] = body["defrosts"]
        assert (frost["start"], frost["end"], frost["ends_after_range"]) == (
            z(midnight - 2 * M), z(midnight), False)
    assert day2["defrosts"] == []
    assert (day1["summary"]["observed_starts"], day1["summary"]["observed_stops"]) == (1, 0)
    assert (day2["summary"]["observed_starts"], day2["summary"]["observed_stops"]) == (0, 1)
    assert both["summary"]["observed_starts"] == 1 and both["summary"]["observed_stops"] == 1


@pytest.mark.parametrize("day, minutes", [(date(2027, 3, 28), 1380), (date(2027, 10, 31), 1500)])
def test_dst_days_through_the_api(any_storage, day, minutes):
    start = local_midnight(day)
    switch = int(datetime(day.year, day.month, day.day, 1, tzinfo=timezone.utc).timestamp())
    count = minutes + 60
    put(any_storage, start, [CO if switch - 30 * M <= start + i * M < switch + 30 * M else OFF
                             for i in range(count)])
    body = Api(storage=any_storage, start=NOW).body("/api/v1/activity", t=NOW,
                                         **{"from": day.isoformat(), "to": date.fromordinal(day.toordinal() + 1).isoformat()})
    s = body["summary"]
    assert (s["closed_minutes"], s["recorded_minutes"], s["gap_minutes"]) == (minutes, minutes, 0)
    [run] = body["compressor_runs"]
    assert (run["start"], run["minutes"], run["start_observed"], run["end_observed"]) == (
        z(switch - 30 * M), 60, True, True)


# ------------------------------------------------------------------ API contract

def test_literal_response_contract(any_storage):
    put(any_storage, T0, [OFF, CO, CO, OFF])
    body = Api(storage=any_storage, start=T0 + H).body(
        "/api/v1/activity", t=T0 + H, **{"from": z(T0), "to": z(T0 + 4 * M)})
    zero_cop = {"cop": None, "paired_minutes": 1, "input_kwh": 0.0, "output_kwh": 0.0}
    run_energy = {"co_power_consumption": {"kwh": 1801.0 / 60000, "minutes": 2},
                  "co_power_production": {"kwh": 7200.5 / 60000, "minutes": 2},
                  "dhw_power_consumption": {"kwh": 0.0, "minutes": 2},
                  "dhw_power_production": {"kwh": 0.0, "minutes": 2}}
    run_cop = {"co": {"cop": 7200.5 / 1801.0, "paired_minutes": 2, "input_kwh": 1801.0 / 60000,
                      "output_kwh": 7200.5 / 60000},
               "dhw": {"cop": None, "paired_minutes": 2, "input_kwh": 0.0, "output_kwh": 0.0},
               "total": {"cop": 7200.5 / 1801.0, "paired_minutes": 2, "input_kwh": 1801.0 / 60000,
                         "output_kwh": 7200.5 / 60000}}
    off_energy = {k: {"kwh": 0.0, "minutes": 1} for k in (
        "co_power_consumption", "co_power_production", "dhw_power_consumption", "dhw_power_production")}
    assert body == {
        "from": "2027-01-15T08:00:00Z",
        "to": "2027-01-15T08:04:00Z",
        "now": "2027-01-15T09:00:00Z",
        "closed_until": "2027-01-15T09:00:00Z",
        "segment_rule_version": 1,
        "evidence": {"from": "2027-01-15T07:00:00Z", "to": "2027-01-15T09:00:00Z"},
        "summary": {
            "closed_minutes": 4, "recorded_minutes": 4, "gap_minutes": 0,
            "activity_minutes": {"off": 2, "idle": 0, "co": 2, "dhw": 0, "transition": 0, "defrost": 0,
                                 "unknown": 0},
            "compressor_minutes": {"off": 2, "on": 2, "unknown": 0},
            "observed_starts": 1, "observed_stops": 1,
            "compressor_runs_overlapping": 1, "defrosts_overlapping": 0,
            "complete_runs": {"count": 1, "minutes": [2], "total_minutes": 2, "min_minutes": 2,
                              "max_minutes": 2, "mean_minutes": 2.0},
            "exact_off_intervals": {"count": 0, "minutes": [], "total_minutes": 0, "min_minutes": None,
                                    "max_minutes": None, "mean_minutes": None},
            "observed_defrost_seconds": 0.0,
        },
        "timeline": [
            {"type": "activity", "activity": "off", "start": "2027-01-15T08:00:00Z",
             "end": "2027-01-15T08:01:00Z", "minutes": 1,
             "event": {"start": "2027-01-15T08:00:00Z", "end": "2027-01-15T08:01:00Z", "minutes": 1,
                       "overlap_start": "2027-01-15T08:00:00Z", "overlap_end": "2027-01-15T08:01:00Z",
                       "overlap_minutes": 1, "starts_before_range": False, "ends_after_range": False,
                       "start_boundary": "gap", "end_boundary": "observed", "start_observed": False,
                       "end_observed": True, "energy": off_energy,
                       "cop": {"co": zero_cop, "dhw": zero_cop, "total": zero_cop},
                       "observed_defrost_seconds": 0.0}},
            {"type": "activity", "activity": "co", "start": "2027-01-15T08:01:00Z",
             "end": "2027-01-15T08:03:00Z", "minutes": 2,
             "event": {"start": "2027-01-15T08:01:00Z", "end": "2027-01-15T08:03:00Z", "minutes": 2,
                       "overlap_start": "2027-01-15T08:01:00Z", "overlap_end": "2027-01-15T08:03:00Z",
                       "overlap_minutes": 2, "starts_before_range": False, "ends_after_range": False,
                       "start_boundary": "observed", "end_boundary": "observed", "start_observed": True,
                       "end_observed": True, "energy": run_energy, "cop": run_cop,
                       "observed_defrost_seconds": 0.0}},
            {"type": "activity", "activity": "off", "start": "2027-01-15T08:03:00Z",
             "end": "2027-01-15T08:04:00Z", "minutes": 1,
             "event": {"start": "2027-01-15T08:03:00Z", "end": "2027-01-15T08:04:00Z", "minutes": 1,
                       "overlap_start": "2027-01-15T08:03:00Z", "overlap_end": "2027-01-15T08:04:00Z",
                       "overlap_minutes": 1, "starts_before_range": False, "ends_after_range": False,
                       "start_boundary": "observed", "end_boundary": "gap", "start_observed": True,
                       "end_observed": False, "energy": off_energy,
                       "cop": {"co": zero_cop, "dhw": zero_cop, "total": zero_cop},
                       "observed_defrost_seconds": 0.0}},
        ],
        "compressor_runs": [
            {"start": "2027-01-15T08:01:00Z", "end": "2027-01-15T08:03:00Z", "minutes": 2,
             "overlap_start": "2027-01-15T08:01:00Z", "overlap_end": "2027-01-15T08:03:00Z",
             "overlap_minutes": 2, "starts_before_range": False, "ends_after_range": False,
             "start_boundary": "observed", "end_boundary": "observed", "start_observed": True,
             "end_observed": True, "energy": run_energy, "cop": run_cop,
             "activity_minutes": {"off": 0, "idle": 0, "co": 2, "dhw": 0, "transition": 0, "defrost": 0,
                                  "unknown": 0},
             "observed_defrost_seconds": 0.0},
        ],
        "compressor_off_intervals": [
            {"start": "2027-01-15T08:00:00Z", "end": "2027-01-15T08:01:00Z", "minutes": 1,
             "overlap_start": "2027-01-15T08:00:00Z", "overlap_end": "2027-01-15T08:01:00Z",
             "overlap_minutes": 1, "starts_before_range": False, "ends_after_range": False,
             "start_boundary": "gap", "end_boundary": "observed", "start_observed": False,
             "end_observed": True, "exact": False},
            {"start": "2027-01-15T08:03:00Z", "end": "2027-01-15T08:04:00Z", "minutes": 1,
             "overlap_start": "2027-01-15T08:03:00Z", "overlap_end": "2027-01-15T08:04:00Z",
             "overlap_minutes": 1, "starts_before_range": False, "ends_after_range": False,
             "start_boundary": "observed", "end_boundary": "gap", "start_observed": True,
             "end_observed": False, "exact": False},
        ],
        "defrosts": [],
    }


@pytest.mark.parametrize("params, fragment", [
    ({"to": "2027-01-15T09:00:00Z"}, "'from' is required"),
    ({"from": "2027-01-15T08:00:00Z"}, "'to' is required"),
    ({"from": "yesterday", "to": "2027-01-15T09:00:00Z"}, "ISO 8601"),
    ({"from": "2027-01-15T08:00:00", "to": "2027-01-15T09:00:00Z"}, "explicit UTC offset"),
    ({"from": "2027-01-15T08:00:30Z", "to": "2027-01-15T09:00:00Z"}, "whole minute"),
    ({"from": "2027-01-15T09:00:00Z", "to": "2027-01-15T09:00:00Z"}, "earlier than"),
    ({"from": "2027-01-15T10:00:00Z", "to": "2027-01-15T09:00:00Z"}, "earlier than"),
])
def test_malformed_requests_are_400(params, fragment):
    r = Api().get("/api/v1/activity", t=NOW, **params)
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_too_long_range_is_422_and_database_outage_is_503():
    api = Api()
    r = api.get("/api/v1/activity", t=NOW, **{"from": "2027-01-01", "to": "2027-02-02"})
    assert r.status_code == 422 and "at most 31 days" in r.json()["detail"]
    assert api.get("/api/v1/activity", t=NOW, **{"from": "2027-01-01", "to": "2027-02-01"}).status_code == 200
    api.storage.available = False
    r = api.get("/api/v1/activity", t=NOW, **{"from": "2027-01-01", "to": "2027-01-02"})
    assert r.status_code == 503 and "database unavailable" in r.json()["detail"]


def test_reads_never_write(any_storage, monkeypatch):
    put(any_storage, T0, SCENARIO + TAIL)
    delete_activity(any_storage, T0 + H, T0 + 2 * H)  # a hour awaiting backfill is read, not repaired
    before = raw(any_storage), activity_rows(any_storage)
    q(any_storage, T0, T0 + 2 * H)
    assert (raw(any_storage), activity_rows(any_storage)) == before


def activity_rows(storage):
    with storage.session() as s:
        return s.read_activity_segments(0, END), s.read_rollup(0, END, ["recorded"])


def test_raw_reference_timeline_matches_the_api(any_storage):
    """Independent reference: the pure domain over all raw minutes."""
    put(any_storage, T0, SCENARIO + TAIL)
    raw_rows = raw(any_storage)
    tl = timeline(build_segments(raw_rows), T0 - H, T0 + 5 * H, T0 + 5 * H)
    reference = [(z(r.start), z(r.end), r.start_boundary.value, r.end_boundary.value)
                 for r in compressor_runs(tl) if r.start < T0 + 2 * H and r.end > T0]
    assert runs(q(any_storage, T0, T0 + 2 * H)) == reference


def test_range_limit_constant():
    with pytest.raises(Unrepresentable):
        activity_history.query(conftest.FakeStorage(), T0, T0 + 31 * DAY + 2 * H, NOW)


# ------------------------------------------------------------------ live

def live(snapshot=RUNNING, *, retained=False, query_at=T0 + 30, drop=(), extra=None, disconnect=False,
         db=True):
    api = Api()
    api.storage.available = db
    payload = {k: v for k, v in {**snapshot, **(extra or {})}.items() if k not in drop}
    api.connect(T0)
    api.publish(T0, payload, retained=retained)
    if disconnect:
        api.disconnect(T0 + 10)
    return api.body("/api/v1/activity/live", t=query_at)


@pytest.mark.parametrize("snapshot, extra, drop, expected", [
    (IDLE_SNAPSHOT, None, (), ("off", "off")),
    (IDLE_SNAPSHOT, {"main/Heatpump_State": "1"}, (), ("idle", "off")),
    (RUNNING, None, (), ("co", "on")),
    (RUNNING, {"main/ThreeWay_Valve_State": "1"}, (), ("dhw", "on")),
    (RUNNING, {"extra/DHW_Power_Consumption_Extra": "1500"}, ("main/ThreeWay_Valve_State",),
     ("transition", "on")),
    (RUNNING, {"main/Defrosting_State": "1"}, (), ("defrost", "on")),
    (RUNNING, None, ("main/Compressor_Freq",), ("unknown", "unknown")),
    (RUNNING, None, ("main/Defrosting_State",), ("unknown", "on")),
])
def test_live_activity_classification(snapshot, extra, drop, expected):
    body = live(snapshot, extra=extra, drop=drop)
    assert (body["activity"], body["compressor"]) == expected
    assert body["all_inputs_live"] is (not drop)
    assert body["rule_version"] == 1 and body["now"] == "2027-01-15T08:00:30Z"


def test_live_activity_inputs_are_literal():
    body = live()
    assert body["inputs"]["compressor_freq"] == {"value": 40.0, "mode": "live",
                                                 "received_at": "2027-01-15T08:00:00Z", "used": True}
    assert body["inputs"]["dhw_power_consumption"] == {"value": 0.0, "mode": "live",
                                                       "received_at": "2027-01-15T08:00:00Z", "used": True}
    assert body["mqtt"] == {"connected": True, "alive": True, "epoch": 1}


def test_retained_stale_and_disconnected_inputs_are_unknown():
    retained = live(retained=True)
    assert (retained["activity"], retained["compressor"], retained["all_inputs_live"]) == (
        "unknown", "unknown", False)
    assert retained["inputs"]["compressor_freq"] == {"value": 40.0, "mode": "retained",
                                                     "received_at": "2027-01-15T08:00:00Z", "used": False}
    stale = live(query_at=T0 + 601)
    assert (stale["activity"], stale["compressor"]) == ("unknown", "unknown")
    assert stale["inputs"]["compressor_freq"]["mode"] == "none"
    gone = live(disconnect=True)
    assert (gone["activity"], gone["compressor"], gone["mqtt"]["connected"]) == ("unknown", "unknown", False)


def test_live_activity_does_not_depend_on_the_database():
    assert live(db=False) == live(db=True)


def test_partitioned_ranges_agree_with_the_whole_range():
    """Additive facts add up over any partition; every run keeps identical full-span facts."""
    import random

    rng = random.Random(5)

    def full_span(run):
        return {k: v for k, v in run.items()
                if not k.startswith("overlap") and k not in ("starts_before_range", "ends_after_range")}

    for trial in range(40):
        storage = conftest.FakeStorage()
        shapes = []
        while len(shapes) < 600:
            shapes += [rng.choice([CO, DHW, OFF, IDLE, UNKNOWN, defrost(0.5), GAP])] * rng.choice(
                [1, 1, 2, 5, 30, 90])
        persist_canonical(storage, rows_of(T0, shapes[:600]))
        for _ in range(rng.randrange(0, 11)):  # any durable/raw partition of the hours
            roll_next_hour(storage, END)
        whole = q(storage, T0, T0 + 600 * M)
        runs_by_span = {(r["start"], r["end"]): full_span(r) for r in whole["compressor_runs"]}
        edges = [0, *sorted(rng.sample(range(1, 600), 5)), 600]
        parts = [q(storage, T0 + a * M, T0 + b * M) for a, b in zip(edges, edges[1:])]
        for key in ("observed_starts", "observed_stops", "closed_minutes", "gap_minutes",
                    "observed_defrost_seconds"):
            assert sum(p["summary"][key] for p in parts) == whole["summary"][key], (trial, key)
        for part in parts:
            for run in part["compressor_runs"]:
                assert full_span(run) == runs_by_span[(run["start"], run["end"])], trial


# ------------------------------------------------------------------ durable read integrity (hardening)

HOUR0 = [CO] * 5 + [OFF] * 5 + [DHW] * 5  # three durable segments, 15 recorded minutes, then a gap


def durable_hour(storage, purged):
    put(storage, T0, HOUR0)
    put(storage, T0 + H, [OFF] * 180)
    if purged:
        assert purge_all(storage) > 0
        assert raw(storage, T0, T0 + H) == []


def replace_hour(storage, hour, records):
    with storage.session() as s:
        s.replace_activity_hour(hour, records)


def hour_records(storage, hour):
    with storage.session() as s:
        return s.read_activity_segments(hour, hour + H)


def assert_fails_closed(storage, a, b):
    with pytest.raises(ActivityRecordInvalid):
        q(storage, a, b)
    r = Api(storage=storage, start=NOW).get("/api/v1/activity", t=NOW, **{"from": z(a), "to": z(b)})
    assert r.status_code == 500 and "stored activity history is inconsistent" in r.json()["detail"]


@pytest.mark.parametrize("purged", [False, True], ids=["raw present", "after purge"])
@pytest.mark.parametrize("index", [0, 1, 2], ids=["first", "middle", "last"])
def test_deleted_durable_segment_fails_closed_never_a_gap(any_storage, purged, index):
    durable_hour(any_storage, purged)
    assert q(any_storage, T0, T0 + H)["summary"]["recorded_minutes"] == 15
    records = hour_records(any_storage, T0)
    replace_hour(any_storage, T0, records[:index] + records[index + 1:])
    assert_fails_closed(any_storage, T0, T0 + H)  # no raw fallback, no 5-minute "gap"


@pytest.mark.parametrize("purged", [False, True], ids=["raw present", "after purge"])
def test_forged_extra_durable_segment_fails_closed(any_storage, purged):
    durable_hour(any_storage, purged)
    [forged] = build_segments([(r.ts, r.values) for r in rows_of(T0 + 20 * M, [OFF] * 3)])
    replace_hour(any_storage, T0, [*hour_records(any_storage, T0), segment_record(forged)])  # inside the gap
    assert decode_segment(segment_record(forged)) == forged  # structurally valid and canonical
    assert_fails_closed(any_storage, T0, T0 + H)


def test_corruption_in_an_evidence_only_hour_fails_closed(any_storage):
    put(any_storage, T0, [OFF] * 5 + [DHW] * 5 + [OFF] * 20 + [CO] * 30)
    put(any_storage, T0 + H, [CO] * 30 + [OFF] * 30)
    put(any_storage, T0 + 2 * H, [OFF])
    a, b = T0 + H + 10 * M, T0 + H + 20 * M  # the run started in hour 0: loaded only by widening
    assert q(any_storage, a, b)["compressor_runs"][0]["start"] == z(T0 + 30 * M)
    replace_hour(any_storage, T0, hour_records(any_storage, T0)[1:])  # far from the run itself
    assert_fails_closed(any_storage, a, b)


def test_zero_durable_rows_still_mean_raw_then_unavailable(any_storage):
    """Whole-hour absence keeps its B/C meaning: raw if present, else unavailable (422), not 500."""
    durable_hour(any_storage, False)
    reference = q(any_storage, T0, T0 + H)
    delete_activity(any_storage, T0, T0 + H)
    assert q(any_storage, T0, T0 + H) == reference  # derived from raw
    backfill_activity_step(any_storage, 0, 24)
    purge_all(any_storage)
    delete_activity(any_storage, T0, T0 + H)
    with pytest.raises(ActivityUnavailable):
        q(any_storage, T0, T0 + H)


@pytest.mark.parametrize("variant", ["duplicate key", "whitespace", "key order", "exponent spelling",
                                     "signed zero"])
def test_noncanonical_durable_rows_fail_closed(any_storage, variant):
    put(any_storage, T0, SCENARIO + TAIL)
    records = hour_records(any_storage, T0)
    changed = list(records[1])  # CO minutes 5-9
    changed[6] = _variants()[variant](changed[6])
    assert changed[6] != records[1][6]
    if variant != "signed zero":  # every other variant decodes to exactly the canonical segment
        assert decode_segment(tuple(changed)) == decode_segment(records[1])
    replace_hour(any_storage, T0, [records[0], tuple(changed), *records[2:]])
    assert_fails_closed(any_storage, T0, T0 + H)


def test_duplicate_key_row_is_refused_on_mariadb(mariadb):
    mariadb.ensure_schema()
    put(mariadb, T0, SCENARIO + TAIL)
    records = hour_records(mariadb, T0)
    changed = list(records[1])
    changed[6] = _variants()["duplicate key"](changed[6])
    replace_hour(mariadb, T0, [records[0], tuple(changed), *records[2:]])
    with mariadb.session() as s:
        s._cur.execute("SELECT JSON_VALID(energy_json) FROM activity_segment_1h WHERE start_ts = %s",
                       (changed[0],))
        assert s._cur.fetchone() == (1,)
    assert_fails_closed(mariadb, T0, T0 + H)


def test_canonical_arbitrary_float_rows_are_accepted(any_storage):
    import random

    rng = random.Random(21)
    shapes = []
    for _ in range(180):
        shape = dict(rng.choice([CO, DHW, OFF, IDLE, UNKNOWN, defrost(0.583333)]))
        for key in ("co_power_consumption", "co_power_production", "dhw_power_consumption",
                    "dhw_power_production"):
            if shape[key] is not None:
                shape[key] = rng.choice([0.0, 0.1 + 0.2, 1 / 3, 1e-9, rng.uniform(0, 5000)])
        shapes.append(shape)
    put(any_storage, T0, shapes, roll=False)
    reference = q(any_storage, T0, T0 + 3 * H)  # raw
    roll_all(any_storage)
    assert q(any_storage, T0, T0 + 3 * H) == reference  # durable: canonical, complete, accepted
