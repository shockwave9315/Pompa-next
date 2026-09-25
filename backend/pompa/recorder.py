"""Recorder: serialises ingest events, closes minutes, buffers and persists pairs.

Event entry points run on the MQTT network thread; ``tick`` runs on the single
recorder thread; ``snapshot`` runs on API threads. One lock guards all
in-memory state. Database I/O happens outside the lock so a slow or absent
database never delays MQTT receive handling.

Write buffer, two explicit parts:

* ``_waiting``: closed canonical/optional pairs never submitted to storage, FIFO, at most
  ``buffer_rows``. On overflow the oldest waiting pair is dropped and counted;
  it was never submitted, so it cannot have been persisted by this recorder.
* ``_protected``: the batch of pairs submitted to storage (at most ``buffer_rows``). An
  *ambiguous* failure does not prove nothing was committed (the acknowledgement
  may be lost), so the batch is retried unchanged and idempotently until a write
  returns success. It is never dropped and never counted in ``dropped_rows``. A
  *definite refusal* is different: it is raised before anything is upserted and
  can never succeed, so exactly the pairs of the refused hours leave the batch,
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
* Every rebuilt hour also replaces its durable Stage 4C activity segments in
  the same transaction. Rolled hours recorded before those segments existed are
  materialized by a bounded backfill through the same ``rebuild_hour``; purge
  waits for it in this process and in any case proves the segments first.
* Purge deletes whole hours below ``min(purge_cutoff, oldest pending minute's
  hour)`` and only after proving, per hour, that the canonical rollup accounts
  for every stored minute, that the optional rollup equals the fold of its raw
  and policy evidence, and that the stored activity segments equal the segments
  of the locked raw minutes. Any doubt deletes nothing. The raw evidence of an
  hour that a pending minute can still enter is therefore never purged before
  its rebuild. That proof is also what lets a surviving rollup row stand as
  evidence of deletion, which is how ``Session.first_purged_hour`` answers every
  path.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .activity import ActivityRecordInvalid, build_segments, decode_segments, segment_record
from .aggregation import (RECORDED, SERIES, OptionalHistoryInconsistent, OptionalStats,
                          fold_minutes, fold_optional_minutes)
from .ingest import LWT_OFFLINE, Ingest, PhysicalReading
from .minute import MINUTE, MinuteAccumulator, MinuteRow, floor_minute, iso_utc
from .optional_minute import OptionalAccumulator, OptionalMinute
from .history_profile import HISTORY_PROFILES_BY_IDENTITY, capability_topics, drift_reason
from .optional_policy import locked_timeline, series_semantics
from .storage import Session, Storage, StorageUnavailable
from .timegrid import HOUR, floor_hour, purge_cutoff

log = logging.getLogger(__name__)

ROLL_HOURS_PER_TICK = 24
ACTIVITY_BACKFILL_HOURS_PER_STEP = 24
ACTIVITY_BACKFILL_SCAN_SECONDS = 7 * 24 * HOUR  # raw span one backfill discovery query examines
PURGE_HOURS_PER_STEP = 24  # at most 1440 minute rows deleted per step
PURGE_INTERVAL_SECONDS = HOUR
MAINTENANCE_RETRY_SECONDS = 60
# A backward CLOCK_REALTIME step is detected by comparing consecutive readings. Ordinary thread
# interleaving can also invert two readings — the MQTT and recorder threads sample the clock before
# they contend for the lock — but only by the lock wait, measured well under a second even under
# synthetic contention. A real NTP step is orders of magnitude larger, and a step below this
# threshold cannot meaningfully distort a 600-second freshness budget anyway.
CLOCK_STEP_BACK_SECONDS = 1.0


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


@dataclass(frozen=True)
class RecordedMinute:
    canonical: MinuteRow
    optional: OptionalMinute

    def __post_init__(self) -> None:
        if self.canonical.ts != self.optional.ts:
            raise ValueError("canonical/optional minute timestamp mismatch")
        object.__setattr__(self, "canonical", MinuteRow(
            self.canonical.ts, MappingProxyType(dict(self.canonical.values))))

    @property
    def ts(self) -> int:
        return self.canonical.ts


def _optional_rollup_values(folded: dict[int, OptionalStats]) -> list[tuple]:
    return [(sid, st.selected_minutes, st.known_minutes,
             st.v_sum, st.v_min, st.v_max, st.v_last)
            for sid, st in sorted(folded.items()) if st.selected_minutes]


def _fold_optional_hour(session: Session, hour_ts: int, minutes: list[int],
                        head_id: int) -> dict[int, OptionalStats]:
    timeline = locked_timeline(session, head_id, minutes)
    raw = dict(session.read_optional_minutes(hour_ts, hour_ts + HOUR, locking=True))
    return fold_optional_minutes(minutes, timeline, raw)


def rebuild_hour(session: Session, hour_ts: int) -> None:
    """Atomically replace canonical rollup, complete optional rollup and activity segments.

    All three come from the same locked canonical minutes of this one UTC hour,
    in the caller's transaction, so they commit together or not at all. Only
    ever called for an hour that stores minutes: replacing a rolled hour with
    an empty fold would delete evidence instead of correcting it.
    """
    head_id = session.lock_policy_head()
    rows = session.read_minutes(hour_ts, hour_ts + HOUR, locking=True)
    if not rows:
        raise ValueError(f"refusing to rebuild hour {iso_utc(hour_ts)} from no stored minutes")
    folded = fold_minutes(rows)
    optional = _fold_optional_hour(session, hour_ts, [ts for ts, _ in rows], head_id)
    session.replace_rollup_hour(hour_ts, [(k, s.n, s.sum, s.min, s.max, s.last)
                                          for k in SERIES if (s := folded.get(k)) is not None])
    session.replace_optional_rollup_hour(hour_ts, _optional_rollup_values(optional))
    session.replace_activity_hour(hour_ts, [segment_record(seg) for seg in build_segments(rows)])


def persist(storage: Storage, rows: list[RecordedMinute]) -> None:
    """Upsert minutes and rebuild every touched, already rolled hour in one transaction.

    A touched hour that is already rolled but whose raw evidence was already
    purged (a wall clock stepped back across a restart by more than raw
    retention) cannot be rebuilt from the newly arriving minute alone: doing
    so would replace its complete rollup with a partial one. Every such hour is
    found before the upsert, and the whole transaction is refused, leaving raw
    and rollup untouched. ``Session.first_purged_hour`` is the one fact that
    decides it, here and on the read path.
    """
    if any(not isinstance(r, RecordedMinute) for r in rows):
        raise TypeError("persist requires RecordedMinute pairs")
    with storage.session() as s:
        head_id = s.lock_policy_head()
        timeline = locked_timeline(s, head_id, [r.ts for r in rows])
        rolled_until = s.lock_rolled_until()
        touched = (sorted({floor_hour(r.ts) for r in rows if r.ts < rolled_until})
                  if rolled_until is not None else [])
        # Current reads: a purge may have committed while this transaction waited for the head lock.
        refused = [h for h in touched if s.first_purged_hour(h, h + HOUR, locking=True) is not None]
        if refused:
            raise RebuildRefused(
                f"hour(s) {', '.join(iso_utc(h) for h in refused)} are rolled but their raw evidence"
                " was already purged; refusing to rebuild them from a new partial write", refused)
        s.upsert_minutes([r.canonical for r in rows])
        topics = capability_topics()
        for pair in rows:
            values = {}
            for member in timeline[pair.ts]:
                if drift_reason(member.identity, member.expected_topic, member.profile_version,
                                HISTORY_PROFILES_BY_IDENTITY, topics,
                                persisted_semantics=series_semantics(member)) is not None:
                    continue
                value = pair.optional.values.get(member.identity)
                if value is not None and math.isfinite(value):
                    values[str(member.id)] = value
            s.replace_optional_minute(pair.ts, values)
        for hour_ts in touched:
            rebuild_hour(s, hour_ts)


def roll_next_hour(storage: Storage, closed_before: int) -> int | None:
    """Roll the first stored hour at or above ``rolled_until`` if it ends by ``closed_before``."""
    with storage.session() as s:
        s.lock_policy_head()
        rolled_until = s.lock_rolled_until()
        first = s.lock_first_minute_at_or_after(0 if rolled_until is None else rolled_until)
        if first is None or floor_hour(first) + HOUR > closed_before:
            return None
        rebuild_hour(s, floor_hour(first))
        return floor_hour(first)


def backfill_activity_step(storage: Storage, scan_from: int, max_hours: int) -> tuple[int, list[int], bool]:
    """Materialize activity for rolled hours that store raw minutes but no segments yet.

    Such hours exist only where rolling happened before Stage 4C-B, so no
    watermark records them: ``Session.unmaterialized_activity_hours`` derives
    them from the tables. Each is rebuilt through ``rebuild_hour``, so its
    canonical, optional and activity representations stay one atomic result.
    Hours whose raw was already purged have no raw minutes and are never
    candidates: activity is never fabricated for them.

    ``scan_from`` is an in-memory scan hint only. Returns ``(next scan_from,
    rebuilt hours, done)``; ``done`` means no rolled hour at or above
    ``scan_from`` lacks segments.
    """
    with storage.session() as s:
        s.lock_policy_head()
        rolled_until = s.lock_rolled_until()
        first = s.lock_first_minute_at_or_after(scan_from)
        if rolled_until is None or first is None or first >= rolled_until:
            return scan_from, [], True
        start = floor_hour(first)
        end = min(rolled_until, start + ACTIVITY_BACKFILL_SCAN_SECONDS)
        # A snapshot read, older than the head lock. Safe: ``start`` is a current read of the
        # oldest raw minute and purge deletes only a prefix, so nothing purged meanwhile is in
        # range; an hour materialized meanwhile is only rebuilt again, idempotently.
        hours = s.unmaterialized_activity_hours(start, end, max_hours)
        for hour_ts in hours:
            rebuild_hour(s, hour_ts)
        if len(hours) == max_hours:
            return hours[-1] + HOUR, hours, False
        return end, hours, end >= rolled_until


def purge_step(storage: Storage, now: float, retention_days: int, pending_from: int | None,
               max_hours: int) -> tuple[int | None, int, bool]:
    """Delete at most ``max_hours`` whole hours of raw minutes below the safe cutoff.

    Returns ``(cutoff, deleted rows, more to delete)``; ``cutoff`` is ``None``
    when nothing may be purged. Raises ``PurgeRefused`` (deleting nothing)
    when the canonical rollup, optional rollup or activity segments fail their
    raw-evidence proof. Lock order is the one every writer uses: policy head,
    rolled frontier, canonical raw, then derived tables.
    """
    with storage.session() as s:
        head_id = s.lock_policy_head()
        cutoff = purge_cutoff(now, s.lock_rolled_until(), retention_days)
        if cutoff is None:
            return None, 0, False
        if pending_from is not None:
            cutoff = min(cutoff, floor_hour(pending_from))
        oldest = s.lock_oldest_minute_ts()
        if oldest is None or oldest >= cutoff:
            return cutoff, 0, False
        first = floor_hour(oldest)
        end = min(cutoff, first + max_hours * HOUR)
        locked_rows = s.read_minutes(first, end, locking=True)
        candidates = [ts for ts, _ in locked_rows]
        counts: dict[int, int] = {}
        for ts in candidates:
            hour = floor_hour(ts)
            counts[hour] = counts.get(hour, 0) + 1
        rolled_counts = s.lock_rollup_counts(first, end, RECORDED)
        for hour_ts, stored in sorted(counts.items()):
            rolled = rolled_counts.get(hour_ts)
            if rolled != stored:
                raise PurgeRefused(f"rollup of hour {iso_utc(hour_ts)} accounts for {rolled or 0}"
                                   f" of {stored} stored minutes")
        try:
            timeline = locked_timeline(s, head_id, candidates)
            raw = dict(s.read_optional_minutes(first, end, locking=True))
            stored = {(h, sid): (selected, known, v_sum, v_min, v_max, v_last)
                      for h, sid, selected, known, v_sum, v_min, v_max, v_last
                      in s.read_optional_rollup(first, end, locking=True)}
            expected = {}
            for hour_ts in range(first, end, HOUR):
                hour_minutes = [ts for ts in candidates if hour_ts <= ts < hour_ts + HOUR]
                hour_raw = {ts: values for ts, values in raw.items()
                            if hour_ts <= ts < hour_ts + HOUR}
                folded = fold_optional_minutes(hour_minutes, timeline, hour_raw)
                for sid, selected, known, v_sum, v_min, v_max, v_last in _optional_rollup_values(folded):
                    expected[(hour_ts, sid)] = (selected, known, v_sum, v_min, v_max, v_last)
            if expected != stored:
                raise PurgeRefused("optional rollup does not match selected/known raw evidence")
        except OptionalHistoryInconsistent as e:
            raise PurgeRefused(f"optional raw evidence is inconsistent: {e}") from e
        _prove_activity(s, locked_rows, first, end)
        s.delete_optional_minutes(first, end)
        return cutoff, s.delete_minutes_before(end), end < cutoff


def _prove_activity(session: Session, rows: list, first: int, end: int) -> None:
    """Stored activity segments must equal the segments of the locked raw minutes, hour by hour.

    Exact equality of every field (start, minutes, activity, compressor,
    defrost fraction, rule version via decoding, every energy statistic), not
    a minute count: raw is the only source that could ever repair them.
    """
    try:
        stored = decode_segments(session.read_activity_segments(first, end, locking=True))
    except ActivityRecordInvalid as e:
        raise PurgeRefused(f"stored activity segments are invalid: {e}") from e
    for hour_ts in range(first, end, HOUR):
        expected = build_segments([r for r in rows if hour_ts <= r[0] < hour_ts + HOUR])
        actual = [seg for seg in stored if hour_ts <= seg.start < hour_ts + HOUR]
        if actual != expected:
            raise PurgeRefused(f"activity segments of hour {iso_utc(hour_ts)} do not match its raw minutes"
                               f" ({len(actual)} stored, {len(expected)} expected)")


def physical_reading_dict(reading: PhysicalReading, *, now: float, connected: bool,
                           lwt_offline: bool, stale_after: int) -> dict:
    """Serialize a physical receipt: ``mode`` is provenance, ``available`` is a current fact.

    ``available`` is true only for a fresh non-retained receipt in a currently
    connected, non-``Offline`` epoch, using the same ``stale_after`` boundary as
    canonical freshness. It is a live-surface fact only; it does not imply
    Stage 4B history eligibility for this physical identity.
    """
    payload = reading.payload
    mode = "none" if payload is None else ("retained" if reading.retained else "live")
    available = (
        mode == "live"
        and connected
        and not lwt_offline
        and reading.received_at is not None
        and now - reading.received_at <= stale_after
    )
    return {
        "topic": reading.topic,
        "value": payload.value if payload else None,
        "kind": payload.kind if payload else None,
        "raw": payload.raw if payload else None,
        "mode": mode,
        "available": available,
        "received_at": iso_utc(reading.received_at),
    }


class Recorder:
    def __init__(self, ingest: Ingest, accumulator: MinuteAccumulator, storage: Storage, buffer_rows: int,
                 retention_days: int = 365):
        self.ingest = ingest
        self.accumulator = accumulator
        self.optional_accumulator = OptionalAccumulator(ingest, accumulator.process_start)
        self.storage = storage
        self.buffer_rows = buffer_rows
        self._lock = threading.Lock()
        self._waiting: deque[RecordedMinute] = deque()
        self._protected: list[RecordedMinute] = []
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
        # Stage 4C-B backfill of rolled hours recorded before activity segments existed. In memory
        # only: derived again from the tables after every process start.
        self._activity_scan_from = 0
        self._activity_backfilled = False
        self._activity_retry_at = float("-inf")
        self.activity_backfill_error: str | None = None
        self.last_rolled_hour: int | None = None
        self.last_rolled_at: float | None = None
        self.rollup_error: str | None = None
        self.rollup_error_at: float | None = None
        # Previous raw CLOCK_REALTIME reading, for detecting a backward step (see ``_advance``).
        self._last_wall = accumulator.process_start
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
            self._advance(t)
            self.ingest.connect(t)

    def on_disconnect(self, t: float) -> None:
        with self._lock:
            self._advance(t)
            self.ingest.disconnect(t)

    def on_lwt(self, payload: str, retained: bool, t: float) -> None:
        with self._lock:
            self._advance(t)
            self.ingest.lwt_message(payload, retained, t)

    def on_message(self, topic: str, payload: str, retained: bool, t: float) -> None:
        with self._lock:
            self._advance(t)
            self.ingest.message(topic, payload, retained, t)

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

    def _write(self, batch: list[RecordedMinute], now: float) -> WriteOutcome:
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

    def _refuse(self, batch: list[RecordedMinute], error: RebuildRefused, now: float) -> None:
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
        self._backfill_activity(now)
        if self._activity_backfilled:  # purge never outruns activity materialization
            self._purge(now, min(pending) if pending else None)

    def _roll(self, now: float, closed_before: int, written: int) -> None:
        """Roll closed hours in ascending order; stop at the first failure (contiguity)."""
        key = (closed_before, written)
        if key == self._roll_done_key:
            return  # nothing written and no hour closed since the last complete pass
        for _ in range(ROLL_HOURS_PER_TICK):
            try:
                hour_ts = roll_next_hour(self.storage, closed_before)
            except (StorageUnavailable, OptionalHistoryInconsistent) as e:
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

    def _backfill_activity(self, now: float) -> None:
        """One bounded step per tick until no rolled raw hour lacks activity segments."""
        if self._activity_backfilled or now < self._activity_retry_at:
            return
        try:
            scan_from, hours, done = backfill_activity_step(
                self.storage, self._activity_scan_from, ACTIVITY_BACKFILL_HOURS_PER_STEP)
        except (StorageUnavailable, OptionalHistoryInconsistent) as e:
            if self.activity_backfill_error is None:
                log.warning("activity backfill failed, retried later: %s", e)
            self.activity_backfill_error = str(e)
            self._activity_retry_at = now + MAINTENANCE_RETRY_SECONDS
            return
        self.activity_backfill_error = None
        self._activity_scan_from, self._activity_backfilled = scan_from, done
        if hours:
            log.info("materialized activity segments for %d rolled hour(s) up to %s",
                     len(hours), iso_utc(hours[-1]))

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

    def live(self, clock: Callable[[], float], include_readings: bool = False) -> dict:
        """Canonical live metric state for ``/api/v1/live``.

        ``clock`` is read *inside* the lock, so the observation instant belongs
        to the same atomic observation as the state it describes: a message the
        MQTT thread applies while an API thread waits for the lock is either
        wholly in the response, with a ``now`` at or after its receipt, or not
        in it at all. Sampling the clock first would let a later receipt appear
        under an earlier ``now`` without any wall-clock reversal.

        ``now`` is the raw clock, the same reading freshness is measured
        against, so ``alive`` and every ``mode`` in this response describe real
        elapsed time. It is deliberately not floored at the accumulator's
        cursor: doing that would mix a pre-step observation instant with
        post-step receipt timestamps and make a just-received message look
        stale for the size of the step. A backward step instead discards the
        stamps taken before it (see ``_advance``), which is what keeps the two
        operands of every freshness subtraction on one side of the correction.

        No database I/O and no state change: minutes are closed by ``tick``,
        never by an API thread.
        """
        with self._lock:
            now = clock()
            ing = self.ingest
            body = {
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
            if include_readings:
                body["readings"] = {
                    reading.identity: physical_reading_dict(
                        reading, now=now, connected=ing.connected,
                        lwt_offline=ing.lwt == LWT_OFFLINE, stale_after=ing.stale_after,
                    )
                    for reading in ing.physical_snapshot()
                }
            return body

    def physical_readings(self) -> tuple[PhysicalReading, ...]:
        """Copy immutable physical readings under the MQTT/tick snapshot lock."""
        with self._lock:
            return self.ingest.physical_snapshot()

    def safe_future_minute(self, now: float) -> int:
        """Stage 4B checkpoint B (§25.2.1): the earliest future whole minute a policy change
        may affect, as of ``now``.

        Read-only: takes the recorder lock, performs no database I/O, closes
        no minute and mutates no accumulator state. Only a caller resolving a
        policy PUT needs this; it must be called and released *before* any
        database transaction begins, never while holding a database lock.

        The open minute's own cursor can be temporarily ahead of (a queued
        backlog) or behind (a detected backward clock step, ARCHITECTURE.md
        §24) the raw wall clock, so the safe boundary is the later of the next
        whole minute after the raw clock and the minute after the
        accumulator's currently open one.
        """
        with self._lock:
            return max(floor_minute(now) + MINUTE, self.accumulator.minute_start + MINUTE)

    def snapshot(self, clock: Callable[[], float]) -> tuple[float, dict]:
        """Factual in-memory state for ``/api/v1/status``, with its observation instant.

        Like ``live``, ``clock`` is read raw inside the lock: ``now``, the
        freshness of every source and the ``alive`` verdict are one
        observation on one clock. The instant is returned because the caller
        needs it for facts computed outside this lock, such as the prospective
        purge cutoff — which ``Recorder._purge`` derives from the raw tick
        clock, so reporting anything else here would describe a cutoff purge
        will not use.
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
                    "clock_steps": ing.clock_steps,
                    "last_clock_step_at": iso_utc(ing.last_clock_step_at),
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

        This clamp exists only for the accumulator's own monotonic sequencing
        (segment integration, minute closing, upsert-by-``ts`` safety): after a
        wall-clock step backwards, no minute closes until real time passes the
        cursor again, so the result is a gap, never a duplicate. Callers must
        still feed ``Ingest`` the *raw*, unclamped ``t`` for freshness purposes
        (``on_connect``/``on_disconnect``/``on_lwt``/``on_message`` do this) —
        clamping a source's confirmed-evidence timestamp to a cursor that is
        temporarily ahead of the wall clock would inflate how long that
        evidence counts as fresh by the size of the clock step.

        Two consecutive readings that go backwards by more than
        ``CLOCK_STEP_BACK_SECONDS`` are a real backward step, and every
        confirmed timestamp taken before it is discarded
        (``Ingest.clock_stepped_back``). The comparison is against the previous
        reading, not against the cursor: the cursor stays ahead for the whole
        replayed interval, so comparing against it would re-discard the
        evidence of every message arriving in that interval and black the
        sources out until the wall clock caught up. Detection runs before the
        caller applies its event, so a message that carries the step forward
        immediately re-establishes its own source.

        The same step also poisons the accumulator's open minute
        (``MinuteAccumulator.discard_open``): a minute that already integrated
        any pre-correction state must never be combined with post-correction
        evidence in one row, so it becomes a gap instead. An empty open minute
        (nothing integrated yet) is left usable.
        """
        if t < self._last_wall - CLOCK_STEP_BACK_SECONDS:
            log.warning("CLOCK_REALTIME stepped back %.3fs to %s; confirmed source evidence discarded",
                        self._last_wall - t, iso_utc(t))
            self.ingest.clock_stepped_back(t)
            self.accumulator.discard_open()
            self.optional_accumulator.discard_open()
        self._last_wall = t
        t = max(t, self.accumulator.cursor)
        optional = {row.ts: row for row in self.optional_accumulator.advance(t)}
        for row in self.accumulator.advance(t):
            self.rows_closed += 1
            self.last_row_minute = row.ts
            self._waiting.append(RecordedMinute(row, optional.get(row.ts, OptionalMinute(row.ts, {}))))
            if len(self._waiting) > self.buffer_rows:
                lost = self._waiting.popleft()
                self.dropped_rows += 1
                log.warning("waiting queue full, dropped never-submitted minute %s", iso_utc(lost.ts))
        return t
