"""Fail-closed raw retention purge: cutoff, proof of rolled evidence, bounds and scheduling."""

import pytest

from conftest import T0, minutes, row
from pompa import history
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import PURGE_INTERVAL_SECONDS, PurgeRefused, Recorder, persist, purge_step, roll_next_hour
from pompa.timegrid import Unrepresentable

H = 3600
DAY = 86400
H0 = T0  # 2027-01-15T08:00:00Z


def roll_all(storage, closed_before=2**32 - 1):
    while roll_next_hour(storage, closed_before) is not None:
        pass


def stored(storage):
    with storage.session() as s:
        return [ts for ts, _ in s.read_minutes(0, 2**32 - 1, ["outside_temp"])]


def hours_of(storage):
    return sorted({ts - ts % H for ts in stored(storage)})


def purge_all(storage, now, retention_days, pending_from=None, max_hours=24):
    """Run bounded steps until nothing more may be deleted; returns (cutoff, total deleted)."""
    total, more, cutoff = 0, True, None
    while more:
        cutoff, deleted, more = purge_step(storage, now, retention_days, pending_from, max_hours)
        total += deleted
    return cutoff, total


def ten_rolled_hours(storage):
    persist(storage, minutes(H0, 10 * 60))
    roll_all(storage)
    return H0 + 10 * H  # rolled_until


# ------------------------------------------------------------------ cutoff


def test_no_rollup_means_no_purge(any_storage):
    persist(any_storage, minutes(H0, 2 * 60))
    assert purge_step(any_storage, now=H0 + 400 * DAY, retention_days=1, pending_from=None,
                      max_hours=24) == (None, 0, False)
    assert len(stored(any_storage)) == 120


def test_two_hour_reprocessing_window_is_preserved(any_storage):
    rolled_until = ten_rolled_hours(any_storage)
    cutoff, deleted, more = purge_step(any_storage, now=H0 + 400 * DAY, retention_days=365,
                                       pending_from=None, max_hours=24)
    assert (cutoff, more) == (rolled_until - 2 * H, False)
    assert deleted == 8 * 60 and hours_of(any_storage) == [H0 + 8 * H, H0 + 9 * H]


def test_retention_boundary_keeps_the_hour_it_lands_on(any_storage):
    persist(any_storage, minutes(H0, 72 * 60))
    roll_all(any_storage)
    now = H0 + 72 * H  # retention 1 day: floor_hour(now - 1 day) = H0 + 48 h
    cutoff, deleted = purge_all(any_storage, now, retention_days=1)
    assert cutoff == H0 + 48 * H and deleted == 48 * 60
    assert min(stored(any_storage)) == H0 + 48 * H  # the boundary minute itself stays


def test_disabled_retention_purges_nothing(any_storage):
    ten_rolled_hours(any_storage)
    rec = recorder_on(any_storage, retention_days=0)
    rec.tick(H0 + 400 * DAY)
    assert len(stored(any_storage)) == 600
    assert rec.snapshot(H0)["recorder"]["purge"]["last_run_at"] is None


def test_pending_minute_holds_the_cutoff_above_its_hour(any_storage):
    ten_rolled_hours(any_storage)
    pending = H0 + 3 * H + 600  # a protected row still waiting to be written
    cutoff, deleted, _ = purge_step(any_storage, now=H0 + 400 * DAY, retention_days=365,
                                    pending_from=pending, max_hours=24)
    assert cutoff == H0 + 3 * H and deleted == 3 * 60
    assert hours_of(any_storage)[0] == H0 + 3 * H  # its hour's raw evidence survives intact


# ------------------------------------------------------------------ fail closed


def test_unrolled_hours_below_the_cutoff_refuse_the_purge(any_storage):
    """A rollup that is not contiguous proves nothing about the hours below it."""
    persist(any_storage, minutes(H0, 10 * 60))
    with any_storage.session() as s:  # only hour 5 has a rollup row
        s.replace_rollup_hour(H0 + 5 * H, [("recorded", 60, 60.0, 1.0, 1.0, 1.0)])
    with pytest.raises(PurgeRefused, match="08:00:00Z"):
        purge_step(any_storage, now=H0 + 400 * DAY, retention_days=365, pending_from=None, max_hours=24)
    assert len(stored(any_storage)) == 600


MISSING = H0 + 2 * H + 1800  # one minute absent from the recorded history


def rolled_history_missing_a_minute(storage):
    persist(storage, [r for r in minutes(H0, 10 * 60) if r.ts != MISSING])
    roll_all(storage)


def test_rollup_that_misses_a_minute_refuses_the_purge(any_storage):
    rolled_history_missing_a_minute(any_storage)
    with any_storage.session() as s:  # a raw minute written without the rollup repair
        s.upsert_minutes([row(MISSING, outside_temp=5.0)])
    with pytest.raises(PurgeRefused, match="accounts for 59 of 60"):
        purge_step(any_storage, now=H0 + 400 * DAY, retention_days=365, pending_from=None, max_hours=24)
    assert len(stored(any_storage)) == 600


def test_refusal_leaves_the_repair_possible(any_storage):
    """The evidence a rebuild needs is still there, and the repair fixes the hour."""
    rolled_history_missing_a_minute(any_storage)
    late = row(MISSING, outside_temp=5.0)
    with any_storage.session() as s:
        s.upsert_minutes([late])
    with pytest.raises(PurgeRefused):
        purge_step(any_storage, now=H0 + 400 * DAY, retention_days=365, pending_from=None, max_hours=24)
    persist(any_storage, [late])  # rewriting it repairs the hour it belongs to
    _, deleted = purge_all(any_storage, now=H0 + 400 * DAY, retention_days=365)
    assert deleted == 8 * 60  # including the repaired minute, which is no longer missing


# ------------------------------------------------------------------ bounds and schedule


def test_deletion_is_bounded_per_step(any_storage):
    ten_rolled_hours(any_storage)
    cutoff, deleted, more = purge_step(any_storage, now=H0 + 400 * DAY, retention_days=365,
                                       pending_from=None, max_hours=2)
    assert (deleted, more) == (120, True) and hours_of(any_storage)[0] == H0 + 2 * H
    while more:
        _, _, more = purge_step(any_storage, now=H0 + 400 * DAY, retention_days=365,
                                pending_from=None, max_hours=2)
    assert hours_of(any_storage) == [H0 + 8 * H, H0 + 9 * H]


def recorder_on(storage, retention_days=365, start=H0 + 400 * DAY):
    ingest = Ingest(600)
    return Recorder(ingest, MinuteAccumulator(ingest, start), storage, 60, retention_days)


def test_recorder_purges_hourly_and_reports_facts(any_storage):
    ten_rolled_hours(any_storage)
    now = H0 + 400 * DAY
    rec = recorder_on(any_storage)
    rec.tick(now)
    facts = rec.snapshot(now)["recorder"]["purge"]
    assert facts["last_deleted_rows"] == 8 * 60 and facts["deleted_rows"] == 8 * 60
    assert facts["last_cutoff"] == "2027-01-15T16:00:00Z" and facts["error"] is None
    persist(any_storage, minutes(H0 + 10 * H, 60))
    rec.tick(now + 1)  # within the hour: no second purge run
    assert rec.last_purge_at == now and rec.purged_rows == 8 * 60
    rec.tick(now + PURGE_INTERVAL_SECONDS + 1)
    assert rec.last_purge_at == now + PURGE_INTERVAL_SECONDS + 1


def test_recorder_records_a_refusal_without_deleting(any_storage):
    persist(any_storage, minutes(H0, 10 * 60))
    with any_storage.session() as s:
        s.replace_rollup_hour(H0 + 5 * H, [("recorded", 60, 60.0, 1.0, 1.0, 1.0)])
    rec = recorder_on(any_storage)
    rec.tick(H0 + 400 * DAY)
    facts = rec.snapshot(H0)["recorder"]["purge"]
    assert "accounts for 0 of 60" in facts["error"] and facts["last_deleted_rows"] == 0
    assert len(stored(any_storage)) == 600


def test_recorder_purge_bounded_steps_continue_on_the_next_tick(any_storage, monkeypatch):
    monkeypatch.setattr("pompa.recorder.PURGE_HOURS_PER_STEP", 3)
    ten_rolled_hours(any_storage)
    now = H0 + 400 * DAY
    rec = recorder_on(any_storage)
    rec.tick(now)
    assert hours_of(any_storage)[0] == H0 + 3 * H
    rec.tick(now + 1)  # more remained, so the next tick continues immediately
    assert hours_of(any_storage)[0] == H0 + 6 * H
    rec.tick(now + 2)
    assert hours_of(any_storage) == [H0 + 8 * H, H0 + 9 * H]
    assert rec.purged_rows == 8 * 60 and rec.last_purge_deleted == 120  # 3 + 3 + 2 hours
    rec.tick(now + 3)  # the backlog is gone, so the next run waits an hour again
    assert rec.purged_rows == 8 * 60 and rec._purge_next_at == now + 2 + PURGE_INTERVAL_SECONDS


# ------------------------------------------------------------------ what survives purge


def test_purged_hours_remain_readable_as_rollups_but_not_as_minutes(any_storage):
    persist(any_storage, minutes(H0, 72 * 60))
    roll_all(any_storage)
    now = H0 + 72 * H
    purge_all(any_storage, now, retention_days=1)
    body = history.query(any_storage, H0, H0 + 2 * H, "1h", ["outside_temp", "cop_co"], now, retention_days=1)
    assert [b["recorded_minutes"] for b in body["buckets"]] == [60, 60]
    assert body["series"]["cop_co"]["paired_minutes"] == [55, 55]  # 5 unpaired minutes per hour
    with pytest.raises(Unrepresentable):
        history.query(any_storage, H0, H0 + 2 * H, "1m", ["outside_temp"], now, retention_days=1)
