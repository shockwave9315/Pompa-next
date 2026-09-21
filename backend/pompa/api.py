"""HTTP API: request validation and serialisation only."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date, datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import history as history_engine
from .minute import MINUTE, iso_utc
from .recorder import Recorder
from .storage import Storage, StorageUnavailable
from .timegrid import BUCKETS, Unrepresentable, local_midnight, purge_cutoff

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Instants must fit the INT UNSIGNED minute keys; the upper bound keeps local-day math in range.
MIN_INSTANT, MAX_INSTANT = 0, 4_102_444_800  # 1970-01-01Z .. 2100-01-01Z


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _parse_instant(name: str, raw: str | None) -> int:
    """ISO 8601 with an explicit offset, or ``YYYY-MM-DD`` meaning local midnight (Europe/Warsaw)."""
    if not raw:
        raise _bad(f"'{name}' is required")
    try:
        if _DATE.match(raw):
            ts: float = local_midnight(date.fromisoformat(raw))
        else:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                raise _bad(f"'{name}' must include an explicit UTC offset (e.g. 'Z' or '+02:00')"
                           f" or be a calendar date YYYY-MM-DD, got {raw!r}")
            ts = dt.timestamp()
    except (ValueError, OverflowError):
        raise _bad(f"'{name}' must be an ISO 8601 timestamp with an explicit offset"
                   f" or a calendar date YYYY-MM-DD, got {raw!r}") from None
    if ts % MINUTE:
        raise _bad(f"'{name}' must be aligned to a whole minute, got {raw!r}")
    if not MIN_INSTANT <= ts <= MAX_INSTANT:
        raise HTTPException(status_code=422, detail=f"'{name}' is outside the recordable range 1970–2100")
    return int(ts)


def _parse_series(raw: str | None) -> list[str]:
    if raw is None:
        return list(history_engine.HISTORY_SERIES)
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise _bad("'series' must list at least one series")
    unknown = [k for k in keys if k not in history_engine.HISTORY_SERIES]
    if unknown:
        raise _bad(f"unknown series: {', '.join(unknown)}")
    if len(set(keys)) != len(keys):
        raise _bad("'series' contains duplicates")
    return keys


def create_app(recorder: Recorder, storage: Storage, clock: Callable[[], float] = time.time,
               lifespan=None) -> FastAPI:
    app = FastAPI(title="Pompa Next", version="1", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": jsonable_encoder(exc.errors())})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/live")
    def live() -> dict:
        """In-memory only: unaffected by database availability."""
        return recorder.live(clock)

    @app.get("/api/v1/metrics")
    def metrics() -> dict:
        """Catalog-derived: unaffected by database and MQTT availability."""
        return history_engine.catalog()

    @app.get("/api/v1/status")
    def status() -> dict:
        # now and the in-memory facts are one locked observation; the database
        # facts below are read afterwards and are not part of it.
        now, facts = recorder.snapshot(clock)
        try:
            oldest, newest, rolled_until = storage.facts()
            database = {"available": True, "error": None,
                        "oldest_minute": iso_utc(oldest), "newest_minute": iso_utc(newest),
                        "rolled_until": iso_utc(rolled_until),
                        # Prospective policy, not history: what purge may delete next. What it
                        # already deleted is a per-hour fact, not one boundary (natural gaps are legal).
                        "purge_cutoff": iso_utc(purge_cutoff(now, rolled_until, recorder.retention_days))}
        except StorageUnavailable as e:
            database = {"available": False, "error": str(e), "oldest_minute": None, "newest_minute": None,
                        "rolled_until": None, "purge_cutoff": None}
        return {
            "now": iso_utc(now),
            "mqtt": facts["mqtt"],
            "recorder": facts["recorder"],
            "database": database,
            "sources": facts["sources"],
        }

    @app.get("/api/v1/history")
    def history(
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
        bucket: str = Query("auto"),
        series: str | None = Query(None),
    ) -> dict:
        start = _parse_instant("from", from_)
        end = _parse_instant("to", to)
        if start >= end:
            raise _bad("'from' must be earlier than 'to'")
        if bucket not in BUCKETS:
            raise _bad(f"unknown bucket {bucket!r}; expected one of {', '.join(BUCKETS)}")
        names = _parse_series(series)
        try:
            return history_engine.query(storage, start, end, bucket, names, clock())
        except Unrepresentable as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        except StorageUnavailable as e:
            raise HTTPException(status_code=503, detail=f"database unavailable: {e}") from None

    return app
