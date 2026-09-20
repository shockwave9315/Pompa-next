"""Recorder: serialises ingest events, closes minutes, buffers and persists rows.

Event entry points run on the MQTT network thread; ``tick`` runs on the single
recorder thread; ``snapshot`` runs on API threads. One lock guards all
in-memory state. Database I/O happens outside the lock so a slow or absent
database never delays MQTT receive handling.

Write buffer, two explicit parts:

* ``_waiting``: closed rows never submitted to storage, FIFO, at most
  ``buffer_rows``. On overflow the oldest waiting row is dropped and counted;
  it was never submitted, so it cannot have been persisted by this recorder.
* ``_protected``: the batch submitted to storage (at most ``buffer_rows``). An
  *ambiguous* failure does not prove nothing was committed (the acknowledgement
  may be lost), so the batch is retried unchanged and idempotently until a write
  returns success. It is never dropped and never counted in ``dropped_rows``. A
  *definite refusal* is different: it is raised before anything is upserted and
  can never succeed, so exactly the rows of the refused hours leave the batch,
  are counted in ``refused_rows``, and the rest is written at once.

A flush claims the whole waiting queue only when no protected batch exists.
Transient memory is therefore at most ``buffer_rows`` protected plus
``buffer_rows`` waiting rows. At most one flush runs at a time.

Rollup and purge (after a tick whose flush fully succeeded):

* Every minute write is one transaction that also rebuilds each touched hour
  already below ``rolled_until`` (``persist``). A late minute — a protected
  batch retried after a long outage, a minute closed while its hour was being
  rolled, a clock step back across a restart — therefore commits together with
  its corrected rollup, or not at all. A lost acknowledgement leaves both
  committed; the retry repeats an idempotent upsert and an idempotent rebuild.
  A rolled hour whose raw evidence has already been purged is never rebuilt
  from newly arriving partial raw data; the write fails closed instead
  (``RebuildRefused``).
* Closed hours are rolled in ascending order, one transaction per hour, so
  ``rolled_until = MAX(hour_ts) + 1 h`` bounds a contiguous rolled range.
  Raw minutes at or above it are simply not rolled yet.
* Purge deletes whole hours below ``min(purge_cutoff, oldest pending minute's
  hour)`` and only after proving, per hour, that the rollup accounts for every
  stored minute. Any doubt deletes nothing. The raw evidence of an hour that a
  pending minute can still enter is therefore never purged before its rebuild.
  That proof is also what lets a surviving rollup row stand as evidence of
  deletion, which is how ``Session.first_purged_hour`` answers every path.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Iterable
from enum import Enum

from .aggregation import RECORDED, SERIES, fold_minutes
from .ingest import Ingest
from .minute import MinuteAccumulator, MinuteRow, iso_utc
from .storage import Session, Storage, StorageUnavailable
from .timegrid import HOUR, floor_hour, purge_cutoff

log = logging.getLogger(__name__)

ROLL_HOURS_PER_TICK = 24
PURGE_HOURS_PER_STEP = 24  # at most 1440 minute rows deleted per step
PURGE_INTERVAL_SECONDS = HOUR
MAINTENANCE_RETRY_SECONDS = 60


class PurgeRefused(Exception):
    """Purge could not prove that deleting raw minutes loses nothing; nothing was deleted."""


class RebuildRefused(Exception):
    """Touched hours are already rolled but their raw evidence was already purged.

    Rebuilding one from a newly arrived partial write would silently replace a
    complete rollup with a partial one, so the whole transaction is refused
    before anything is upserted. ``refused_hours`` names every hour that caused
    it, so the recorder can drop exactly the unwritable rows without guessing.
    """

    def __init__(self, message: str, hours: Iterable[int]):
        super().__init__(message)
        self.refused_hours: frozenset[int] = frozenset(hours)


class WriteOutcome(Enum):
    """What one storage write proved.

    ``AMBIGUOUS`` and ``REFUSED`` are not interchangeable: the first may have
    committed and must be retried unchanged, the second provably did not and
    can never succeed.
    """

    SUCCESS = "success"      # committed
    AMBIGUOUS = "ambiguous"  # may or may not have committed; retry the batch unchanged
    REFUSED = "refused"      # nothing was written and nothing can be; the batch was shrunk


def rebuild_hour(session: Session, hour_ts: int) -> None:
    """Replace one hour of ``rollup_1h`` with the ordered fold of its stored minutes.

    Only ever called for an hour that stores minutes: replacing a rolled hour
    with an empty fold would delete evidence instead of correcting it.
    """
    rows = session.read_minutes(hour_ts, hour_ts + HOUR)
    if not rows:
        raise ValueError(f"refusing to rebuild hour {iso_utc(hour_ts)} from no stored minutes")
    folded = fold_minutes(rows)
    session.replace_rollup_hour(hour_ts, [(k, s.n, s.sum, s.min, s.max, s.last)
                                          for k in SERIES if (s := folded.get(k)) is not None])


def persist(storage: Storage, rows: list[MinuteRow]) -> None:
    """Upsert minutes and rebuild every touched, already rolled hour in one transaction.

    A touched hour that is already rolled but whose raw evidence was already
    purged (a wall clock stepped back across a restart by more than raw
    retention) cannot be rebuilt from the newly arriving minute alone: doing
    so would replace its complete rollup with a partial one. Every such hour is
    found before the upsert, and the whole transaction is refused, leaving raw
    and rollup untouched. ``Session.first_purged_hour`` is the one fact that
    decides it, here and on the read path.
    """
    with storage.session() as s:
        rolled_until = s.rolled_until()
        touched = (sorted({floor_hour(r.ts) for r in rows if r.ts < rolled_until})
                  if rolled_until is not None else [])
        refused = [h for h in touched if s.first_purged_hour(h, h + HOUR) is not None]
        if refused:
            raise RebuildRefused(
                f"hour(s) {', '.join(iso_utc(h) for h in refused)} are rolled but their raw evidence"
                " was already purged; refusing to rebuild them from a new partial write", refused)
        s.upsert_minutes(rows)
        for hour_ts in touched:
            rebuild_hour(s, hour_ts)


def roll_next_hour(storage: Storage, closed_before: int) -> int | None:
    """Roll the first stored hour at or above ``rolled_until`` if it ends by ``closed_before``."""
    with storage.session() as s:
        rolled_until = s.rolled_until()
        first = s.first_minute_at_or_after(0 if rolled_until is None else rolled_until)
        if first is None or floor_hour(first) + HOUR > closed_before:
            return None
        rebuild_hour(s, floor_hour(first))
        return floor_hour(first)


def purge_step(storage: Storage, now: float, retention_days: int, pending_from: int | None,
               max_hours: int) -> tuple[int | None, int, bool]:
    """Delete at most ``max_hours`` whole hours of raw minutes below the safe cutoff.

    Returns ``(cutoff, deleted rows, more to delete)``; ``cutoff`` is ``None``
    when nothing may be purged. Raises ``PurgeRefused`` (deleting nothing)
    when an affected hour's rollup does not account for all its stored minutes.
    """
    with storage.session() as s:
        cutoff = purge_cutoff(now, s.rolled_until(), retention_days)
        if cutoff is None:
            return None, 0, False
        if pending_from is not None:
            cutoff = min(cutoff, floor_hour(pending_from))
        oldest, _ = s.minute_bounds()
        if oldest is None or oldest >= cutoff:
            return cutoff, 0, False
        first = floor_hour(oldest)
        end = min(cutoff, first + max_hours * HOUR)
        for hour_ts, stored, rolled in s.hour_counts(first, end, RECORDED):
            if rolled != stored:
                raise PurgeRefused(f"rollup of hour {iso_utc(hour_ts)} accounts for {rolled or 0}"
                                   f" of {stored} stored minutes")
        return cutoff, s.delete_minutes_before(end), end < cutoff


class Recorder:
    def __init__(self, ingest: Ingest, accumulator: MinuteAccumulator, storage: Storage, buffer_rows: int,
                 retention_days: int = 365):
        self.ingest = ingest
        self.accumulator = accumulator
        self.storage = storage
        self.buffer_rows = buffer_rows
        self._lock = threading.Lock()
        self._waiting: deque[MinuteRow] = deque()
        self._protected: list[MinuteRow] = []
        self._flushing = False
        self.dropped_rows = 0   # never-submitted rows lost to waiting-queue overflow
        self.refused_rows = 0   # rows the historical safety guard will never let be written
        self.last_refusal: dict | None = None
        self.rows_closed = 0
        self.rows_written = 0
        self.last_row_minute: int | None = None
        self.last_written_minute: int | None = None
        self.schema_ready = False
        self.db_last_ok_at: float | None = None
        self.db_last_error: str | None = None
        self.db_last_error_at: float | None = None
        self.retention_days = retention_days  # sample_1m; 0 disables purge
        self._roll_done_key: tuple[int, int] | None = None
        self.last_rolled_hour: int | None = None
        self.last_rolled_at: float | None = None
        self.rollup_error: str | None = None
        self.rollup_error_at: float | None = None
        self._purge_next_at = float("-inf")
        self.last_purge_at: float | None = None
        self.last_purge_cutoff: int | None = None
        self.last_purge_deleted = 0
        self.purged_rows = 0
        self.purge_error: str | None = None
        self.purge_error_at: float | None = None

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
        """Close due minutes, bootstrap the schema if needed, flush, then roll up and purge.

        Writes the protected batch (a retry if one exists), then keeps claiming
        and writing the waiting queue until it is empty or a write fails. Rollup
        and purge run only after a flush that left nothing unconfirmed.
        """
        with self._lock:
            self._advance(now)
            if self._flushing:
                return  # one flush at a time (only a shutdown tick can overlap)
            self._flushing = True
        try:
            if self._flush(now):
                self._maintain(now)
        finally:
            with self._lock:
                self._flushing = False

    def _flush(self, now: float) -> bool:
        while True:
            with self._lock:
                if not self._protected:
                    self._protected = list(self._waiting)
                    self._waiting.clear()
                batch = self._protected
            if self.schema_ready and not batch:
                return True
            outcome = self._write(batch, now)
            if outcome is WriteOutcome.AMBIGUOUS:
                return False
            if outcome is WriteOutcome.REFUSED:
                # ``_refuse`` removed at least the row that caused it, so this
                # loop always shrinks; write whatever is left of the batch now.
                continue
            with self._lock:
                self._protected = []
                self.rows_written += len(batch)
                if batch:
                    self.last_written_minute = batch[-1].ts
            if not batch:
                return True

    def _write(self, batch: list[MinuteRow], now: float) -> WriteOutcome:
        """Database I/O, called without the lock. Reports which of the three outcomes happened."""
        outcome = WriteOutcome.SUCCESS
        try:
            if not self.schema_ready:
                self.storage.ensure_schema()
                self.schema_ready = True
                log.info("sample_1m and rollup_1h schema ready")
            if batch:
                persist(self.storage, batch)
        except StorageUnavailable as e:
            if self.db_last_error is None:
                log.warning("database unavailable, %d row(s) held for retry: %s", len(batch), e)
            self.db_last_error, self.db_last_error_at = str(e), now
            return WriteOutcome.AMBIGUOUS
        except RebuildRefused as e:
            self._refuse(batch, e, now)
            outcome = WriteOutcome.REFUSED
        # The database answered in both remaining cases; only the refusal was ours.
        if self.db_last_error is not None:
            log.info("database available again")
        self.db_last_error = None
        self.db_last_ok_at = now
        return outcome

    def _refuse(self, batch: list[MinuteRow], error: RebuildRefused, now: float) -> None:
        """Drop exactly the rows of permanently unwritable hours; keep the rest of the batch.

        The guard runs before the first upsert and the transaction was rolled
        back, so none of this batch reached storage, and nothing will ever
        restore the purged raw evidence these rows would rebuild from. Retrying
        them forever would block the queue and every fresh minute behind it, so
        they are dropped and counted apart from ``rows_written`` (never written)
        and from ``dropped_rows`` (which means queue overflow).
        """
        hours = error.refused_hours
        kept = [r for r in batch if floor_hour(r.ts) not in hours]
        refused = [r for r in batch if floor_hour(r.ts) in hours]
        with self._lock:
            self._protected = kept
            self.refused_rows += len(refused)
            self.last_refusal = {
                "at": iso_utc(now),
                "hours": [iso_utc(h) for h in sorted(hours)],
                "rows": len(refused),
                "reason": str(error),
            }
        log.warning("refused %d minute(s) for purged rolled hour(s) %s; %d row(s) of the batch remain",
                    len(refused), ", ".join(iso_utc(h) for h in sorted(hours)), len(kept))

    # ------------------------------------------------------------- rollup and purge

    def _maintain(self, now: float) -> None:
        with self._lock:
            closed_before = floor_hour(self.accumulator.minute_start)
            pending = [r.ts for r in self._protected] + [r.ts for r in self._waiting]
            written = self.rows_written
        self._roll(now, closed_before, written)
        self._purge(now, min(pending) if pending else None)

    def _roll(self, now: float, closed_before: int, written: int) -> None:
        """Roll closed hours in ascending order; stop at the first failure (contiguity)."""
        key = (closed_before, written)
        if key == self._roll_done_key:
            return  # nothing written and no hour closed since the last complete pass
        for _ in range(ROLL_HOURS_PER_TICK):
            try:
                hour_ts = roll_next_hour(self.storage, closed_before)
            except StorageUnavailable as e:
                if self.rollup_error is None:
                    log.warning("rollup failed, retried next tick: %s", e)
                self.rollup_error, self.rollup_error_at = str(e), now
                return
            if hour_ts is None:
                self._roll_done_key = key
                break
            self.last_rolled_hour, self.last_rolled_at = hour_ts, now
        if self.rollup_error is not None:
            log.info("rollup succeeded again")
        self.rollup_error = None

    def _purge(self, now: float, pending_from: int | None) -> None:
        """Hourly, bounded, fail-closed deletion of raw minutes past retention."""
        if self.retention_days == 0 or now < self._purge_next_at:
            return
        try:
            cutoff, deleted, more = purge_step(self.storage, now, self.retention_days, pending_from,
                                               PURGE_HOURS_PER_STEP)
        except (PurgeRefused, StorageUnavailable) as e:
            if self.purge_error != str(e):
                log.warning("purge deleted nothing: %s", e)
            self.purge_error, self.purge_error_at = str(e), now
            refused = isinstance(e, PurgeRefused)
            self._purge_next_at = now + (PURGE_INTERVAL_SECONDS if refused else MAINTENANCE_RETRY_SECONDS)
            return
        self.purge_error = None
        self.last_purge_at, self.last_purge_cutoff, self.last_purge_deleted = now, cutoff, deleted
        self.purged_rows += deleted
        if deleted:
            log.info("purged %d raw minute(s) below %s", deleted, iso_utc(cutoff))
        self._purge_next_at = now if more else now + PURGE_INTERVAL_SECONDS

    # ------------------------------------------------------------- facts

    def live(self, clock: Callable[[], float]) -> dict:
        """Canonical live metric state for ``/api/v1/live``.

        ``clock`` is read *inside* the lock, so the observation instant belongs
        to the same atomic observation as the state it describes: a message the
        MQTT thread applies while an API thread waits for the lock is either
        wholly in the response, with a ``now`` at or after its receipt, or not
        in it at all. Sampling the clock first would let a later receipt appear
        under an earlier ``now`` without any wall-clock reversal.

        No database I/O and no state change: minutes are closed by ``tick``,
        never by an API thread.
        """
        with self._lock:
            now = clock()
            ing = self.ingest
            return {
                "now": iso_utc(now),
                "mqtt": {"connected": ing.connected, "alive": ing.alive_at(now), "epoch": ing.epoch},
                "metrics": {
                    key: {
                        "value": v.value,
                        "mode": v.mode,
                        "source_id": v.source_id,
                        "source_topic": v.source_topic,
                        "received_at": iso_utc(v.received_at),
                    }
                    for key, v in ing.live_snapshot(now).items()
                },
            }

    def snapshot(self, clock: Callable[[], float]) -> tuple[float, dict]:
        """Factual in-memory state for ``/api/v1/status``, with its observation instant.

        Like ``live``, ``clock`` is read inside the lock: ``now``, the freshness
        of every source and the ``alive`` verdict are one observation. The
        instant is returned because the caller needs it for facts computed
        outside this lock, such as the prospective purge cutoff.
        """
        with self._lock:
            now = clock()
            ing, acc = self.ingest, self.accumulator
            return now, {
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
                    # Definitively unwritable: refused before any write and never retried.
                    "refused_rows": self.refused_rows,
                    "last_refusal": self.last_refusal,
                    "schema_ready": self.schema_ready,
                    "db_last_ok_at": iso_utc(self.db_last_ok_at),
                    "db_last_error": self.db_last_error,
                    "db_last_error_at": iso_utc(self.db_last_error_at),
                    "retention_1m_days": self.retention_days,
                    "rollup": {
                        "last_rolled_hour": iso_utc(self.last_rolled_hour),
                        "last_rolled_at": iso_utc(self.last_rolled_at),
                        "error": self.rollup_error,
                        "error_at": iso_utc(self.rollup_error_at),
                    },
                    "purge": {
                        "last_run_at": iso_utc(self.last_purge_at),
                        "last_cutoff": iso_utc(self.last_purge_cutoff),
                        "last_deleted_rows": self.last_purge_deleted,
                        "deleted_rows": self.purged_rows,
                        "error": self.purge_error,
                        "error_at": iso_utc(self.purge_error_at),
                    },
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
