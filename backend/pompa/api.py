"""HTTP API: request validation and serialisation only."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import activity_history
from . import history as history_engine
from .activity import ActivityRecordInvalid
from . import optional_policy
from .capabilities import capability_dict, effective_capabilities
from .history_profile import HISTORY_PROFILES, capability_topics, history_profile_dict
from .minute import MINUTE, iso_utc
from .optional_policy import (
    MemberInfo, RevisionInfo, SelectionView, SeriesDefinitionConflict, StaleBaseRevision,
)
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
    unknown = [k for k in keys if k not in history_engine.HISTORY_SERIES
               and not history_engine.OPTIONAL_SELECTOR.fullmatch(k)]
    if unknown:
        raise _bad(f"unknown series: {', '.join(unknown)}")
    if len(set(keys)) != len(keys):
        raise _bad("'series' contains duplicates")
    return keys


def _revision_dict(revision: RevisionInfo) -> dict:
    return {"id": revision.id, "effective_from": iso_utc(revision.effective_from_minute)}


def _member_dict(member: MemberInfo) -> dict:
    return {
        "identity": member.identity,
        "topic": member.expected_topic,
        "profile_version": member.profile_version,
        "label": member.label,
        "unit": member.unit,
        "kind": member.kind,
        "semantic_type": member.semantic_type,
        "energy": member.energy,
        "blocked_reason": member.blocked_reason,
    }


def _selection_dict(view: SelectionView) -> dict:
    return {
        "active_revision": _revision_dict(view.active_revision),
        "head_revision": _revision_dict(view.head_revision),
        "pending": view.pending,
        "active_members": [_member_dict(m) for m in view.active_members],
        "head_members": [_member_dict(m) for m in view.head_members],
    }


class SelectionRequest(BaseModel):
    base_revision: int
    identities: list[str]


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
    def live(request: Request, include: Literal["readings"] | None = Query(None)) -> dict:
        """In-memory only: unaffected by database availability."""
        if len(request.query_params.getlist("include")) > 1:
            raise _bad("'include' may appear only once")
        return recorder.live(clock, include_readings=include == "readings")

    @app.get("/api/v1/activity/live")
    def activity_live() -> dict:
        """Current activity from the in-memory live observation only: no database read."""
        return activity_history.live_activity(recorder.live(clock))

    @app.get("/api/v1/activity")
    def activity(from_: str | None = Query(None, alias="from"), to: str | None = Query(None)) -> dict:
        """Factual activity, compressor runs, off intervals and defrosts over exact ``[from, to)``."""
        start = _parse_instant("from", from_)
        end = _parse_instant("to", to)
        if start >= end:
            raise _bad("'from' must be earlier than 'to'")
        try:
            now, settled_before = recorder.settled_before(clock)
            return activity_history.query(storage, start, end, now, settled_before=settled_before)
        except (Unrepresentable, activity_history.ActivityUnavailable) as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        except ActivityRecordInvalid as e:
            # Stored durable activity is corrupt: never answered from raw instead, never a 422.
            raise HTTPException(status_code=500, detail=f"stored activity history is inconsistent: {e}") from None
        except StorageUnavailable as e:
            raise HTTPException(status_code=503, detail=f"database unavailable: {e}") from None

    @app.get("/api/v1/metrics")
    def metrics(request: Request,
               include: Literal["capabilities", "history_profiles"] | None = Query(None)) -> dict:
        """Catalog-derived: unaffected by database and MQTT availability."""
        if len(request.query_params.getlist("include")) > 1:
            raise _bad("'include' may appear only once")
        body = history_engine.catalog()
        if include == "capabilities":
            body["capabilities"] = [capability_dict(c) for c in effective_capabilities()]
        elif include == "history_profiles":
            topics = capability_topics()
            body["history_profiles"] = [history_profile_dict(p, topics) for p in HISTORY_PROFILES]
        return body

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

    @app.get("/api/v1/optional-history/selection")
    def get_optional_history_selection() -> dict:
        """Stage 4B checkpoint B (§25.2.1): DB-backed active-vs-pending optional-history selection."""
        try:
            view = optional_policy.read_selection(storage, clock())
        except StorageUnavailable as e:
            raise HTTPException(status_code=503, detail=f"database unavailable: {e}") from None
        return _selection_dict(view)

    @app.get("/api/v1/optional-history/series")
    def get_optional_history_series() -> dict:
        """Persisted historical meanings, including old and currently blocked versions."""
        try:
            with storage.session() as session:
                rows = session.list_optional_series()
        except StorageUnavailable as e:
            raise HTTPException(status_code=503, detail=f"database unavailable: {e}") from None
        return {"series": [history_engine.optional_series_metadata(row) for row in rows]}

    @app.put("/api/v1/optional-history/selection")
    def put_optional_history_selection(body: SelectionRequest) -> dict:
        """Whole-selection replacement; only future complete minutes are ever affected."""
        try:
            result = optional_policy.replace_selection(
                recorder, storage, body.base_revision, body.identities, clock)
        except (StaleBaseRevision, SeriesDefinitionConflict) as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except ValueError as e:
            raise _bad(str(e)) from None
        except StorageUnavailable as e:
            raise HTTPException(status_code=503, detail=f"database unavailable: {e}") from None
        return {"revision": _revision_dict(result.revision), "idempotent_replay": result.idempotent_replay}

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
        except history_engine.HistoryRequestError as e:
            raise _bad(str(e)) from None
        except StorageUnavailable as e:
            raise HTTPException(status_code=503, detail=f"database unavailable: {e}") from None

    return app
