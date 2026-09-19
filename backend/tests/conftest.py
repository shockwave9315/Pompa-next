import os
import sys
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



class FakeStorage:
    """In-memory stand-in for Storage with a switchable outage."""

    def __init__(self):
        self.available = True
        self.rows = {}  # ts -> values
        self.schema_calls = 0
        self.upsert_calls = 0

    def _check(self):
        from pompa.storage import StorageUnavailable

        if not self.available:
            raise StorageUnavailable("fake outage")

    def ensure_schema(self):
        self.schema_calls += 1
        self._check()

    def upsert(self, rows):
        self.upsert_calls += 1
        self._check()
        for r in rows:
            self.rows[r.ts] = dict(r.values)

    def read(self, start, end, keys):
        self._check()
        return [(ts, {k: v[k] for k in keys}) for ts, v in sorted(self.rows.items()) if start <= ts < end]

    def bounds(self):
        self._check()
        return (min(self.rows), max(self.rows)) if self.rows else (None, None)


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
    return storage
