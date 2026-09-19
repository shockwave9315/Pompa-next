"""Canonical live state and ``GET /api/v1/live``.

Every case drives the real recorder entry points, so the tested path is the one
MQTT uses: lock, ingest mutation, minute accumulation.
"""

import threading

import pytest

from conftest import T0, Api
from pompa.catalog import METRICS
from pompa.minute import iso_utc

XTOP0 = "extra/Heat_Power_Consumption_Extra"
TOP16 = "main/Heat_Power_Consumption"
OUTLET = "main/Main_Outlet_Temp"
CO_IN = "co_power_consumption"
ENTRY_KEYS = {"value", "mode", "source_id", "source_topic", "received_at"}
NONE_ENTRY = {"value": None, "mode": "none", "source_id": None, "source_topic": None, "received_at": None}

Z = "2027-01-15T08:{:02d}:{:02d}Z"  # T0 + m minutes + s seconds


class Live(Api):
    """``Api`` with live-specific readers."""

    def body(self, t):
        return super().body("/api/v1/live", t)

    def metric(self, key, t):
        return self.body(t)["metrics"][key]


@pytest.fixture
def live():
    return Live()


def selected(entry):
    return entry["value"], entry["mode"], entry["source_id"]


# --------------------------------------------------------------------- shape


def test_every_catalog_metric_appears_once_in_catalog_order(live):
    body = live.body(T0 + 1)
    assert list(body["metrics"]) == [m.key for m in METRICS]
    assert all(set(e) == ENTRY_KEYS for e in body["metrics"].values())
    assert set(body) == {"now", "mqtt", "metrics"}
    assert set(body["mqtt"]) == {"connected", "alive", "epoch"}


def test_initial_state_is_none(live):
    body = live.body(T0 + 1)
    assert body["mqtt"] == {"connected": False, "alive": False, "epoch": 0}
    assert all(e == NONE_ENTRY for e in body["metrics"].values())


def test_live_value_carries_its_physical_source_and_receipt(live):
    live.connect(T0).msg(T0 + 30, OUTLET, "35")
    assert live.metric("main_outlet_temp", T0 + 40) == {
        "value": 35.0, "mode": "live", "source_id": "TOP6",
        "source_topic": "main/Main_Outlet_Temp", "received_at": Z.format(0, 30)}
    assert live.body(T0 + 40)["mqtt"] == {"connected": True, "alive": True, "epoch": 1}


def test_real_zero_stays_zero(live):
    live.connect(T0).msg(T0 + 1, XTOP0, "0")
    assert selected(live.metric(CO_IN, T0 + 2)) == (0.0, "live", "XTOP0")


# ------------------------------------------------------------------ retained


def test_retained_only_is_labelled_retained(live):
    live.connect(T0).msg(T0 + 1, XTOP0, "900", retained=True)
    assert live.metric(CO_IN, T0 + 2) == {
        "value": 900.0, "mode": "retained", "source_id": "XTOP0",
        "source_topic": "extra/Heat_Power_Consumption_Extra", "received_at": Z.format(0, 1)}


def test_retained_establishes_no_life_freshness_or_history(live):
    live.connect(T0).lwt(T0, "Online", retained=True).publish(T0 + 1, retained=True)
    assert live.body(T0 + 2)["mqtt"]["alive"] is False
    assert all(e["mode"] == "retained" for e in live.body(T0 + 2)["metrics"].values())
    assert not any(s.seen_live for s in live.ingest.sources.values())
    assert live.ingest.last_live_at is None
    live.body(T0 + 1200)  # reading live never closes minutes
    live.recorder.tick(T0 + 1200)
    assert (live.recorder.rows_closed, live.storage.rows) == (0, {})


def test_retained_higher_priority_loses_to_confirmed_lower_priority(live):
    live.connect(T0).msg(T0 + 1, XTOP0, "900", retained=True).msg(T0 + 2, TOP16, "1000")
    assert selected(live.metric(CO_IN, T0 + 3)) == (1000.0, "live", "TOP16")


def test_later_live_higher_priority_takes_over(live):
    live.connect(T0).msg(T0 + 1, XTOP0, "900", retained=True).msg(T0 + 2, TOP16, "1000")
    live.msg(T0 + 30, XTOP0, "950")
    assert selected(live.metric(CO_IN, T0 + 31)) == (950.0, "live", "XTOP0")


def test_retained_delivery_after_a_live_one_does_not_downgrade_the_source(live):
    live.connect(T0).msg(T0 + 1, XTOP0, "900").msg(T0 + 2, XTOP0, "111", retained=True)
    assert selected(live.metric(CO_IN, T0 + 3)) == (900.0, "live", "XTOP0")


def test_a_later_invalid_message_does_not_revive_the_retained_value(live):
    live.connect(T0).msg(T0 + 1, TOP16, "1000", retained=True).msg(T0 + 2, TOP16, "-200")
    assert live.metric(CO_IN, T0 + 3) == NONE_ENTRY
    live.msg(T0 + 3, TOP16, "nonsense")
    assert live.metric(CO_IN, T0 + 4) == NONE_ENTRY


# ------------------------------------------------- priority, staleness, zeros


def test_sentinel_higher_priority_source_falls_through(live):
    live.connect(T0).msg(T0 + 1, TOP16, "-200").msg(T0 + 1, XTOP0, "garbage")
    assert live.metric(CO_IN, T0 + 2) == NONE_ENTRY
    live.msg(T0 + 2, TOP16, "1000")
    assert selected(live.metric(CO_IN, T0 + 3)) == (1000.0, "live", "TOP16")


def test_stale_higher_priority_falls_through_to_a_fresh_lower_priority(live):
    live.connect(T0).msg(T0, XTOP0, "900").msg(T0 + 300, TOP16, "1000")
    assert selected(live.metric(CO_IN, T0 + 599)) == (900.0, "live", "XTOP0")
    assert selected(live.metric(CO_IN, T0 + 600)) == (1000.0, "live", "TOP16")


def test_stale_non_retained_data_is_never_exposed(live):
    live.connect(T0).msg(T0, XTOP0, "900").msg(T0, TOP16, "1000")
    assert live.metric(CO_IN, T0 + 599)["mode"] == "live"
    assert live.metric(CO_IN, T0 + 600) == NONE_ENTRY


def test_stale_live_value_does_not_fall_back_to_an_older_retained_delivery(live):
    live.connect(T0).msg(T0, XTOP0, "900", retained=True).msg(T0, XTOP0, "950")
    assert selected(live.metric(CO_IN, T0 + 1)) == (950.0, "live", "XTOP0")
    assert live.metric(CO_IN, T0 + 601) == NONE_ENTRY


# --------------------------------------------------- disconnect and reconnect


@pytest.mark.parametrize("end", [
    lambda live, t: live.disconnect(t),
    lambda live, t: live.lwt(t, "Offline"),
])
def test_disconnect_and_lwt_offline_remove_confirmed_live_state(live, end):
    live.connect(T0).publish(T0 + 1)
    assert live.metric(CO_IN, T0 + 2)["mode"] == "live"
    end(live, T0 + 5)
    assert live.metric(CO_IN, T0 + 6) == NONE_ENTRY
    assert live.body(T0 + 6)["mqtt"]["alive"] is False


def test_reconnect_requires_new_live_evidence(live):
    live.connect(T0).publish(T0 + 1).disconnect(T0 + 10).connect(T0 + 11)
    assert live.body(T0 + 12)["mqtt"] == {"connected": True, "alive": False, "epoch": 2}
    assert live.metric(CO_IN, T0 + 12) == NONE_ENTRY
    live.msg(T0 + 13, XTOP0, "800")
    assert selected(live.metric(CO_IN, T0 + 14)) == (800.0, "live", "XTOP0")


def test_retained_redelivery_after_reconnect_stays_retained(live):
    live.connect(T0).publish(T0 + 1).disconnect(T0 + 10).connect(T0 + 11)
    live.lwt(T0 + 11, "Online", retained=True).msg(T0 + 12, XTOP0, "900", retained=True)
    assert selected(live.metric(CO_IN, T0 + 13)) == (900.0, "retained", "XTOP0")
    assert live.body(T0 + 13)["mqtt"]["alive"] is False


# ------------------------------------------------------------- independence


def test_live_performs_no_database_io(live):
    live.connect(T0).publish(T0 + 1)
    live.storage.sessions = 0
    live.storage.available = False
    assert live.metric(CO_IN, T0 + 2)["mode"] == "live"
    assert live.storage.sessions == 0 and live.storage.schema_calls == 0


def test_repeated_reads_follow_every_update():
    live = Live()
    live.connect(T0)
    for i in range(200):
        t = T0 + i
        live.msg(t, XTOP0, str(900 + i)).msg(t, TOP16, str(1000 + i))
        entry = live.metric(CO_IN, t)
        assert selected(entry) == (float(900 + i), "live", "XTOP0")
        assert entry["received_at"] is not None


def test_concurrent_mqtt_updates_never_tear_a_snapshot():
    """A message thread mutating ingest while ``/live`` is read repeatedly.

    Each published value encodes its own receipt second, so a response that
    mixed one message's value with another's metadata is detectable.
    """
    live = Live()
    live.connect(T0)
    live.msg(T0, XTOP0, str(T0))
    stop = threading.Event()
    failures: list[str] = []

    def publish():
        t = T0
        while t < T0 + 400:
            t += 1
            live.now = float(t)
            live.recorder.on_message(XTOP0, str(t), False, t)
        stop.set()

    def read():
        while not stop.is_set():
            body = live.recorder.live(live.now)
            entry = body["metrics"][CO_IN]
            if entry["mode"] != "live" or entry["source_id"] != "XTOP0":
                failures.append(f"lost live state: {entry}")
            elif iso_utc(entry["value"]) != entry["received_at"]:
                failures.append(f"torn snapshot: {entry}")
            if list(body["metrics"]) != [m.key for m in METRICS]:
                failures.append("metric set changed mid-response")

    threads = [threading.Thread(target=publish), threading.Thread(target=read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not failures[:5]
    assert live.ingest.sources[XTOP0].value == float(T0 + 400)
