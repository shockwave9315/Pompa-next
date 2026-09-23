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



def row(ts, **values):
    """A MinuteRow; unspecified metrics are NULL."""
    from pompa.catalog import RECORDED_KEYS
    from pompa.minute import MinuteRow

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


class FakeSession:
    """Mirrors ``pompa.storage.Session`` over the dictionaries of a FakeStorage transaction."""

    def __init__(self, rows, rollup, storage):
        self.rows = rows  # ts -> values
        self.rollup = rollup  # (hour_ts, series) -> (n, sum, min, max, last)
        # Stage 4B checkpoint B optional-history policy tables (docs/ARCHITECTURE.md §25.2.1):
        # copied from FakeStorage per session, like rows/rollup, and committed back the same way.
        self._storage = storage
        self.optional_series = dict(storage.optional_series)
        self.optional_series_by_key = dict(storage.optional_series_by_key)
        self.optional_revisions = dict(storage.optional_revisions)
        self.optional_members = {k: set(v) for k, v in storage.optional_members.items()}
        self.optional_head = storage.optional_head

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

    def first_purged_hour(self, start, end):
        """Same fact as MariaDB: whole overlapped hours, rollup present, no raw inside."""
        lo, hi = start - start % 3600, -(-end // 3600) * 3600
        for h in sorted({h for h, _ in self.rollup if lo <= h < hi}):
            if not any(h <= ts < h + 3600 for ts in self.rows):
                return h
        return None

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

    # ---------------------------------------------------------- optional history policy (fake)

    def read_policy_head(self):
        return self.optional_head

    def lock_policy_head(self):
        # Single-threaded fake: a plain read already behaves like the real locking/current read.
        return self.optional_head

    def lock_latest_minute_ts(self):
        return max(self.rows) if self.rows else None

    def read_revision(self, revision_id):
        return self.optional_revisions.get(revision_id)

    # Single-threaded fake: no separate snapshot-vs-locking-read distinction to model.
    lock_revision = read_revision

    def read_revision_members(self, revision_id):
        ids = sorted(self.optional_members.get(revision_id, set()))
        return [self.optional_series[i] for i in ids]

    lock_revision_members = read_revision_members

    def get_or_create_series(self, identity, expected_topic, profile_version, label, unit, kind,
                             semantic_type, sentinels_json, min_value, max_value, energy, created_at):
        from pompa.storage import SeriesRow

        key = (identity, expected_topic, profile_version)
        if key in self.optional_series_by_key:
            return self.optional_series_by_key[key]
        series_id = self._storage._alloc_optional_series_id()
        self.optional_series[series_id] = SeriesRow(series_id, identity, expected_topic, profile_version,
                                                     label, unit, kind, semantic_type, sentinels_json,
                                                     min_value, max_value, energy)
        self.optional_series_by_key[key] = series_id
        return series_id

    def lock_series(self, series_id):
        return self.optional_series.get(series_id)

    def insert_revision(self, base_revision_id, effective_from_minute, created_at):
        if effective_from_minute % 60:
            raise ValueError(f"unaligned effective_from_minute {effective_from_minute}")
        revision_id = self._storage._alloc_optional_revision_id()
        self.optional_revisions[revision_id] = (revision_id, base_revision_id, effective_from_minute,
                                                created_at)
        return revision_id

    def insert_revision_members(self, revision_id, series_ids):
        self.optional_members.setdefault(revision_id, set()).update(series_ids)

    def update_policy_head(self, revision_id):
        self.optional_head = revision_id


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
        # Stage 4B checkpoint B optional-history policy (docs/ARCHITECTURE.md §25.2.1): a genesis
        # revision (id 1, empty selection, effective from the beginning of time) with the head
        # already pointing at it, exactly like Storage.ensure_schema seeds real MariaDB.
        self.optional_series = {}  # id -> storage.SeriesRow
        self.optional_series_by_key = {}  # (identity, expected_topic, profile_version) -> id
        self.optional_revisions = {1: (1, None, 0, 0)}  # id -> (id, base_id, effective_from, created_at)
        self.optional_members = {1: set()}  # revision_id -> {series_id}
        self.optional_head = 1
        self._next_optional_series_id = 1
        self._next_optional_revision_id = 2

    def _check(self):
        from pompa.storage import StorageUnavailable

        if not self.available:
            raise StorageUnavailable("fake outage")

    def ensure_schema(self):
        self.schema_calls += 1
        self._check()

    def _alloc_optional_series_id(self):
        series_id = self._next_optional_series_id
        self._next_optional_series_id += 1
        return series_id

    def _alloc_optional_revision_id(self):
        revision_id = self._next_optional_revision_id
        self._next_optional_revision_id += 1
        return revision_id

    def _adopt(self, tx):
        self.rows, self.rollup = tx.rows, tx.rollup
        self.optional_series, self.optional_series_by_key = tx.optional_series, tx.optional_series_by_key
        self.optional_revisions, self.optional_members = tx.optional_revisions, tx.optional_members
        self.optional_head = tx.optional_head

    @contextmanager
    def session(self):
        from pompa.storage import StorageUnavailable

        self.sessions += 1
        self._check()
        tx = FakeSession(dict(self.rows), dict(self.rollup), self)
        tx.upsert_minutes = self._counting(tx.upsert_minutes)
        yield tx
        if self.fail_commit:
            self.fail_commit -= 1
            if self.ack_lost:
                self._adopt(tx)
                raise StorageUnavailable("ack lost")
            raise StorageUnavailable("commit failed")
        self._adopt(tx)

    def _counting(self, upsert):
        def wrapped(rows):
            self.upsert_calls += 1
            return upsert(rows)
        return wrapped

    def facts(self):
        self._check()
        tx = FakeSession(self.rows, self.rollup, self)
        return (*tx.minute_bounds(), tx.rolled_until())


class Api:
    """Recorder, FakeStorage and a TestClient with an explicit clock.

    MQTT events go through the recorder entry points, so tests exercise the
    same lock and accumulation path as the network thread.
    """

    def __init__(self, stale=600, start=T0, storage=None):
        from fastapi.testclient import TestClient

        from pompa.api import create_app
        from pompa.ingest import Ingest
        from pompa.minute import MinuteAccumulator
        from pompa.recorder import Recorder

        self.storage = FakeStorage() if storage is None else storage
        self.ingest = Ingest(stale)
        self.recorder = Recorder(self.ingest, MinuteAccumulator(self.ingest, start), self.storage, 60)
        self.now = float(start)
        self.client = TestClient(create_app(self.recorder, self.storage, clock=lambda: self.now))

    def connect(self, t):
        self.recorder.on_connect(t)
        return self

    def disconnect(self, t):
        self.recorder.on_disconnect(t)
        return self

    def lwt(self, t, payload, retained=False):
        self.recorder.on_lwt(payload, retained, t)
        return self

    def msg(self, t, topic, payload, retained=False):
        self.recorder.on_message(topic, payload, retained, t)
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

    def tick(self, t):
        self.recorder.tick(t)
        return self

    def get(self, path, t=None, **params):
        if t is not None:
            self.now = float(t)
        return self.client.get(path, params=params)

    def body(self, path, t=None, **params):
        r = self.get(path, t, **params)
        assert r.status_code == 200, r.text
        return r.json()


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
        # FK-safe drop order: tables that reference another Stage 4B policy table drop first.
        cur.execute("DROP TABLE IF EXISTS optional_policy_member")
        cur.execute("DROP TABLE IF EXISTS optional_policy_head")
        cur.execute("DROP TABLE IF EXISTS optional_policy_revision")
        cur.execute("DROP TABLE IF EXISTS optional_series")
        cur.execute("DROP TABLE IF EXISTS sample_1m")
        cur.execute("DROP TABLE IF EXISTS rollup_1h")
        conn.commit()
    return storage


@pytest.fixture(params=["fake", "mariadb"])
def any_storage(request):
    """Both backends: tests using it must behave identically on FakeStorage and MariaDB."""
    if request.param == "fake":
        return FakeStorage()
    storage = request.getfixturevalue("mariadb")
    storage.ensure_schema()
    return storage
