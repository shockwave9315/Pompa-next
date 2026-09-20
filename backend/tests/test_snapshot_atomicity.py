"""The observation instant belongs to the same locked snapshot as the state.

`/api/v1/live` and `/api/v1/status` read the clock *inside* the recorder lock.
Sampling it before acquiring the lock let an ordinary MQTT callback slip in
between, so a response could carry a receipt later than its own ``now`` without
any wall-clock reversal.
"""

import threading
from datetime import datetime

import pytest

from conftest import RUNNING, T0, Api

XTOP0 = "extra/Heat_Power_Consumption_Extra"
CO_IN = "co_power_consumption"


def at(iso: str | None) -> float | None:
    return None if iso is None else datetime.fromisoformat(iso).timestamp()


class Clock:
    """A wall clock that only moves forward, as the MQTT adapter's does."""

    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def holds_the_lock(recorder) -> bool:
    """True when this very thread already owns the recorder lock (it is not reentrant)."""
    if recorder._lock.acquire(blocking=False):
        recorder._lock.release()
        return False
    return True


# ----------------------------------------------------- the reported ordering


def previous_live(recorder, clock, mutate):
    """The implementation before this fix: ``recorder.live(clock())``.

    ``mutate`` stands for the MQTT callback that acquires the lock after the
    API thread sampled the clock and before the API thread gets the lock.
    """
    now = clock()
    mutate()
    return recorder.live(lambda: now)


def test_the_previous_ordering_could_report_a_receipt_after_now():
    api = Api()
    api.connect(T0)
    clock = Clock(T0 + 100)

    def mqtt_callback():
        clock.t = T0 + 300
        api.msg(T0 + 300, XTOP0, "900")

    stale = previous_live(api.recorder, clock, mqtt_callback)
    entry = stale["metrics"][CO_IN]
    assert entry["mode"] == "live"  # the later message is in the response ...
    assert at(entry["received_at"]) > at(stale["now"])  # ... under an earlier now


def test_the_fixed_ordering_cannot():
    api = Api()
    api.connect(T0)
    clock = Clock(T0 + 100)

    def mqtt_callback():
        clock.t = T0 + 300
        api.msg(T0 + 300, XTOP0, "900")

    mqtt_callback()  # the same message, applied before the snapshot
    body = api.recorder.live(clock)
    entry = body["metrics"][CO_IN]
    assert entry["mode"] == "live"
    assert at(entry["received_at"]) <= at(body["now"]) == T0 + 300


# ------------------------------------------------- the clock is read in place


def test_live_samples_the_clock_while_holding_the_recorder_lock():
    api = Api()
    api.connect(T0).msg(T0 + 1, XTOP0, "900")
    seen = []
    body = api.recorder.live(lambda: (seen.append(holds_the_lock(api.recorder)), T0 + 10)[1])
    assert seen == [True]
    assert body["now"] == "2027-01-15T08:00:10Z"


def test_status_samples_the_clock_while_holding_the_recorder_lock():
    api = Api()
    api.connect(T0).msg(T0 + 1, XTOP0, "900")
    seen = []
    now, facts = api.recorder.snapshot(lambda: (seen.append(holds_the_lock(api.recorder)), T0 + 10)[1])
    assert seen == [True] and now == T0 + 10
    assert facts["mqtt"]["alive"] is True


def test_an_mqtt_callback_cannot_run_between_the_sample_and_the_snapshot():
    """While the clock is being read, the MQTT thread is provably blocked."""
    api = Api()
    api.connect(T0)
    blocked = threading.Thread(target=lambda: api.msg(T0 + 300, XTOP0, "900"))
    still_running = []

    def clock():
        blocked.start()
        blocked.join(timeout=0.2)
        still_running.append(blocked.is_alive())
        return T0 + 100

    body = api.recorder.live(clock)
    blocked.join(timeout=5)
    assert still_running == [True]  # it cannot acquire the lock this call owns
    assert body["metrics"][CO_IN]["mode"] == "none"  # so its message is not in this response
    assert api.recorder.live(Clock(T0 + 301))["metrics"][CO_IN]["mode"] == "live"


# --------------------------------------------------------- under concurrency


@pytest.mark.parametrize("endpoint", ["live", "status"])
def test_no_response_carries_a_receipt_later_than_its_own_now(endpoint):
    """A publishing thread against a reading thread: ``now`` never precedes a receipt.

    Real contention cannot be relied on to hit the old window — it was a few
    instructions wide — so this guards the invariant while
    ``test_the_previous_ordering_could_report_a_receipt_after_now`` is the
    deterministic reproduction.
    """
    api = Api()
    api.connect(T0)
    clock = Clock(T0)
    stop = threading.Event()
    failures: list[str] = []

    def publish():
        for i in range(1, 400):
            t = T0 + i
            clock.t = float(t)  # the clock reaches the receipt before the message is applied
            api.recorder.on_message(XTOP0, str(900 + i), False, t)
        stop.set()

    def read_live():
        body = api.recorder.live(clock)
        return at(body["now"]), [at(e["received_at"]) for e in body["metrics"].values()]

    def read_status():
        now, facts = api.recorder.snapshot(clock)
        receipts = [at(s["last_received_at"]) for s in facts["sources"]]
        return now, receipts + [at(facts["mqtt"]["last_live_message_at"])]

    read = read_live if endpoint == "live" else read_status

    def reader():
        while not stop.is_set():
            now, receipts = read()
            late = [r for r in receipts if r is not None and r > now]
            if late:
                failures.append(f"now={now} precedes receipts {late}")

    threads = [threading.Thread(target=publish), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert failures[:5] == []
    assert api.ingest.sources[XTOP0].value == 900.0 + 399


def test_status_now_and_alive_are_one_observation():
    api = Api()
    api.connect(T0).publish(T0 + 1, RUNNING)
    fresh_now, fresh = api.recorder.snapshot(Clock(T0 + 600))
    stale_now, stale = api.recorder.snapshot(Clock(T0 + 602))
    assert (fresh_now, fresh["mqtt"]["alive"]) == (T0 + 600, True)
    assert (stale_now, stale["mqtt"]["alive"]) == (T0 + 602, False)
    assert fresh["mqtt"]["alive_since"] == "2027-01-15T08:00:01Z" and stale["mqtt"]["alive_since"] is None


def test_endpoints_keep_the_frozen_shapes():
    api = Api()
    api.connect(T0).publish(T0 + 1, RUNNING)
    live = api.body("/api/v1/live", T0 + 2)
    assert set(live) == {"now", "mqtt", "metrics"} and set(live["mqtt"]) == {"connected", "alive", "epoch"}
    status = api.body("/api/v1/status", T0 + 2)
    assert set(status) == {"now", "mqtt", "recorder", "database", "sources"}
    assert status["now"] == "2027-01-15T08:00:02Z"
    assert at(status["mqtt"]["last_live_message_at"]) <= at(status["now"])
    assert status["database"]["available"] is True  # database facts still read, outside the lock
