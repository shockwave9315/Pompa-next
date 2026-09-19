"""rollup_1h: exact hourly folds, contiguous rolling and late writes into rolled hours.

Every test runs on FakeStorage and, with POMPA_TEST_DB_HOST, on MariaDB.
"""

from contextlib import contextmanager

import pytest

from conftest import T0
from pompa import recorder as recorder_module
from pompa.aggregation import SERIES, fold_minutes
from pompa.catalog import RECORDED_KEYS
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator, MinuteRow
from pompa.recorder import Recorder, persist, rebuild_hour, roll_next_hour
from pompa.storage import StorageUnavailable

H = 3600
H0 = T0  # 2027-01-15T08:00:00Z, an hour boundary
assert H0 % H == 0


def row(ts, **values):
    return MinuteRow(ts, {k: values.get(k) for k in RECORDED_KEYS})


def sample(ts, i):
    """A varied minute: real zeros, NULLs and an unpaired power channel now and then."""
    return row(ts, main_outlet_temp=30.0 + (i % 7) * 0.125, outside_temp=None if i % 5 == 0 else -2.5 + i % 3,
               co_power_consumption=0.0 if i % 11 == 0 else 800.0 + i,
               co_power_production=None if i % 13 == 0 else 3000.0 + 2 * i,
               dhw_power_consumption=18.0, dhw_power_production=0.0 if i % 2 else 1500.0,
               operating_mode=float(i % 4), operations_counter=7000.0 + i // 10)


def minutes(start, count, step=60):
    return [sample(start + step * i, i) for i in range(count)]


def rolled(storage):
    with storage.session() as s:
        return {(h, k): v for h, k, *v in s.read_rollup(0, 2**32 - 1, SERIES)}


def expected(storage):
    """The independent reference: fold of every stored hour below rolled_until."""
    with storage.session() as s:
        until = s.rolled_until()
        if until is None:
            return {}
        rows = s.read_minutes(0, until)
    by_hour = {}
    for ts, values in rows:
        by_hour.setdefault(ts - ts % H, []).append((ts, values))
    out = {}
    for h, hour_rows in by_hour.items():
        for k, st in fold_minutes(hour_rows).items():
            out[(h, k)] = [st.n, st.sum, st.min, st.max, st.last]
    return out


def assert_exact(storage):
    assert rolled(storage) == expected(storage)


def roll_all(storage, closed_before=2**32 - 1):
    hours = []
    while (h := roll_next_hour(storage, closed_before)) is not None:
        hours.append(h)
    return hours


def until(storage):
    with storage.session() as s:
        return s.rolled_until()


# ------------------------------------------------------------------ hourly fold


def test_rollup_rows_are_the_ordered_fold(any_storage):
    rows = minutes(H0, 60)
    rows[-1] = row(H0 + 59 * 60, operating_mode=None, outside_temp=None)  # trailing NULLs
    persist(any_storage, rows)
    assert roll_all(any_storage) == [H0]
    got = rolled(any_storage)
    assert got[(H0, "recorded")] == [60, 60.0, 1.0, 1.0, 1.0]
    # kind=last keeps the chronologically last known value, not the NULL minute.
    assert got[(H0, "operating_mode")][4] == 58 % 4
    # Only series with n > 0 have a row; paired CO misses the minutes with unknown production.
    assert got[(H0, "pair_co_in")][0] == 60 - 1 - len([i for i in range(59) if i % 13 == 0])
    assert (H0, "main_target_temp") not in got
    assert set(k for _, k in got) <= set(SERIES)
    assert_exact(any_storage)


def test_rebuild_is_idempotent(any_storage):
    persist(any_storage, minutes(H0, 90))
    roll_all(any_storage)
    first = rolled(any_storage)
    with any_storage.session() as s:
        rebuild_hour(s, H0)
        rebuild_hour(s, H0)
    assert rolled(any_storage) == first
    assert_exact(any_storage)


def test_rolling_is_ascending_contiguous_and_only_closed_hours(any_storage):
    persist(any_storage, minutes(H0, 30) + minutes(H0 + 3 * H, 30) + minutes(H0 + 5 * H + 600, 10))
    assert until(any_storage) is None
    assert roll_all(any_storage, closed_before=H0 + 5 * H) == [H0, H0 + 3 * H]  # empty hours need no row
    assert until(any_storage) == H0 + 4 * H
    assert roll_next_hour(any_storage, H0 + 5 * H + 1800) is None  # hour 5 still open
    assert roll_all(any_storage, closed_before=H0 + 6 * H) == [H0 + 5 * H]
    assert_exact(any_storage)


def test_first_rollup_starts_at_the_oldest_stored_minute(any_storage):
    persist(any_storage, minutes(H0 + 7 * H + 120, 3))
    assert roll_all(any_storage, closed_before=H0 + 9 * H) == [H0 + 7 * H]


# ------------------------------------------------------------------ late writes


@pytest.mark.parametrize("hours_old", [1, 5])
def test_late_minute_into_rolled_hour_is_repaired_in_the_same_write(any_storage, hours_old):
    """A delayed buffered minute entering a rolled recent hour, or one older than two hours."""
    persist(any_storage, [r for r in minutes(H0, 6 * 60) if r.ts % H != 1800])  # minute :30 missing everywhere
    roll_all(any_storage)
    assert until(any_storage) == H0 + 6 * H
    target = H0 + (6 - hours_old) * H
    before = rolled(any_storage)[(target, "recorded")]
    assert before[0] == 59

    late = row(target + 1800, main_outlet_temp=99.0, operating_mode=7.0,
               co_power_consumption=1000.0, co_power_production=5000.0)
    persist(any_storage, [late])  # nothing else runs: the write itself repairs the hour
    got = rolled(any_storage)
    assert got[(target, "recorded")][0] == 60
    assert got[(target, "main_outlet_temp")][3] == 99.0  # new maximum
    assert_exact(any_storage)
    assert until(any_storage) == H0 + 6 * H


def test_late_minute_changes_last_only_when_it_is_the_latest(any_storage):
    rows = minutes(H0, 50)  # minutes :00..:49
    persist(any_storage, rows)
    roll_all(any_storage, closed_before=H0 + H)
    persist(any_storage, [row(H0 + 55 * 60, operating_mode=8.0)])  # later than every stored minute
    assert rolled(any_storage)[(H0, "operating_mode")][4] == 8.0
    persist(any_storage, [row(H0 + 20 * 60, operating_mode=5.0)])  # replaces minute :20, not the last
    assert rolled(any_storage)[(H0, "operating_mode")][4] == 8.0
    assert_exact(any_storage)


def test_late_minute_into_unrolled_hour_waits_for_the_roller(any_storage):
    persist(any_storage, minutes(H0, 60))
    roll_all(any_storage, closed_before=H0 + H)
    persist(any_storage, [row(H0 + 2 * H + 60, outside_temp=1.0)])  # hour 2 >= rolled_until
    assert (H0 + 2 * H, "recorded") not in rolled(any_storage)
    assert roll_all(any_storage, closed_before=H0 + 3 * H) == [H0 + 2 * H]
    assert_exact(any_storage)


def test_batch_touching_rolled_and_unrolled_hours(any_storage):
    persist(any_storage, minutes(H0, 3 * 60))
    roll_all(any_storage, closed_before=H0 + 2 * H)  # hours 0 and 1 rolled, hour 2 not
    persist(any_storage, [row(H0 + 60, outside_temp=50.0), row(H0 + H + 60, outside_temp=51.0),
                          row(H0 + 2 * H + 60, outside_temp=52.0)])
    got = rolled(any_storage)
    assert got[(H0, "outside_temp")][3] == 50.0 and got[(H0 + H, "outside_temp")][3] == 51.0
    assert (H0 + 2 * H, "outside_temp") not in got
    assert_exact(any_storage)


# ------------------------------------------------------------------ fault injection


def lose_next_ack(storage, monkeypatch):
    """The next session commits, then the client sees StorageUnavailable."""
    original = storage.session

    @contextmanager
    def session():
        monkeypatch.setattr(storage, "session", original)
        with original() as s:
            yield s
        raise StorageUnavailable("ack lost")

    monkeypatch.setattr(storage, "session", session)


def test_ack_loss_leaves_raw_and_rollup_committed_together_and_retry_is_idempotent(any_storage, monkeypatch):
    persist(any_storage, minutes(H0, 4 * 60))
    roll_all(any_storage)
    late = [row(H0 + 60 * 61, main_outlet_temp=12.0)]
    lose_next_ack(any_storage, monkeypatch)
    with pytest.raises(StorageUnavailable):
        persist(any_storage, late)
    # The ambiguous commit happened: raw and its rollup are both there and consistent.
    after_ack_loss = rolled(any_storage)
    assert after_ack_loss[(H0 + H, "main_outlet_temp")][2] == 12.0
    assert_exact(any_storage)
    persist(any_storage, late)  # the protected retry
    assert rolled(any_storage) == after_ack_loss
    assert_exact(any_storage)


def test_failure_after_rebuild_rolls_back_raw_and_rollup(any_storage, monkeypatch):
    """Nothing is committed when the transaction fails after DELETE+INSERT of the rollup."""
    persist(any_storage, minutes(H0, 2 * 60))
    roll_all(any_storage)
    before_rollup = rolled(any_storage)
    with any_storage.session() as s:
        before_raw = s.read_minutes(0, 2**32 - 1)

    real_rebuild = recorder_module.rebuild_hour

    def rebuild_then_fail(session, hour_ts):
        real_rebuild(session, hour_ts)
        raise StorageUnavailable("connection lost before commit")

    monkeypatch.setattr(recorder_module, "rebuild_hour", rebuild_then_fail)
    with pytest.raises(StorageUnavailable):
        persist(any_storage, [row(H0 + 30 * 60, main_outlet_temp=-40.0)])
    monkeypatch.undo()
    assert rolled(any_storage) == before_rollup
    with any_storage.session() as s:
        assert s.read_minutes(0, 2**32 - 1) == before_raw


def test_failed_hour_roll_keeps_the_range_contiguous(any_storage, monkeypatch):
    persist(any_storage, minutes(H0, 3 * 60))
    real_rebuild = recorder_module.rebuild_hour

    def fail_on_second_hour(session, hour_ts):
        if hour_ts == H0 + H:
            raise StorageUnavailable("boom")
        real_rebuild(session, hour_ts)

    monkeypatch.setattr(recorder_module, "rebuild_hour", fail_on_second_hour)
    assert roll_next_hour(any_storage, H0 + 3 * H) == H0
    with pytest.raises(StorageUnavailable):
        roll_next_hour(any_storage, H0 + 3 * H)
    assert until(any_storage) == H0 + H  # hour 2 was not rolled past the failed hour 1
    monkeypatch.undo()
    assert roll_all(any_storage, H0 + 3 * H) == [H0 + H, H0 + 2 * H]
    assert_exact(any_storage)


# ------------------------------------------------------------------ recorder orchestration


def recorder_on(storage, start):
    ingest = Ingest(600)
    return Recorder(ingest, MinuteAccumulator(ingest, start), storage, 60)


def test_recorder_rolls_closed_hours_only(any_storage):
    persist(any_storage, minutes(H0, 3 * 60 - 30))
    rec = recorder_on(any_storage, H0 + 2 * H + 1800)  # process runs inside hour 2
    rec.tick(H0 + 2 * H + 1801)
    assert until(any_storage) == H0 + 2 * H
    assert rec.snapshot(H0)["recorder"]["rollup"]["last_rolled_hour"] == "2027-01-15T09:00:00Z"
    rec.tick(H0 + 3 * H + 1)
    assert until(any_storage) == H0 + 3 * H
    assert_exact(any_storage)


def test_protected_late_minute_ack_loss_then_retry(any_storage, monkeypatch):
    """An old protected minute is committed, its acknowledgement lost, then retried."""
    persist(any_storage, [r for r in minutes(H0, 6 * 60) if r.ts != H0 + H + 600])
    roll_all(any_storage)
    now = H0 + 6 * H + 30
    rec = recorder_on(any_storage, now)
    late = row(H0 + H + 600, main_outlet_temp=77.0)  # five hours old, its hour long rolled
    rec._waiting.append(late)
    lose_next_ack(any_storage, monkeypatch)
    rec.tick(now + 1)
    assert [r.ts for r in rec._protected] == [late.ts] and rec.rows_written == 0
    assert rolled(any_storage)[(H0 + H, "main_outlet_temp")][3] == 77.0  # committed with its rollup
    assert_exact(any_storage)
    rec.tick(now + 2)  # retry succeeds
    assert rec._protected == [] and rec.rows_written == 1 and rec.dropped_rows == 0
    assert_exact(any_storage)


def test_ambiguous_commit_then_process_termination(any_storage, monkeypatch):
    persist(any_storage, minutes(H0, 3 * 60))
    roll_all(any_storage)
    now = H0 + 3 * H + 30
    rec = recorder_on(any_storage, now)
    rec._waiting.extend([row(H0 + 600, outside_temp=-30.0), row(H0 + 3 * H, outside_temp=-31.0)])
    lose_next_ack(any_storage, monkeypatch)
    rec.tick(now + 1)
    assert len(rec._protected) == 2
    del rec  # process dies before any retry
    assert rolled(any_storage)[(H0, "outside_temp")][2] == -30.0
    assert_exact(any_storage)
    successor = recorder_on(any_storage, now + 300)
    successor.tick(H0 + 4 * H + 1)  # rolls hour 3, whose only minute came from the lost-ack write
    assert until(any_storage) == H0 + 4 * H
    assert rolled(any_storage)[(H0 + 3 * H, "outside_temp")] == [1, -31.0, -31.0, -31.0, -31.0]
    assert_exact(any_storage)


def test_definite_failure_commits_nothing_and_retry_repairs(any_storage, monkeypatch):
    persist(any_storage, minutes(H0, 2 * 60))
    roll_all(any_storage)
    before = rolled(any_storage)
    now = H0 + 2 * H + 30
    rec = recorder_on(any_storage, now)
    rec._waiting.append(row(H0 + 30 * 60, outside_temp=40.0))
    original = any_storage.session

    @contextmanager
    def failing_session():
        with original() as s:
            yield s
            raise StorageUnavailable("server gone before commit")

    monkeypatch.setattr(any_storage, "session", failing_session)
    rec.tick(now + 1)
    monkeypatch.undo()
    assert rolled(any_storage) == before and len(rec._protected) == 1
    rec.tick(now + 2)
    assert rolled(any_storage)[(H0, "outside_temp")][3] == 40.0
    assert_exact(any_storage)
