from conftest import T0
import pytest

from pompa.ingest import Ingest
from pompa.optional_minute import OptionalAccumulator


def setup(stale=600):
    ingest = Ingest(stale)
    acc = OptionalAccumulator(ingest, T0)
    ingest.connect(T0)
    return ingest, acc


def event(ingest, acc, t, topic, value, retained=False):
    closed = acc.advance(t)
    ingest.message(topic, value, retained, t)
    return closed


def test_mean_weighted_zero_missing_and_replacement():
    ing, acc = setup()
    topic = "main/Outside_Pipe_Temp"
    event(ing, acc, T0, topic, "0")
    event(ing, acc, T0 + 15, topic, "20")
    event(ing, acc, T0 + 45, topic, "40")
    assert acc.advance(T0 + 60)[0].values["TOP21"] == 20.0
    event(ing, acc, T0 + 60, topic, "0")
    assert acc.advance(T0 + 120)[0].values["TOP21"] == 0.0
    event(ing, acc, T0 + 130, topic, "-128")
    assert acc.advance(T0 + 180)[0].values["TOP21"] is None
    event(ing, acc, T0 + 180, topic, "garbage")
    assert acc.advance(T0 + 240) == []


def test_missing_first_segment_and_expiry():
    ing, acc = setup(60)
    topic = "main/Outside_Pipe_Temp"
    event(ing, acc, T0 + 10, topic, "8")
    assert acc.advance(T0 + 60)[0].values["TOP21"] is None
    assert acc.advance(T0 + 120)[0].values["TOP21"] is None  # expires at :10


def test_last_final_segment_and_boundary_order():
    ing, acc = setup()
    topic = "main/Room_Heater_Operations_Hours"
    event(ing, acc, T0 + 20, topic, "10")
    assert acc.advance(T0 + 60)[0].values["TOP90"] == 10.0
    event(ing, acc, T0 + 60, topic, "11")
    assert acc.advance(T0 + 120)[0].values["TOP90"] == 11.0
    event(ing, acc, T0 + 140, topic, "bad")
    assert acc.advance(T0 + 180)[0].values["TOP90"] is None


def test_retained_disconnect_reconnect_offline_and_clock_invalidation():
    ing, acc = setup()
    topic = "main/Outside_Pipe_Temp"
    event(ing, acc, T0, topic, "4", retained=True)
    assert ing.optional_historical("TOP21", T0 + 1) is None
    event(ing, acc, T0 + 1, topic, "4")
    assert ing.optional_historical("TOP21", T0 + 1).value == 4.0
    acc.advance(T0 + 30)
    ing.clock_stepped_back(T0 + 30)
    acc.discard_open()
    assert ing.optional_historical("TOP21", T0 + 30) is None
    event(ing, acc, T0 + 31, topic, "5")
    assert acc.advance(T0 + 60) == []
    ing.disconnect(T0 + 60)
    assert ing.optional_historical("TOP21", T0 + 60) is None
    ing.connect(T0 + 60)
    assert ing.optional_historical("TOP21", T0 + 60) is None
    event(ing, acc, T0 + 60, topic, "6")
    assert ing.optional_historical("TOP21", T0 + 61).value == 6.0
    ing.lwt_message("Offline", True, T0 + 61)
    assert ing.optional_historical("TOP21", T0 + 61) is None


def test_empty_open_minute_remains_usable_on_clock_step():
    ing, acc = setup()
    ing.clock_stepped_back(T0)
    acc.discard_open()
    event(ing, acc, T0, "main/Outside_Pipe_Temp", "7")
    assert acc.advance(T0 + 60)[0].values["TOP21"] == 7.0


@pytest.mark.parametrize("changes", [
    [(0, "1e307")],  # one segment product overflows
    [(0, "1e307"), (10, "1e307"), (20, "1e307")],  # finite segments overflow the sum
    [(0, "1e307"), (30, "-1e307")],  # naive infinity plus negative infinity is NaN
])
def test_nonfinite_derived_mean_is_unknown(changes):
    ing, acc = setup()
    for offset, value in changes:
        event(ing, acc, T0 + offset, "main/High_Pressure", value)
    minute = acc.advance(T0 + 60)[0]
    assert minute.values["TOP64"] is None


def test_extreme_finite_last_value_does_not_need_mean_arithmetic():
    ing, acc = setup()
    event(ing, acc, T0, "main/Room_Heater_Operations_Hours", "1e307")
    assert acc.advance(T0 + 60)[0].values["TOP90"] == 1e307
