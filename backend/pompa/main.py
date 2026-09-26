"""Process entry point: one process, one MQTT client, one recorder thread.

``uvicorn.run`` is given the app object, so multiple workers are structurally
impossible; run exactly one container of this service.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from contextlib import asynccontextmanager

import uvicorn

from .api import create_app
from .config import ConfigError, load_settings
from .control_runtime import ControlRuntime
from .ingest import Ingest
from .minute import MinuteAccumulator
from .mqtt import MqttAdapter
from .recorder import Recorder
from .storage import Storage
from .timegrid import LOCAL_TZ_NAME, validate_local_days

log = logging.getLogger("pompa")

TICK_SECONDS = 1.0


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        sys.exit(2)

    logging.Formatter.converter = time.gmtime  # UTC, comparable with API timestamps
    logging.basicConfig(level=settings.log_level, datefmt="%Y-%m-%dT%H:%M:%SZ",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        # Daily buckets are composed of whole UTC hours; stop if the timezone database disagrees.
        validate_local_days()
    except RuntimeError as e:
        print(f"timezone error: {e}", file=sys.stderr)
        sys.exit(2)

    process_start = time.time()
    ingest = Ingest(settings.stale_after_seconds)
    storage = Storage(settings.db_host, settings.db_port, settings.db_user,
                      settings.db_password, settings.db_name)
    recorder = Recorder(ingest, MinuteAccumulator(ingest, process_start), storage,
                        settings.write_buffer_rows, settings.retention_1m_days)
    adapter = MqttAdapter(settings, recorder)
    stop = threading.Event()

    def run_recorder() -> None:
        while not stop.wait(TICK_SECONDS):
            try:
                recorder.tick(time.time())
            except Exception:
                log.exception("recorder tick failed")

    @asynccontextmanager
    async def lifespan(_app):
        log.info("process start %.3f, STALE_AFTER_SECONDS=%d, buffer %d rows,"
                 " RETENTION_1M_DAYS=%d, calendar days in %s",
                 process_start, settings.stale_after_seconds, settings.write_buffer_rows,
                 settings.retention_1m_days, LOCAL_TZ_NAME)
        thread = threading.Thread(target=run_recorder, name="recorder", daemon=True)
        thread.start()
        adapter.start()
        try:
            yield
        finally:
            adapter.stop()  # ends source life: the open minute is never recorded
            stop.set()
            thread.join(timeout=30)
            recorder.tick(time.time())  # last flush of already closed minutes
            facts = recorder.snapshot(time.time)[1]["recorder"]
            left = facts["protected_rows"] + facts["waiting_rows"]
            if left:
                log.warning("shutdown with %d closed minute(s) not persisted", left)

    # Control publishes through the same adapter, client and connection as ingest (§25.5.4).
    app = create_app(recorder, storage, lifespan=lifespan, controls=ControlRuntime(recorder, adapter))
    uvicorn.run(app, host=settings.api_host, port=settings.api_port,
                log_level=settings.log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()
