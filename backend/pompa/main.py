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
from .ingest import Ingest
from .minute import MinuteAccumulator
from .mqtt import MqttAdapter
from .recorder import Recorder
from .storage import Storage

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

    process_start = time.time()
    ingest = Ingest(settings.stale_after_seconds)
    storage = Storage(settings.db_host, settings.db_port, settings.db_user,
                      settings.db_password, settings.db_name)
    recorder = Recorder(ingest, MinuteAccumulator(ingest, process_start), storage,
                        settings.write_buffer_rows)
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
        log.info("process start %.3f, STALE_AFTER_SECONDS=%d (bootstrap value), buffer %d rows",
                 process_start, settings.stale_after_seconds, settings.write_buffer_rows)
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
            left = recorder.snapshot(time.time())["recorder"]["buffered_rows"]
            if left:
                log.warning("shutdown with %d closed minute(s) not persisted", left)

    app = create_app(recorder, storage, lifespan=lifespan)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port,
                log_level=settings.log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()
