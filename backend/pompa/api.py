"""HTTP API: request validation and serialisation only."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .catalog import RECORDED_KEYS
from .history import build_1m
from .minute import MINUTE, iso_utc
from .recorder import Recorder
from .storage import Storage, StorageUnavailable

# Stage 1 serves only bucket=1m over at most 48 hours; the other buckets are
# part of the history contract and arrive with the Stage 2 engine.
KNOWN_BUCKETS = ("auto", "1m", "5m", "1h", "1d", "total")
MAX_1M_RANGE_SECONDS = 48 * 3600


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _parse_instant(name: str, raw: str | None) -> float:
    if not raw:
        raise _bad(f"'{name}' is required")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise _bad(f"'{name}' must be an ISO 8601 timestamp with an explicit offset, got {raw!r}") from None
    if dt.tzinfo is None:
        raise _bad(f"'{name}' must include an explicit UTC offset (e.g. 'Z' or '+02:00'), got {raw!r}")
    return dt.timestamp()


def _parse_series(raw: str | None) -> list[str]:
    if raw is None:
        return list(RECORDED_KEYS)
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise _bad("'series' must list at least one metric")
    unknown = [k for k in keys if k not in RECORDED_KEYS]
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

    @app.get("/api/v1/status")
    def status() -> dict:
        now = clock()
        facts = recorder.snapshot(now)
        try:
            oldest, newest, rolled_until = storage.facts()
            database = {"available": True, "error": None,
                        "oldest_minute": iso_utc(oldest), "newest_minute": iso_utc(newest),
                        "rolled_until": iso_utc(rolled_until)}
        except StorageUnavailable as e:
            database = {"available": False, "error": str(e), "oldest_minute": None, "newest_minute": None,
                        "rolled_until": None}
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
        bucket: str = Query("1m"),
        series: str | None = Query(None),
    ) -> dict:
        start = _parse_instant("from", from_)
        end = _parse_instant("to", to)
        if start >= end:
            raise _bad("'from' must be earlier than 'to'")
        if bucket not in KNOWN_BUCKETS:
            raise _bad(f"unknown bucket {bucket!r}; expected one of {', '.join(KNOWN_BUCKETS)}")
        keys = _parse_series(series)
        if bucket != "1m":
            raise HTTPException(status_code=422, detail=f"bucket={bucket} is not available in Stage 1; use bucket=1m")
        for name, ts in (("from", start), ("to", end)):
            if ts % MINUTE:
                raise _bad(f"'{name}' must be aligned to a whole minute for bucket=1m")
        start, end = int(start), int(end)
        if end - start > MAX_1M_RANGE_SECONDS:
            raise HTTPException(status_code=422, detail="bucket=1m range is limited to 48 hours in Stage 1")
        try:
            with storage.session() as s:
                rows = s.read_minutes(start, end, keys)
        except StorageUnavailable as e:
            raise HTTPException(status_code=503, detail=f"database unavailable: {e}") from None
        return build_1m(start, end, keys, rows, clock())

    return app
