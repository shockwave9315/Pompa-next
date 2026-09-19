"""Ingest: connection epochs, LWT, per-physical-source state and source selection.

All methods take explicit receive timestamps (UTC Unix seconds, float) and are
not thread-safe; the recorder serialises access. Freshness boundaries:

* ``alive_at(t)`` follows the architecture literally: ``t - last_live <= STALE``.
* Historical values are valid on the half-open interval
  ``[received, received + STALE)``. For any half-open minute this is equivalent
  to the architecture's row rule ``M + 60 - last_live <= STALE``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .catalog import METRICS, SOURCE_BY_TOPIC, Metric, Outcome, Source, parse_value

log = logging.getLogger(__name__)

LWT_OFFLINE = "Offline"
MAX_UNCATALOGUED_TOPICS = 500


@dataclass
class SourceState:
    metric: Metric
    source: Source
    # Historical evidence; reset at every connection epoch.
    seen_live: bool = False
    value: float | None = None  # from the latest non-retained message; None = unknown
    last_live_at: float | None = None
    # Latest message of any kind (live cache; retained values are labelled).
    last_value: float | None = None
    last_outcome: Outcome | None = None
    last_received_at: float | None = None
    last_retained: bool | None = None
    # Cumulative measurement facts for the whole process lifetime.
    live_messages: int = 0
    retained_messages: int = 0
    sentinel_messages: int = 0
    rejected_messages: int = 0
    first_live_at: float | None = None
    max_live_gap: float | None = None  # between consecutive non-retained messages in one epoch


class Ingest:
    def __init__(self, stale_after: float):
        self.stale_after = float(stale_after)
        self.sources: dict[str, SourceState] = {
            topic: SourceState(metric, source) for topic, (metric, source) in SOURCE_BY_TOPIC.items()
        }
        self._by_metric: dict[str, list[SourceState]] = {
            m.key: [self.sources[s.topic] for s in m.sources] for m in METRICS
        }
        self.connected = False
        self.epoch = 0
        self.connects = 0
        self.disconnects = 0
        self.connected_at: float | None = None
        self.disconnected_at: float | None = None
        self.lwt: str | None = None  # as received in the current epoch
        self.lwt_retained: bool | None = None
        self.lwt_at: float | None = None
        self.lwt_messages = 0
        self.last_live_at: float | None = None  # life evidence: non-retained, current epoch, not Offline
        self.latest_live_message_at: float | None = None  # never reset; reporting only
        self.alive_since: float | None = None
        self.parse_rejects = 0
        self.uncatalogued_topics: set[str] = set()

    # ------------------------------------------------------------------ events

    def connect(self, t: float) -> None:
        """A successful MQTT connection starts a new epoch; nothing carries over."""
        self.epoch += 1
        self.connects += 1
        self.connected = True
        self.connected_at = t
        self.lwt = self.lwt_retained = self.lwt_at = None
        for s in self.sources.values():
            s.seen_live = False
            s.value = None
            s.last_live_at = None
        self.last_live_at = None
        self.alive_since = None

    def disconnect(self, t: float) -> None:
        if not self.connected:
            return  # e.g. a failed connect attempt: no epoch ended
        self.connected = False
        self.disconnects += 1
        self.disconnected_at = t
        self._invalidate()

    def lwt_message(self, payload: str, retained: bool, t: float) -> None:
        state = payload.strip()
        self.lwt, self.lwt_retained, self.lwt_at = state, retained, t
        self.lwt_messages += 1
        log.info("LWT %r retained=%s epoch=%d", state, retained, self.epoch)
        if state == LWT_OFFLINE:
            self._invalidate()
        self._refresh_alive(t)

    def message(self, topic: str, payload: str, retained: bool, t: float) -> None:
        """``topic`` is relative to the configured prefix, e.g. ``main/Pump_Flow``."""
        s = self.sources.get(topic)
        if s is None:
            if len(self.uncatalogued_topics) < MAX_UNCATALOGUED_TOPICS:
                self.uncatalogued_topics.add(topic)
            return

        value, outcome = parse_value(s.metric, s.source, payload)
        if outcome is Outcome.REJECTED:
            s.rejected_messages += 1
            self.parse_rejects += 1
        elif outcome is Outcome.SENTINEL:
            s.sentinel_messages += 1
        s.last_value, s.last_outcome, s.last_received_at, s.last_retained = value, outcome, t, retained

        if retained:
            # Retained delivery is live-cache only: never seen_live, freshness,
            # source life or history.
            s.retained_messages += 1
            return

        self._expire(t)
        s.live_messages += 1
        if s.first_live_at is None:
            s.first_live_at = t
        if s.last_live_at is not None:
            gap = t - s.last_live_at
            if s.max_live_gap is None or gap > s.max_live_gap:
                s.max_live_gap = gap
        s.seen_live = True
        s.value = value
        s.last_live_at = t
        self.last_live_at = t
        self.latest_live_message_at = t
        self._refresh_alive(t)

    # ------------------------------------------------------------------ queries

    def alive_at(self, t: float) -> bool:
        return (
            self.connected
            and self.lwt != LWT_OFFLINE
            and self.last_live_at is not None
            and t - self.last_live_at <= self.stale_after
        )

    def alive_through(self, start: float, end: float) -> bool:
        """Source observed alive for all of ``[start, end)``.

        Only meaningful with the state as of ``end``: every event before ``end``
        applied and none at or after it.
        """
        return (
            self.alive_since is not None
            and self.alive_since <= start
            and self.alive_at(end)
        )

    def historical(self, metric_key: str, t: float) -> SourceState | None:
        """First source in priority order that is seen_live, valid and fresh at ``t``."""
        if not self.connected or self.lwt == LWT_OFFLINE:
            return None
        for s in self._by_metric[metric_key]:
            if s.seen_live and s.value is not None and t < s.last_live_at + self.stale_after:
                return s
        return None

    def next_expiry_after(self, t: float) -> float | None:
        """Earliest moment after ``t`` at which a historical value stops being fresh."""
        expiries = [
            s.last_live_at + self.stale_after
            for s in self.sources.values()
            if s.value is not None and s.last_live_at + self.stale_after > t
        ]
        return min(expiries, default=None)

    # ------------------------------------------------------------------ internals

    def _invalidate(self) -> None:
        """Disconnect/Offline: values and source life need new non-retained evidence."""
        for s in self.sources.values():
            s.value = None
        self.last_live_at = None
        self.alive_since = None

    def _expire(self, t: float) -> None:
        """Lazily end an alive interval that went stale before ``t``."""
        if self.last_live_at is not None and t - self.last_live_at > self.stale_after:
            self.alive_since = None

    def _refresh_alive(self, t: float) -> None:
        if not self.alive_at(t):
            self.alive_since = None
        elif self.alive_since is None:
            self.alive_since = t
