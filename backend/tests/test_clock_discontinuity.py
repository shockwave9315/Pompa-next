"""A backward ``CLOCK_REALTIME`` step is a supported, non-malicious operational scenario (an NTP
correction after normal clock drift, e.g. following a container freeze/resume or a slow boot before
the system clock is disciplined) — not administrator tampering. See docs/ARCHITECTURE.md's
"Operating trust boundary" for the scope this defends and what it deliberately does not.

The model is one rule: **freshness is measured only between two CLOCK_REALTIME readings taken on
the same side of a correction.** Three things follow, and each is pinned below.

* Event *sequencing* keeps its own cursor clamp (``Recorder._advance``). It is an ordering device
  and never an operand of a duration, so a step still produces a gap, never a duplicate.
* A source's freshness anchor is the *raw*, unclamped receipt timestamp. Clamping it to a cursor
  that is temporarily ahead of the wall clock would inflate how long that evidence counts as fresh
  by the size of the step.
* A detected backward step discards every confirmed timestamp taken before it. Without that, a
  pre-step anchor survives into the post-step timeline and understates its own age by the size of
  the step — in ``/api/v1/live``/``/api/v1/status`` and inside ``sample_1m``.

Every case here is parametrised over step sizes both below and **above** ``STALE_AFTER_SECONDS``.
A step smaller than the freshness window hides the whole class of bug.
"""

import pytest

from conftest import RUNNING, T0, Api, FakeStorage
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder

XTOP0 = "extra/Heat_Power_Consumption_Extra"
CO_IN = "co_power_consumption"
STALE = 600

# Deliberately straddles STALE_AFTER_SECONDS in both directions.
STEPS = [2, 120, 300, 599, 601, 1200, 7200]


class Clock:
    """CLOCK_REALTIME driven from real elapsed time plus a settable offset.

    ``real`` advances monotonically exactly as wall time does for an observer
    outside the process; ``step_back`` is the NTP correction.
    """

    def __init__(self, start=T0):
        self.real = float(start)
        self.offset = 0.0

    def __call__(self) -> float:
        return self.real + self.offset

    def step_back(self, seconds):
        self.offset -= seconds


def running(api, clock):
    for topic, payload in RUNNING.items():
        api.recorder.on_message(topic, payload, False, clock())


def warm(clock, storage=None):
    """A recorder with ten ordinary minutes behind it, publishing every 10 s.

    It finishes with a tick at the current instant, so a step applied next is
    seen by the following tick — the real cadence, where ``main.py`` ticks
    every ``TICK_SECONDS`` (1 s).
    """
    api = Api(start=clock(), storage=storage)
    api.recorder.on_connect(clock())
    while clock.real < T0 + 600:
        running(api, clock)
        api.recorder.tick(clock())
        clock.real += 10.0
    api.recorder.tick(clock())
    return api


def live(api, clock):
    api.now = clock()
    return api.client.get("/api/v1/live").json()


# ------------------------------------------------------------------- case 1/3


@pytest.mark.parametrize("step", STEPS)
def test_evidence_from_before_a_step_is_never_fresh_for_longer_than_the_real_window(step):
    """Case 1: the step happens, the source then says nothing.

    Pre-step evidence must not survive a moment longer than ``STALE_AFTER_SECONDS``
    of *real* elapsed time. Discarding it at the step is allowed (fail-closed);
    outliving the window is not.
    """
    clock = Clock()
    api = warm(clock)
    last_evidence_real = clock.real - 10.0
    clock.step_back(step)
    went_stale_at = None
    while clock.real <= T0 + 600 + step + 2 * STALE:
        api.recorder.tick(clock())
        if not live(api, clock)["mqtt"]["alive"]:
            went_stale_at = clock.real - last_evidence_real
            break
        clock.real += 1.0
    assert went_stale_at is not None, f"step={step}: never went stale"
    assert went_stale_at <= STALE + 1, f"step={step}: alive for {went_stale_at}s of real time"


@pytest.mark.parametrize("step", STEPS)
def test_a_step_fabricates_no_minute_past_the_real_freshness_window(step):
    """The same case, in ``sample_1m``: no row may hold a value as known past the real window."""
    clock = Clock()
    api = warm(clock)
    last_evidence_real = clock.real - 10.0
    known_before = {ts for ts, v in api.storage.rows.items() if v[CO_IN] is not None}
    clock.step_back(step)
    while clock.real <= T0 + 600 + step + 2 * STALE:
        api.recorder.tick(clock())
        clock.real += 1.0
    created = sorted({ts for ts, v in api.storage.rows.items() if v[CO_IN] is not None} - known_before)
    # Every minute created after the step closed at a real instant at least as late as the step;
    # none of them may rest on evidence already older than the real freshness window.
    assert all(ts < last_evidence_real + STALE for ts in created), (step, created)
    assert len(api.storage.rows) == len(set(api.storage.rows))  # no duplicate primary keys


# --------------------------------------------------------------------- case 2


@pytest.mark.parametrize("step", STEPS)
def test_the_first_message_after_a_step_is_immediately_usable(step):
    """Case 2: a genuinely fresh receipt must be ``live`` at once, whatever the step size."""
    clock = Clock()
    api = warm(clock)
    clock.step_back(step)
    api.recorder.tick(clock())          # the tick that detects the step
    assert live(api, clock)["metrics"][CO_IN]["mode"] == "none"  # fail-closed in between
    running(api, clock)                 # the source publishes again
    entry = live(api, clock)["metrics"][CO_IN]
    assert entry["mode"] == "live", step
    assert entry["value"] == 900.0
    assert entry["received_at"] <= live(api, clock)["now"]
    assert live(api, clock)["mqtt"]["alive"] is True


@pytest.mark.parametrize("step", STEPS)
def test_a_source_publishing_across_a_step_never_drops_out(step):
    """A message that carries the step forward re-establishes its own source in the same call."""
    clock = Clock()
    api = warm(clock)
    clock.step_back(step)
    running(api, clock)                 # detection and re-establishment in one on_message
    assert live(api, clock)["metrics"][CO_IN]["mode"] == "live", step
    assert live(api, clock)["mqtt"]["alive"] is True


# --------------------------------------------------------------------- case 3


@pytest.mark.parametrize("step", STEPS)
def test_new_evidence_after_a_step_gets_exactly_the_real_freshness_budget(step):
    """Case 3: neither ``STALE_AFTER + step`` nor an instant expiry — exactly the real window."""
    clock = Clock()
    api = warm(clock)
    clock.step_back(step)
    running(api, clock)
    evidence_real = clock.real
    went_stale_at = None
    while clock.real <= evidence_real + 2 * STALE + step:
        api.recorder.tick(clock())
        if not live(api, clock)["mqtt"]["alive"]:
            went_stale_at = clock.real - evidence_real
            break
        clock.real += 1.0
    assert went_stale_at == STALE + 1, f"step={step}: expired after {went_stale_at}s of real time"


# --------------------------------------------------------------------- case 4


@pytest.mark.parametrize("step", STEPS)
def test_status_reports_the_purge_cutoff_purge_will_actually_use(step):
    """Case 4: ``/status`` must describe ``Recorder._purge``, which runs on the raw tick clock."""
    from pompa.timegrid import purge_cutoff

    clock = Clock()
    api = warm(clock)
    clock.step_back(step)
    raw = clock()
    status_now, _ = api.recorder.snapshot(clock)
    assert status_now == raw, step
    _, _, rolled = api.storage.facts()
    assert (purge_cutoff(status_now, rolled, api.recorder.retention_days)
            == purge_cutoff(raw, rolled, api.recorder.retention_days))


# ------------------------------------------------------------------ the guard


def test_a_step_too_small_to_detect_can_only_inflate_freshness_by_its_own_size():
    """The bound on what detection cannot catch.

    A step smaller than the interval between two clock readings never makes the
    readings themselves go backwards, so nothing is discarded — and nothing
    needs to be: the most it can add to a freshness budget is its own size,
    which for the sub-second steps NTP takes without slewing is nothing against
    600 s. This is the only residue of the clock model, and it is bounded.
    """
    step = 0.5
    clock = Clock()
    api = warm(clock)
    last_evidence_real = clock.real - 10.0
    clock.step_back(step)
    went_stale_at = None
    while clock.real <= T0 + 600 + 2 * STALE:
        api.recorder.tick(clock())
        if not live(api, clock)["mqtt"]["alive"]:
            went_stale_at = clock.real - last_evidence_real
            break
        clock.real += 1.0
    assert api.recorder.ingest.clock_steps == 0          # below the threshold, nothing discarded
    assert went_stale_at <= STALE + 1 + step             # and the inflation is at most the step


def test_ordinary_thread_interleaving_is_not_a_clock_step():
    """Two threads sampling the clock microseconds apart must not discard confirmed evidence."""
    clock = Clock()
    api = warm(clock)
    assert api.recorder.ingest.clock_steps == 0
    for back in (0.0, 0.001, 0.2, 0.9):   # well inside CLOCK_STEP_BACK_SECONDS
        api.recorder.tick(clock() - back)
    assert api.recorder.ingest.clock_steps == 0
    assert live(api, clock)["metrics"][CO_IN]["mode"] == "live"


def test_a_detected_step_is_reported_as_a_fact():
    clock = Clock()
    api = warm(clock)
    api.now = clock()
    assert api.client.get("/api/v1/status").json()["mqtt"]["clock_steps"] == 0
    clock.step_back(1200)
    api.recorder.tick(clock())
    api.now = clock()
    mqtt = api.client.get("/api/v1/status").json()["mqtt"]
    assert mqtt["clock_steps"] == 1
    assert mqtt["last_clock_step_at"] is not None


def test_repeated_steps_are_idempotent_and_do_not_black_out_the_replayed_interval():
    """Detection compares consecutive readings, so it fires once per step, not for the whole replay."""
    clock = Clock()
    api = warm(clock)
    clock.step_back(1200)
    for _ in range(30):                  # the replayed interval, cursor still 1200 s ahead
        running(api, clock)
        api.recorder.tick(clock())
        assert live(api, clock)["metrics"][CO_IN]["mode"] == "live"
        clock.real += 10.0
    assert api.recorder.ingest.clock_steps == 1


# --------------------------------------------------------- unchanged elsewhere


def test_normal_operation_freshness_boundary_is_unchanged():
    api = Api()
    api.connect(T0).msg(T0, XTOP0, "900")
    assert api.body("/api/v1/live", T0 + 599)["mqtt"]["alive"] is True
    assert api.body("/api/v1/live", T0 + 600)["mqtt"]["alive"] is True
    assert api.body("/api/v1/live", T0 + 601)["mqtt"]["alive"] is False
    assert api.recorder.ingest.clock_steps == 0


def test_a_forward_step_needs_no_detection_at_all():
    """The symmetric case: a forward jump only ages evidence, which the ordinary window handles."""
    clock = Clock()
    api = warm(clock)
    clock.real += 7200                   # a two-hour container freeze; nothing ran in between
    api.recorder.tick(clock())
    assert api.recorder.ingest.clock_steps == 0
    assert live(api, clock)["mqtt"]["alive"] is False
    last = max(ts for ts, v in api.storage.rows.items() if v[CO_IN] is not None)
    assert last < T0 + 600 + STALE       # the freeze becomes a gap once the window closes


def test_restart_after_downtime_still_records_no_backfill():
    """A fresh process after real downtime remains a gap; the clock model must not change this."""
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
    api.tick(T0 + 1200)
    api.tick(T0)                         # the backward step
    entry = api.body("/api/v1/live", T0)["metrics"][CO_IN]
    assert entry["mode"] == "retained" and entry["value"] == 900.0
    assert api.recorder.ingest.sources[XTOP0].last_retained is True
    assert not any(row.get(CO_IN) is not None for row in api.storage.rows.values())


def test_backward_clock_step_does_not_corrupt_gap_statistics():
    """A raw backward step between two live messages must not record a negative publication gap."""
    ing = Ingest(600)
    ing.connect(T0)
    ing.message(XTOP0, "900", False, T0 + 10)
    ing.message(XTOP0, "950", False, T0 + 15)   # genuine +5s gap
    ing.message(XTOP0, "1000", False, T0 + 5)   # backward step: raw t below the previous baseline
    s = ing.sources[XTOP0]
    assert (s.gap_count, s.gap_sum, s.max_live_gap) == (1, 5.0, 5.0)


# ------------------------------------------------ C-1: the open minute leak
#
# ``Ingest.clock_stepped_back`` discards confirmed source evidence, but a detected step can still
# find ``MinuteAccumulator`` mid-integration on the minute it interrupted. Left alone, that open
# minute's cursor does not move, so once the wall clock catches back up the same buffer keeps
# accumulating — mixing pre-step and post-step segments into one ``MinuteRow`` that was never
# observed on a single timeline (30 s of 900 W folded with 30 s of a post-step 0 W becomes a
# fabricated 450 W row). ``MinuteAccumulator.discard_open`` poisons exactly that open minute.

MODE_TOPIC = "main/Operating_Mode_State"


def close_minute_starting_at(rec, start, storage, step_seconds=5):
    """Drive real ticks past ``start + 60`` so whatever is open at ``start`` gets a chance to close."""
    t = start
    while t < start + 65:
        rec.tick(t)
        t += step_seconds


def test_canonical_reproducer_a_partially_integrated_minute_is_never_fabricated():
    """The exact scenario from the audit: 30s of 900W, a 30s backward step, 30s of 0W afterwards.

    Before this fix this closed as 450W (the wrong, blended average). The only correct outcome is
    that the minute never exists at all.
    """
    storage = FakeStorage()
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), storage, 60)
    rec.on_connect(T0)
    rec.on_message(XTOP0, "900", False, T0)
    rec.tick(T0 + 30)                        # 30s integrated at 900W; minute [T0, T0+60) still open
    rec.on_message(XTOP0, "0", False, T0)    # detected 30s backward step; new evidence is 0W
    close_minute_starting_at(rec, T0, storage)
    assert T0 not in storage.rows            # never 450W, never any row at all


def test_recovery_the_discarded_minute_becomes_a_gap_and_the_next_minute_is_clean():
    """A finite gap, not permanent damage: the very next minute closes normally from clean evidence."""
    storage = FakeStorage()
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), storage, 60)
    rec.on_connect(T0)
    rec.on_message(XTOP0, "900", False, T0)
    rec.tick(T0 + 30)
    rec.on_message(XTOP0, "0", False, T0)
    t = T0
    while t < T0 + 130:                      # keep the source alive through the next minute too
        rec.on_message(XTOP0, "0", False, t)
        rec.tick(t)
        t += 10
    assert T0 not in storage.rows                    # M: discarded, a gap
    assert storage.rows[T0 + 60][CO_IN] == 0.0        # M+1: exists, and holds only post-step evidence


@pytest.mark.parametrize("step", STEPS)
def test_a_minute_with_any_pre_step_integration_is_discarded_across_every_step_size(step):
    """Row absence must not depend on the step's magnitude: discard_open never inspects step size."""
    storage = FakeStorage()
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), storage, 60)
    rec.on_connect(T0)
    rec.on_message(XTOP0, "900", False, T0)
    rec.tick(T0 + 30)                        # into=30s, comfortably mid-minute
    step_to = T0 + 30 - step
    rec.on_message(XTOP0, "0", False, step_to)
    close_minute_starting_at(rec, T0, storage)
    assert T0 not in storage.rows, step


@pytest.mark.parametrize("into", [1, 30, 59])
def test_any_amount_of_pre_step_integration_poisons_the_minute(into):
    """Even a single integrated second is enough: there is no partial-credit threshold."""
    storage = FakeStorage()
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), storage, 60)
    rec.on_connect(T0)
    rec.on_message(XTOP0, "900", False, T0)
    rec.tick(T0 + into)
    rec.on_message(XTOP0, "0", False, T0 + into - 90)   # a representative 90s detected step
    close_minute_starting_at(rec, T0, storage)
    assert T0 not in storage.rows, into


def test_an_empty_open_minute_is_left_usable_by_the_step():
    """The refinement: nothing pre-step means nothing to poison, so fresh evidence can still close it."""
    storage = FakeStorage()
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), storage, 60)
    rec.on_connect(T0)
    # No message and no tick yet: cursor == minute_start == T0, nothing integrated at all.
    rec.on_message(XTOP0, "0", False, T0 - 90)  # a detected step landing on an entirely empty minute
    t = T0 - 90
    while t < T0 + 65:
        rec.on_message(XTOP0, "0", False, t)
        rec.tick(t)
        t += 5
    assert storage.rows[T0][CO_IN] == 0.0       # never poisoned: it may close normally


def test_discard_applies_to_the_whole_row_not_only_the_integrated_mean_metric():
    """A ``kind=last`` field must be discarded too: the whole row is poisoned, not patched field by
    field. Neither a blended mean with a good ``last``, nor a null mean with a fresh ``last``, may
    survive as a row.
    """
    storage = FakeStorage()
    ing = Ingest(600)
    rec = Recorder(ing, MinuteAccumulator(ing, T0), storage, 60)
    rec.on_connect(T0)
    rec.on_message(XTOP0, "900", False, T0)
    rec.on_message(MODE_TOPIC, "2", False, T0)
    rec.tick(T0 + 30)
    rec.on_message(XTOP0, "0", False, T0)       # backward step; both fields get different post-step
    rec.on_message(MODE_TOPIC, "3", False, T0)  # evidence than what was already integrated
    close_minute_starting_at(rec, T0, storage)
    assert T0 not in storage.rows
