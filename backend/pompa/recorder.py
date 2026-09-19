"""Recorder: serialises ingest events, closes minutes, buffers and persists rows.

Event entry points run on the MQTT network thread; ``tick`` runs on the single
recorder thread; ``snapshot`` runs on API threads. One lock guards all
in-memory state. Database I/O happens outside the lock so a slow or absent
database never delays MQTT receive handling.

Write buffer: closed rows queue in FIFO order. A flush claims the rows at the
front (``_in_flight``); overflow never removes a claimed row, because its fate
is unknown until the write returns. At most ``buffer_rows`` unclaimed rows are
kept; beyond that the oldest unclaimed row is dropped at once, since it would
be dropped whatever the write's outcome. When the write returns, claimed rows
leave the buffer as written (success) or become ordinary queued rows again
(failure) and the ``buffer_rows`` bound applies to the whole queue. Every
``dropped_rows`` increment is therefore a row that was never persisted.
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
        self._buffer: deque[MinuteRow] = deque()
        self._in_flight = 0  # rows at the front of _buffer claimed by the running flush
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
        """Close due minutes, bootstrap the schema if needed and flush the buffer."""
        with self._lock:
            self._advance(now)
            if self._in_flight:
                return  # one flush at a time (only a shutdown tick can overlap)
            pending = list(self._buffer)
            if self.schema_ready and not pending:
                return
            self._in_flight = len(pending)
        written = False
        try:
            if not self.schema_ready:
                self.storage.ensure_schema()
                self.schema_ready = True
                log.info("sample_1m schema ready")
            self.storage.upsert(pending)
            written = True
        except StorageUnavailable as e:
            if self.db_last_error is None:
                log.warning("database unavailable, %d row(s) buffered: %s", len(pending), e)
            self.db_last_error, self.db_last_error_at = str(e), now
        finally:
            with self._lock:
                if written:
                    for _ in range(self._in_flight):
                        self._buffer.popleft()
                    self.rows_written += len(pending)
                    if pending:
                        self.last_written_minute = pending[-1].ts
                self._in_flight = 0
                self._enforce_capacity()
        if written:
            if self.db_last_error is not None:
                log.info("database available again")
            self.db_last_error = None
            self.db_last_ok_at = now

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
                    "buffered_rows": len(self._buffer),
                    "in_flight_rows": self._in_flight,
                    "buffer_capacity": self.buffer_rows,
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
            self._buffer.append(row)
        self._enforce_capacity()
        return t

    def _enforce_capacity(self) -> None:
        """Drop the oldest unclaimed rows beyond ``buffer_rows`` (see module docstring)."""
        while len(self._buffer) - self._in_flight > self.buffer_rows:
            lost = self._buffer[self._in_flight]
            del self._buffer[self._in_flight]
            self.dropped_rows += 1
            log.warning("write buffer full, dropped minute %s", iso_utc(lost.ts))
