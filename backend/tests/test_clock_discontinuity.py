"""A backward ``CLOCK_REALTIME`` step is a supported, non-malicious operational scenario (an NTP
correction after normal clock drift, e.g. following a container freeze/resume or a slow boot before
the system clock is disciplined) — not administrator tampering. See docs/ARCHITECTURE.md's
"Operating trust boundary" for the scope this defends and what it deliberately does not.

Root cause: ``Recorder._advance`` clamps event timestamps to the accumulator's cursor so minute
closing and upserts stay monotonic and safe. Before this fix, that same clamped timestamp was also
used as a source's freshness anchor (``Ingest.last_live_at`` / ``SourceState.last_live_at``), which
let a source already confirmed alive appear alive for ``STALE_AFTER_SECONDS`` *plus* the size of the
clock step — measured in real elapsed time — both in ``/api/v1/live``/``/api/v1/status`` and, more
seriously, inside ``sample_1m`` itself (fabricated minutes holding a stale value as "known" past the
true freshness window). The fix threads the *raw* receipt timestamp to ``Ingest`` for freshness
while keeping the accumulator's own cursor clamp untouched for sequencing, and floors the API's
observation ``now`` at that same cursor so a receipt can never display later than ``now``.
"""

from conftest import T0, Api, FakeStorage
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder

XTOP0 = "extra/Heat_Power_Consumption_Extra"
CO_IN = "co_power_consumption"


def test_backward_clock_step_never_reports_a_receipt_after_now():
    """API consistency: after a real, supported rollback, received_at <= now (never later)."""
    api = Api()
    api.connect(T0).msg(T0, XTOP0, "900")
    api.tick(T0 + 300)                  # cursor -> T0+300, five minutes of ordinary history
    api.msg(T0, XTOP0, "1234")          # a genuinely new message, wall clock stepped back to T0
    live = api.body("/api/v1/live", T0)
    entry = live["metrics"][CO_IN]
    assert entry["received_at"] <= live["now"]
    assert entry["value"] == 1234.0 and entry["mode"] == "live"


def test_backward_clock_step_does_not_extend_freshness_beyond_600_real_seconds():
    """Physical freshness: aliveness must not outlive STALE_AFTER_SECONDS of real elapsed time."""
    api = Api()
    api.connect(T0).msg(T0, XTOP0, "900")
    api.tick(T0 + 300)
    api.msg(T0, XTOP0, "1234")          # true receipt happens at "real elapsed 0" from here on
    for real_elapsed, expect_alive in [(0, True), (300, True), (600, True), (601, False), (900, False)]:
        alive = api.body("/api/v1/live", T0 + real_elapsed)["mqtt"]["alive"]
        assert alive is expect_alive, real_elapsed


def test_backward_clock_step_does_not_fabricate_history_past_the_true_freshness_window():
    """History safety: no minute may be recorded as known beyond the true 600s real-time budget."""
    api = Api()
    api.connect(T0).msg(T0, XTOP0, "900")
    api.tick(T0 + 300)
    api.msg(T0, XTOP0, "1234")
    for step in range(0, 1300, 30):
        api.tick(T0 + step)
    rows = sorted(ts for ts in api.storage.rows if ts >= T0 + 300)
    assert rows == [T0 + 300 + 60 * i for i in range(5)]  # exactly 300..599s, never 600s or later
    assert len(rows) == len(set(rows))  # no duplicate primary keys


def test_normal_operation_freshness_boundary_is_unchanged():
    """Without any clock discontinuity, the existing 599/600/601s boundary is untouched."""
    api = Api()
    api.connect(T0).msg(T0, XTOP0, "900")
    assert api.body("/api/v1/live", T0 + 599)["mqtt"]["alive"] is True
    assert api.body("/api/v1/live", T0 + 600)["mqtt"]["alive"] is True
    assert api.body("/api/v1/live", T0 + 601)["mqtt"]["alive"] is False


def test_restart_after_downtime_still_records_no_backfill():
    """A fresh process after real downtime remains a gap; the clock fix must not change this."""
    storage = FakeStorage()
    ing1 = Ingest(600)
    rec1 = Recorder(ing1, MinuteAccumulator(ing1, T0), storage, 60)
    rec1.on_connect(T0)
    rec1.on_message(XTOP0, "900", False, T0)
    rec1.tick(T0 + 30)

    new_start = T0 + 3600
    ing2 = Ingest(600)
    rec2 = Recorder(ing2, MinuteAccumulator(ing2, new_start), storage, 60)
    rec2.on_connect(new_start)
    rec2.on_message(XTOP0, "1000", False, new_start)
    rec2.tick(new_start + 90)

    assert all(not (T0 + 30 <= ts < new_start) for ts in storage.rows)


def test_backward_clock_step_does_not_turn_retained_into_confirmed():
    """Retained-vs-historical semantics must survive a clock step exactly as they do otherwise."""
    api = Api()
    api.connect(T0).msg(T0, XTOP0, "900", retained=True)
    api.tick(T0 + 300)
    live = api.body("/api/v1/live", T0)  # a backward step; still nothing but the retained cache
    entry = live["metrics"][CO_IN]
    assert entry["mode"] == "retained" and entry["value"] == 900.0
    assert entry["received_at"] <= live["now"]
    assert not any(row.get(CO_IN) is not None for row in api.storage.rows.values())  # never history


def test_backward_clock_step_does_not_corrupt_gap_statistics():
    """A raw backward step between two live messages must not record a negative publication gap."""
    ing = Ingest(600)
    ing.connect(T0)
    ing.message(XTOP0, "900", False, T0 + 10)
    ing.message(XTOP0, "950", False, T0 + 15)   # genuine +5s gap
    ing.message(XTOP0, "1000", False, T0 + 5)   # backward step: raw t below the previous baseline
    s = ing.sources[XTOP0]
    assert (s.gap_count, s.gap_sum, s.max_live_gap) == (1, 5.0, 5.0)
