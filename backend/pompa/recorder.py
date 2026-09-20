"""Recorder: serialises ingest events, closes minutes, buffers and persists rows.

Event entry points run on the MQTT network thread; ``tick`` runs on the single
recorder thread; ``snapshot`` runs on API threads. One lock guards all
in-memory state. Database I/O happens outside the lock so a slow or absent
database never delays MQTT receive handling.

Write buffer, two explicit parts:

* ``_waiting``: closed rows never submitted to storage, FIFO, at most
  ``buffer_rows``. On overflow the oldest waiting row is dropped and counted;
  it was never submitted, so it cannot have been persisted by this recorder.
* ``_protected``: the batch submitted to storage (at most ``buffer_rows``). A
  failed write does not prove nothing was committed (the acknowledgement may be
  lost), so the batch is retried unchanged and idempotently until a write
  returns success. It is never dropped and never counted in ``dropped_rows``.

A flush claims the whole waiting queue only when no protected batch exists.
Transient memory is therefore at most ``buffer_rows`` protected plus
``buffer_rows`` waiting rows. At most one flush runs at a time.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from .ingest import Ingest
from .minute import MinuteAccumulator, MinuteRow, iso_utc
from .storage import Storage, StorageUnavailable

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, ingest: Ingest, accumulator: MinuteAccumulator, storage: Storage, buffer_rows: int):
        self.ingest = ingest
        self.accumulator = accumulator
        self.storage = storage
        self.buffer_rows = buffer_rows
        self._lock = threading.Lock()
        self._waiting: deque[MinuteRow] = deque()
        self._protected: list[MinuteRow] = []
        self._flushing = False
        self.dropped_rows = 0
        self.rows_closed = 0
        self.rows_written = 0
        self.last_row_minute: int | None = None
        self.last_written_minute: int | None = None
        self.schema_ready = False
        self.db_last_ok_at: float | None = None
        self.db_last_error: str | None = None
        self.db_last_error_at: float | None = None

    # ------------------------------------------------------------- MQTT events

    def on_connect(self, t: float) -> None:
        with self._lock:
            self.ingest.connect(self._advance(t))

    def on_disconnect(self, t: float) -> None:
        with self._lock:
            self.ingest.disconnect(self._advance(t))

    def on_lwt(self, payload: str, retained: bool, t: float) -> None:
        with self._lock:
            self.ingest.lwt_message(payload, retained, self._advance(t))

    def on_message(self, topic: str, payload: str, retained: bool, t: float) -> None:
        with self._lock:
            self.ingest.message(topic, payload, retained, self._advance(t))

    # ------------------------------------------------------------- recorder tick

    def tick(self, now: float) -> None:
        """Close due minutes, bootstrap the schema if needed and flush.

        Writes the protected batch (a retry if one exists), then keeps claiming
        and writing the waiting queue until it is empty or a write fails.
        """
        with self._lock:
            self._advance(now)
            if self._flushing:
                return  # one flush at a time (only a shutdown tick can overlap)
            self._flushing = True
        try:
            while True:
                with self._lock:
                    if not self._protected:
                        self._protected = list(self._waiting)
                        self._waiting.clear()
                    batch = self._protected
                if self.schema_ready and not batch:
                    return
                if not self._write(batch, now):
                    return
                with self._lock:
                    self._protected = []
                    self.rows_written += len(batch)
                    if batch:
                        self.last_written_minute = batch[-1].ts
                if not batch:
                    return
        finally:
            with self._lock:
                self._flushing = False

    def _write(self, batch: list[MinuteRow], now: float) -> bool:
        """Database I/O, called without the lock. True only when the write returned success."""
        try:
            if not self.schema_ready:
                self.storage.ensure_schema()
                self.schema_ready = True
                log.info("sample_1m schema ready")
            self.storage.upsert(batch)
        except StorageUnavailable as e:
            if self.db_last_error is None:
                log.warning("database unavailable, %d row(s) held for retry: %s", len(batch), e)
            self.db_last_error, self.db_last_error_at = str(e), now
            return False
        if self.db_last_error is not None:
            log.info("database available again")
        self.db_last_error = None
        self.db_last_ok_at = now
        return True

    # ------------------------------------------------------------- facts

    def snapshot(self, now: float) -> dict:
        """Factual in-memory state for ``/api/v1/status``."""
        with self._lock:
            ing, acc = self.ingest, self.accumulator
            return {
                "mqtt": {
                    "connected": ing.connected,
                    "epoch": ing.epoch,
                    "connects": ing.connects,
                    "disconnects": ing.disconnects,
                    "connected_at": iso_utc(ing.connected_at),
                    "disconnected_at": iso_utc(ing.disconnected_at),
                    "lwt": {
                        "state": ing.lwt,
                        "retained": ing.lwt_retained,
                        "received_at": iso_utc(ing.lwt_at),
                        "messages": ing.lwt_messages,
                    },
                    "alive": ing.alive_at(now),
                    "alive_since": iso_utc(ing.alive_since) if ing.alive_at(now) else None,
                    "last_live_message_at": iso_utc(ing.latest_live_message_at),
                    "stale_after_seconds": ing.stale_after,
                    "parse_rejects": ing.parse_rejects,
                    "uncatalogued_topics": sorted(ing.uncatalogued_topics),
                },
                "recorder": {
                    "process_start": iso_utc(acc.process_start),
                    "last_closed_minute": iso_utc(acc.last_closed_minute),
                    "last_row_minute": iso_utc(self.last_row_minute),
                    "last_written_minute": iso_utc(self.last_written_minute),
                    "rows_closed": self.rows_closed,
                    "rows_written": self.rows_written,
                    # Submitted to storage, write not yet confirmed (may already be
                    # persisted); retried until success, never dropped.
                    "protected_rows": len(self._protected),
                    # Closed, never submitted; bounded by waiting_capacity, oldest dropped.
                    "waiting_rows": len(self._waiting),
                    "waiting_capacity": self.buffer_rows,
                    "flush_in_progress": self._flushing,
                    "dropped_rows": self.dropped_rows,
                    "schema_ready": self.schema_ready,
                    "db_last_ok_at": iso_utc(self.db_last_ok_at),
                    "db_last_error": self.db_last_error,
                    "db_last_error_at": iso_utc(self.db_last_error_at),
                },
                "sources": [
                    {
                        "id": s.source.id,
                        "topic": s.source.topic,
                        "metric": s.metric.key,
                        "seen_live": s.seen_live,
                        "historical_value": s.value,
                        # Latest non-retained receipt in the current connection epoch (history freshness).
                        "epoch_last_live_at": iso_utc(s.last_live_at),
                        "last_value": s.last_value,
                        "last_outcome": None if s.last_outcome is None else s.last_outcome.value,
                        "last_retained": s.last_retained,
                        "last_received_at": iso_utc(s.last_received_at),
                        # Process-lifetime measurement facts.
                        "first_live_at": iso_utc(s.first_live_at),
                        "latest_live_at": iso_utc(s.latest_live_at),
                        "live_messages": s.live_messages,
                        "retained_messages": s.retained_messages,
                        "sentinel_messages": s.sentinel_messages,
                        "rejected_messages": s.rejected_messages,
                        "gap_count": s.gap_count,
                        "gap_sum_seconds": round(s.gap_sum, 3),
                        "max_live_gap_seconds": None if s.max_live_gap is None else round(s.max_live_gap, 3),
                        "mean_live_gap_seconds": round(s.gap_sum / s.gap_count, 3) if s.gap_count else None,
                    }
                    for s in ing.sources.values()
                ],
            }

    # ------------------------------------------------------------- internals

    def _advance(self, t: float) -> float:
        """Close minutes up to ``t``; returns ``t`` clamped to never go backwards.

        After a wall-clock step backwards, events are applied at the cursor and
        no minute closes until real time passes it again: a gap, never a duplicate.
        """
        t = max(t, self.accumulator.cursor)
        for row in self.accumulator.advance(t):
            self.rows_closed += 1
            self.last_row_minute = row.ts
            self._waiting.append(row)
            if len(self._waiting) > self.buffer_rows:
                lost = self._waiting.popleft()
                self.dropped_rows += 1
                log.warning("waiting queue full, dropped never-submitted minute %s", iso_utc(lost.ts))
        return t
