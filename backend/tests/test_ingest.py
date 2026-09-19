"""Ingest and canonical minute semantics. Explicit timestamps; no sleeps."""

import pytest

from conftest import IDLE, RUNNING, T0
from pompa.catalog import RECORDED_KEYS
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator

XTOP0 = "extra/Heat_Power_Consumption_Extra"
TOP16 = "main/Heat_Power_Consumption"
OUTLET = "main/Main_Outlet_Temp"
OUTSIDE = "main/Outside_Temp"
MODE = "main/Operating_Mode_State"


class Sim:
    """Drives ingest + accumulator the way the recorder does: advance, then apply."""

    def __init__(self, start=T0, stale=600):
        self.ingest = Ingest(stale)
        self.acc = MinuteAccumulator(self.ingest, start)
        self.rows = []

    def at(self, t):
        self.rows += self.acc.advance(t)
        return self

    def connect(self, t):
        self.at(t).ingest.connect(t)
        return self

    def disconnect(self, t):
        self.at(t).ingest.disconnect(t)
        return self

    def lwt(self, t, payload, retained=False):
        self.at(t).ingest.lwt_message(payload, retained, t)
        return self

    def msg(self, t, topic, payload, retained=False):
        self.at(t).ingest.message(topic, payload, retained, t)
        return self

    def publish(self, t, snapshot=RUNNING, retained=False):
        for topic, payload in snapshot.items():
            self.msg(t, topic, payload, retained)
        return self

    def publish_every(self, start, end, step=10, snapshot=RUNNING):
        t = start
        while t < end:
            self.publish(t, snapshot)
            t += step
        return self

    def ts(self):
        return [r.ts for r in self.rows]

    def row(self, ts):
        return next(r for r in self.rows if r.ts == ts)

    def hist(self, key, t):
        s = self.ingest.historical(key, t)
        return None if s is None else (s.value, s.source.id)


def minutes(*offsets):
    return [T0 + 60 * o for o in offsets]


# --------------------------------------------------------------- retained / seen_live


def test_retained_does_not_set_seen_live_or_history():
    sim = Sim().connect(T0).msg(T0 + 1, XTOP0, "900", retained=True)
    s = sim.ingest.sources[XTOP0]
    assert (s.seen_live, s.value, s.last_live_at) == (False, None, None)
    assert (s.last_value, s.last_retained) == (900.0, True)  # live cache, labelled retained
    assert sim.hist("co_power_consumption", T0 + 2) is None


def test_retained_does_not_prove_source_alive():
    sim = Sim().connect(T0).lwt(T0, "Online", retained=True).publish(T0 + 1, retained=True)
    assert not sim.ingest.alive_at(T0 + 2)
    assert sim.ingest.last_live_at is None
    sim.at(T0 + 600)
    assert sim.rows == []


def test_retained_preferred_xtop_does_not_block_fresh_top_fallback():
    sim = Sim().connect(T0).msg(T0 + 1, XTOP0, "900", retained=True).msg(T0 + 2, TOP16, "1000")
    assert sim.hist("co_power_consumption", T0 + 3) == (1000.0, "TOP16")


def test_live_xtop_restores_priority():
    sim = Sim().connect(T0).msg(T0 + 1, XTOP0, "900", retained=True).msg(T0 + 2, TOP16, "1000")
    sim.msg(T0 + 30, XTOP0, "950")
    assert sim.hist("co_power_consumption", T0 + 31) == (950.0, "XTOP0")


def test_first_non_retained_message_enables_history():
    sim = Sim().connect(T0).msg(T0 + 1, OUTLET, "35", retained=True)
    assert sim.hist("main_outlet_temp", T0 + 2) is None
    sim.msg(T0 + 5, OUTLET, "35.5")
    assert sim.ingest.sources[OUTLET].seen_live
    assert sim.hist("main_outlet_temp", T0 + 6) == (35.5, "TOP6")


def test_reconnect_resets_source_eligibility():
    sim = Sim().connect(T0).publish(T0 + 1)
    assert sim.hist("co_power_consumption", T0 + 2) == (900.0, "XTOP0")
    sim.disconnect(T0 + 10).connect(T0 + 11)
    assert sim.ingest.epoch == 2
    assert not any(s.seen_live for s in sim.ingest.sources.values())
    assert sim.hist("co_power_consumption", T0 + 12) is None
    assert not sim.ingest.alive_at(T0 + 12)


def test_retained_only_reconnect_cannot_establish_history():
    sim = Sim().connect(T0).publish_every(T0, T0 + 180)
    assert sim.at(T0 + 180).ts() == minutes(0, 1, 2)
    sim.disconnect(T0 + 181).connect(T0 + 185)
    sim.lwt(T0 + 185, "Online", retained=True).publish(T0 + 185, retained=True)
    sim.at(T0 + 1200)
    assert sim.ts() == minutes(0, 1, 2)


def test_retained_after_live_does_not_replace_historical_value():
    sim = Sim().connect(T0).msg(T0 + 1, OUTLET, "35").msg(T0 + 2, OUTLET, "20", retained=True)
    assert sim.hist("main_outlet_temp", T0 + 3) == (35.0, "TOP6")


def test_invalid_higher_priority_source_falls_through():
    sim = Sim().connect(T0).msg(T0 + 1, XTOP0, "garbage").msg(T0 + 1, TOP16, "1000")
    assert sim.ingest.sources[XTOP0].seen_live
    assert sim.ingest.parse_rejects == 1
    assert sim.hist("co_power_consumption", T0 + 2) == (1000.0, "TOP16")
    # A TOP sentinel with nothing else valid is unknown, never -200.
    sim.msg(T0 + 3, TOP16, "-200")
    assert sim.hist("co_power_consumption", T0 + 4) is None
    assert sim.ingest.sources[TOP16].sentinel_messages == 1


def test_freshness_expiration():
    sim = Sim().connect(T0).msg(T0 + 10, OUTLET, "35")
    assert sim.hist("main_outlet_temp", T0 + 609) == (35.0, "TOP6")
    assert sim.hist("main_outlet_temp", T0 + 610) is None  # valid on [10, 610)
    assert sim.ingest.alive_at(T0 + 610)  # architecture: t - last <= STALE
    assert not sim.ingest.alive_at(T0 + 610.001)


def test_expired_higher_priority_source_falls_back():
    sim = Sim().connect(T0).msg(T0, XTOP0, "900").msg(T0 + 300, TOP16, "1000")
    assert sim.hist("co_power_consumption", T0 + 599) == (900.0, "XTOP0")
    assert sim.hist("co_power_consumption", T0 + 600) == (1000.0, "TOP16")


def test_disconnect_invalidates():
    sim = Sim().connect(T0).publish(T0 + 1).disconnect(T0 + 5)
    assert not sim.ingest.alive_at(T0 + 5)
    assert all(sim.hist(k, T0 + 5) is None for k in RECORDED_KEYS)


def test_lwt_offline_invalidates_and_online_needs_new_evidence():
    sim = Sim().connect(T0).lwt(T0, "Online").publish(T0 + 1)
    assert sim.ingest.alive_at(T0 + 2)
    sim.lwt(T0 + 5, "Offline")
    assert not sim.ingest.alive_at(T0 + 5)
    assert sim.hist("main_outlet_temp", T0 + 5) is None
    sim.lwt(T0 + 8, "Online")
    assert not sim.ingest.alive_at(T0 + 8)  # Online is not metric evidence
    sim.msg(T0 + 9, OUTLET, "35")
    assert sim.ingest.alive_since == T0 + 9


def test_uncatalogued_topics_are_ignored_but_listed():
    sim = Sim().connect(T0).msg(T0 + 1, "main/DHW_Target_Temp", "48")
    assert sim.ingest.uncatalogued_topics == {"main/DHW_Target_Temp"}
    assert sim.ingest.last_live_at is None
    assert sim.ingest.parse_rejects == 0


def gaps(sim, topic=OUTLET):
    s = sim.ingest.sources[topic]
    return s.gap_count, s.gap_sum, s.max_live_gap


def test_gaps_between_repeated_live_messages():
    sim = Sim().connect(T0).msg(T0, OUTLET, "35").msg(T0 + 30, OUTLET, "35").msg(T0 + 100, OUTLET, "36")
    assert gaps(sim) == (2, 100, 70)
    assert sim.ingest.sources[OUTLET].live_messages == 3


def test_first_message_of_interval_is_baseline_only():
    sim = Sim().connect(T0).msg(T0 + 5, OUTLET, "35")
    assert gaps(sim) == (0, 0.0, None)


def test_retained_messages_never_contribute_gaps():
    sim = Sim().connect(T0).msg(T0 + 1, OUTLET, "35", retained=True).msg(T0 + 10, OUTLET, "35")
    assert gaps(sim) == (0, 0.0, None)  # retained is not a baseline
    sim.msg(T0 + 15, OUTLET, "35", retained=True).msg(T0 + 40, OUTLET, "35")
    assert gaps(sim) == (1, 30, 30)  # retained in between neither splits nor adds
    assert sim.ingest.sources[OUTLET].retained_messages == 2


def test_reconnect_starts_new_gap_interval():
    sim = Sim().connect(T0).msg(T0, OUTLET, "35").msg(T0 + 10, OUTLET, "35")
    sim.disconnect(T0 + 20).connect(T0 + 500).msg(T0 + 505, OUTLET, "35")
    assert gaps(sim) == (1, 10, 10)  # no 495 s sample spanning the epochs


def test_lwt_offline_ends_gap_interval():
    sim = Sim().connect(T0).msg(T0, OUTLET, "35").msg(T0 + 10, OUTLET, "35")
    sim.lwt(T0 + 12, "Offline").lwt(T0 + 400, "Online").msg(T0 + 410, OUTLET, "35")
    assert gaps(sim) == (1, 10, 10)  # no 400 s sample spanning the offline interval
    sim.msg(T0 + 420, OUTLET, "35")
    assert gaps(sim) == (2, 20, 10)


def test_gap_statistics_accumulate_across_epochs():
    sim = Sim().connect(T0)
    for t in (0, 10, 30):  # gaps 10, 20
        sim.msg(T0 + t, OUTLET, "35")
    sim.disconnect(T0 + 40).connect(T0 + 100)
    for t in (100, 140, 145):  # gaps 40, 5
        sim.msg(T0 + t, OUTLET, "35")
    sim.lwt(T0 + 150, "Offline").lwt(T0 + 160, "Online")
    for t in (170, 185):  # gap 15
        sim.msg(T0 + t, OUTLET, "35")
    assert gaps(sim) == (5, 90, 40)
    s = sim.ingest.sources[OUTLET]
    assert (s.live_messages, s.first_live_at, s.latest_live_at) == (8, T0, T0 + 185)


def test_gap_measurement_does_not_touch_history_state():
    sim = Sim().connect(T0).msg(T0, OUTLET, "35").lwt(T0 + 5, "Offline").lwt(T0 + 6, "Online")
    sim.msg(T0 + 7, OUTLET, "36")
    s = sim.ingest.sources[OUTLET]
    assert (s.seen_live, s.value, s.last_live_at) == (True, 36.0, T0 + 7)
    assert sim.hist("main_outlet_temp", T0 + 8) == (36.0, "TOP6")


# --------------------------------------------------------------- minute rows


def test_source_alive_full_minute_produces_row():
    sim = Sim().connect(T0).publish_every(T0, T0 + 60).at(T0 + 60)
    assert sim.ts() == minutes(0)
    row = sim.row(T0)
    assert row.values["main_outlet_temp"] == 35.0
    assert row.values["co_power_consumption"] == 900.0  # XTOP0 wins
    assert row.values["operating_mode"] == 4.0
    assert set(row.values) == set(RECORDED_KEYS)


def test_pump_off_source_alive_records_real_zeros():
    sim = Sim().connect(T0).publish_every(T0, T0 + 60, snapshot=IDLE).at(T0 + 60)
    v = sim.row(T0).values
    assert v["compressor_freq"] == 0.0
    assert v["pump_flow"] == 0.0
    assert v["heatpump_state"] == 0.0
    assert v["co_power_consumption"] == 0.0  # XTOP0 real zero
    assert v["dhw_power_consumption"] == 0.0  # XTOP2 real zero, TOP41 -200 ignored
    assert v["main_outlet_temp"] == 35.0


def test_top_power_sentinel_never_becomes_power():
    snapshot = {k: v for k, v in IDLE.items() if not k.startswith("extra/")}
    sim = Sim().connect(T0).publish_every(T0, T0 + 60, snapshot=snapshot).at(T0 + 60)
    v = sim.row(T0).values
    for key in ("co_power_consumption", "co_power_production",
                "dhw_power_consumption", "dhw_power_production"):
        assert v[key] is None


def test_source_offline_whole_minute_no_row():
    sim = Sim().connect(T0).at(T0 + 120)
    assert sim.rows == []


def test_disconnect_midway_no_row():
    sim = Sim().connect(T0).publish_every(T0, T0 + 30).disconnect(T0 + 30).at(T0 + 120)
    assert sim.rows == []


def test_lwt_offline_midway_no_row():
    sim = Sim().connect(T0).publish_every(T0, T0 + 60).lwt(T0 + 59.5, "Offline").at(T0 + 120)
    assert sim.rows == []


def test_source_returns_midway_then_next_full_minute_recorded():
    sim = Sim().connect(T0).at(T0 + 55).publish_every(T0 + 55, T0 + 180).at(T0 + 180)
    assert sim.ts() == minutes(1, 2)


def test_reconnect_midway_invalidates_minute():
    sim = Sim().connect(T0).publish_every(T0, T0 + 60)
    sim.disconnect(T0 + 70).connect(T0 + 71).publish_every(T0 + 71, T0 + 240).at(T0 + 240)
    assert sim.ts() == minutes(0, 2, 3)


def test_process_start_midway_no_current_minute():
    start = T0 + 30
    sim = Sim(start=start).connect(start).publish_every(start, T0 + 180).at(T0 + 180)
    assert sim.ts() == minutes(1, 2)


def test_first_advance_never_infers_life_before_process_start():
    ingest = Ingest(600)
    ingest.connect(T0 - 100)
    for topic, payload in RUNNING.items():
        ingest.message(topic, payload, False, T0 - 100)
    acc = MinuteAccumulator(ingest, T0 + 30)
    assert ingest.alive_through(T0, T0 + 60)  # the state alone would allow it
    assert acc.advance(T0 + 60) == []  # but T0 < process_start
    assert [r.ts for r in acc.advance(T0 + 120)] == minutes(1)


def test_late_advance_does_not_backfill():
    sim = Sim(start=T0 + 30).connect(T0 + 30).at(T0 + 3000)
    assert sim.rows == []
    sim.publish_every(T0 + 3000, T0 + 3120).at(T0 + 3120)
    assert sim.ts() == [T0 + 3000, T0 + 3060]
    assert sim.acc.last_closed_minute == T0 + 3060


def test_freshness_holds_value_then_expires_whole_minutes():
    # One burst at T0+10, then silence: rows while fresh, none after.
    sim = Sim().connect(T0).publish(T0 + 10).at(T0 + 3600)
    # Alive [10, 610]: minute M needs alive_since <= M and M+60 <= 610.
    assert sim.ts() == minutes(1, 2, 3, 4, 5, 6, 7, 8, 9)


def test_row_at_exact_freshness_boundary():
    sim = Sim().connect(T0).publish(T0).at(T0 + 600)
    assert sim.ts()[-1] == T0 + 540  # 540 + 60 - 0 <= 600
    assert sim.row(T0 + 540).values["main_outlet_temp"] == 35.0


def test_event_at_minute_boundary_belongs_to_next_minute():
    sim = Sim().connect(T0).publish_every(T0, T0 + 60).disconnect(T0 + 60).at(T0 + 120)
    assert sim.ts() == minutes(0)


def test_mean_is_time_weighted():
    sim = Sim().connect(T0).publish(T0).msg(T0 + 15, OUTLET, "20").msg(T0 + 45, OUTLET, "50")
    sim.at(T0 + 60)
    # 35*15 + 20*30 + 50*15 = 1875 → 31.25
    assert sim.row(T0).values["main_outlet_temp"] == 31.25


def test_metric_becoming_valid_midway_is_null_for_that_minute():
    snapshot = {k: v for k, v in RUNNING.items() if k != OUTSIDE}
    sim = Sim().connect(T0).publish(T0, snapshot).msg(T0 + 20, OUTSIDE, "5")
    sim.publish_every(T0 + 30, T0 + 120, snapshot=RUNNING).at(T0 + 120)
    assert sim.row(T0).values["outside_temp"] is None
    assert sim.row(T0).values["main_outlet_temp"] == 35.0
    assert sim.row(T0 + 60).values["outside_temp"] == 5.0


def test_metric_becoming_unknown_midway_is_null():
    sim = Sim().connect(T0).publish(T0).msg(T0 + 40, OUTSIDE, "-78").at(T0 + 60)
    assert sim.row(T0).values["outside_temp"] is None


def test_last_uses_value_at_minute_end():
    sim = Sim().connect(T0).publish(T0).msg(T0 + 50, MODE, "3").at(T0 + 60)
    assert sim.row(T0).values["operating_mode"] == 3.0


def test_last_valid_only_at_end_still_counts():
    snapshot = {k: v for k, v in RUNNING.items() if k != MODE}
    sim = Sim().connect(T0).publish(T0, snapshot).msg(T0 + 50, MODE, "3").at(T0 + 60)
    assert sim.row(T0).values["operating_mode"] == 3.0


def test_last_unknown_at_end_is_null():
    sim = Sim().connect(T0).publish(T0).msg(T0 + 50, MODE, "-1").at(T0 + 60)
    assert sim.row(T0).values["operating_mode"] is None


def test_missing_topic_nulls_only_that_metric():
    snapshot = {k: v for k, v in RUNNING.items() if k != "main/Water_Pressure"}
    sim = Sim().connect(T0).publish_every(T0, T0 + 60, snapshot=snapshot).at(T0 + 60)
    v = sim.row(T0).values
    assert v["water_pressure"] is None
    assert all(v[k] is not None for k in RECORDED_KEYS if k != "water_pressure")


def test_source_switch_mid_minute_keeps_canonical_metric_continuous():
    snapshot = {k: v for k, v in RUNNING.items() if k != XTOP0}
    sim = Sim().connect(T0).publish(T0, snapshot).msg(T0 + 30, XTOP0, "400").at(T0 + 60)
    # TOP16 1000 W for 30 s, then XTOP0 400 W for 30 s.
    assert sim.row(T0).values["co_power_consumption"] == 700.0


def test_topic_expiring_midminute_nulls_metric_but_keeps_row():
    others = {k: v for k, v in RUNNING.items() if k != OUTLET}
    sim = Sim().connect(T0).publish(T0, others).msg(T0 + 10, OUTLET, "35")
    sim.publish_every(T0 + 60, T0 + 720, step=60, snapshot=others).at(T0 + 720)
    # OUTLET valid on [10, 610): minute 540 ends at 600 → value; minute 600 → NULL.
    assert sim.row(T0 + 540).values["main_outlet_temp"] == 35.0
    assert sim.row(T0 + 600).values["main_outlet_temp"] is None
    assert sim.row(T0).values["main_outlet_temp"] is None  # valid only from T0+10


def test_advance_backwards_is_noop():
    sim = Sim().connect(T0).publish_every(T0, T0 + 60).at(T0 + 90)
    assert sim.acc.advance(T0 + 30) == []
    assert sim.acc.cursor == T0 + 90


@pytest.mark.parametrize("stale", [60, 120])
def test_configurable_stale_after(stale):
    sim = Sim(stale=stale).connect(T0).publish(T0).at(T0 + 600)
    assert sim.ts()[-1] == T0 + stale - 60
