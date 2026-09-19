import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[2]

T0 = 1_800_000_000  # minute-aligned UTC second
assert T0 % 60 == 0

# A running heat pump; every catalogued topic has a valid value.
RUNNING = {
    "main/Main_Outlet_Temp": "35", "main/Main_Inlet_Temp": "30", "main/Main_Target_Temp": "36",
    "main/DHW_Temp": "44", "main/Outside_Temp": "5", "main/Water_Pressure": "1.7",
    "main/Pump_Flow": "12.5", "main/Pump_Speed": "3000", "main/Compressor_Freq": "40",
    "main/Compressor_Current": "3.1", "main/Fan1_Motor_Speed": "600",
    "extra/Heat_Power_Consumption_Extra": "900", "extra/Heat_Power_Production_Extra": "3600",
    "extra/DHW_Power_Consumption_Extra": "0", "extra/DHW_Power_Production_Extra": "0",
    "main/Heat_Power_Consumption": "1000", "main/Heat_Power_Production": "3400",
    "main/DHW_Power_Consumption": "0", "main/DHW_Power_Production": "0",
    "main/Heatpump_State": "1", "main/Defrosting_State": "0", "main/ThreeWay_Valve_State": "0",
    "main/Operating_Mode_State": "4", "main/Operations_Counter": "7651",
    "main/Operations_Hours": "7724",
}
# Heat pump switched off but HeishaMon publishing: real zeros, TOP power sentinels.
IDLE = {
    **RUNNING,
    "main/Pump_Flow": "0", "main/Pump_Speed": "0", "main/Compressor_Freq": "0",
    "main/Compressor_Current": "0", "main/Fan1_Motor_Speed": "0",
    "extra/Heat_Power_Consumption_Extra": "0", "extra/Heat_Power_Production_Extra": "0",
    "main/Heat_Power_Consumption": "-200", "main/Heat_Power_Production": "-200",
    "main/DHW_Power_Consumption": "-200", "main/DHW_Power_Production": "-200",
    "main/Heatpump_State": "0",
}



class FakeSession:
    """Mirrors ``pompa.storage.Session`` over the dictionaries of a FakeStorage transaction."""

    def __init__(self, rows, rollup):
        self.rows = rows  # ts -> values
        self.rollup = rollup  # (hour_ts, series) -> (n, sum, min, max, last)

    def upsert_minutes(self, rows):
        for r in rows:
            if r.ts % 60:
                raise ValueError(f"unaligned minute ts {r.ts}")
            self.rows[r.ts] = dict(r.values)

    def read_minutes(self, start, end, keys=None):
        from pompa.catalog import RECORDED_KEYS

        keys = RECORDED_KEYS if keys is None else keys
        return [(ts, {k: v[k] for k in keys}) for ts, v in sorted(self.rows.items()) if start <= ts < end]

    def first_minute_at_or_after(self, ts):
        return min((t for t in self.rows if t >= ts), default=None)

    def minute_bounds(self):
        return (min(self.rows), max(self.rows)) if self.rows else (None, None)

    def delete_minutes_before(self, cutoff):
        doomed = [t for t in self.rows if t < cutoff]
        for t in doomed:
            del self.rows[t]
        return len(doomed)

    def rolled_until(self):
        return max(h for h, _ in self.rollup) + 3600 if self.rollup else None

    def replace_rollup_hour(self, hour_ts, values):
        assert hour_ts % 3600 == 0
        for key in [k for k in self.rollup if k[0] == hour_ts]:
            del self.rollup[key]
        for series, n, *stats in values:
            assert n > 0
            self.rollup[(hour_ts, series)] = (n, *stats)

    def read_rollup(self, start, end, series):
        return [(h, s, *v) for (h, s), v in sorted(self.rollup.items()) if start <= h < end and s in series]

    def hour_counts(self, start, end, series):
        counts = {}
        for ts in self.rows:
            if start <= ts < end:
                counts[ts - ts % 3600] = counts.get(ts - ts % 3600, 0) + 1
        return [(h, c, self.rollup[(h, series)][0] if (h, series) in self.rollup else None)
                for h, c in sorted(counts.items())]


class FakeStorage:
    """In-memory stand-in for Storage: one transaction per session, switchable faults.

    ``available=False`` fails every session before it starts. ``fail_commit``
    (a count) makes that many sessions raise instead of committing; with
    ``ack_lost=True`` those sessions commit first and then raise.
    """

    def __init__(self):
        self.available = True
        self.rows = {}  # ts -> values
        self.rollup = {}  # (hour_ts, series) -> (n, sum, min, max, last)
        self.schema_calls = 0
        self.upsert_calls = 0
        self.sessions = 0
        self.fail_commit = 0
        self.ack_lost = False

    def _check(self):
        from pompa.storage import StorageUnavailable

        if not self.available:
            raise StorageUnavailable("fake outage")

    def ensure_schema(self):
        self.schema_calls += 1
        self._check()

    @contextmanager
    def session(self):
        from pompa.storage import StorageUnavailable

        self.sessions += 1
        self._check()
        tx = FakeSession(dict(self.rows), dict(self.rollup))
        tx.upsert_minutes = self._counting(tx.upsert_minutes)
        yield tx
        if self.fail_commit:
            self.fail_commit -= 1
            if self.ack_lost:
                self.rows, self.rollup = tx.rows, tx.rollup
                raise StorageUnavailable("ack lost")
            raise StorageUnavailable("commit failed")
        self.rows, self.rollup = tx.rows, tx.rollup

    def _counting(self, upsert):
        def wrapped(rows):
            self.upsert_calls += 1
            return upsert(rows)
        return wrapped

    def facts(self):
        self._check()
        tx = FakeSession(self.rows, self.rollup)
        return (*tx.minute_bounds(), tx.rolled_until())


@pytest.fixture
def mariadb():
    """Real MariaDB storage; opt in with POMPA_TEST_DB_HOST (see backend/README.md)."""
    host = os.environ.get("POMPA_TEST_DB_HOST")
    if not host:
        pytest.skip("POMPA_TEST_DB_HOST not set")
    from pompa.storage import Storage

    storage = Storage(
        host=host,
        port=int(os.environ.get("POMPA_TEST_DB_PORT", "3306")),
        user=os.environ.get("POMPA_TEST_DB_USER", "pompa"),
        password=os.environ.get("POMPA_TEST_DB_PASSWORD", "pompa"),
        database=os.environ.get("POMPA_TEST_DB_NAME", "pompa_next_test"),
    )
    with storage._connection() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS sample_1m")
        cur.execute("DROP TABLE IF EXISTS rollup_1h")
    return storage


@pytest.fixture(params=["fake", "mariadb"])
def any_storage(request):
    """Both backends: tests using it must behave identically on FakeStorage and MariaDB."""
    if request.param == "fake":
        return FakeStorage()
    storage = request.getfixturevalue("mariadb")
    storage.ensure_schema()
    return storage
