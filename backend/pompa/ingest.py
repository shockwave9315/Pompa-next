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

from .capabilities import TypedPayload, effective_capabilities, normalize_payload
from .catalog import METRICS, SOURCE_BY_TOPIC, Metric, Outcome, Source, parse_value
from .history_profile import HISTORY_PROFILES, HistoryProfile, parse_history_profile_value

log = logging.getLogger(__name__)

LWT_OFFLINE = "Offline"
MAX_UNCATALOGUED_TOPICS = 500


@dataclass(frozen=True)
class LiveValue:
    """One metric's current canonical value and where it came from.

    ``mode`` is protocol provenance, not a health verdict:

    * ``live``: a confirmed fresh non-retained source of the current epoch.
    * ``retained``: a cached retained delivery, exposed only because no
      confirmed source exists. It never entered history.
    * ``none``: nothing current to show; every field is ``None``.
    """

    value: float | None
    mode: str
    source_id: str | None
    source_topic: str | None
    received_at: float | None


NO_LIVE = LiveValue(None, "none", None, None, None)


@dataclass(frozen=True, slots=True)
class PhysicalReading:
    identity: str
    topic: str | None
    payload: TypedPayload | None = None
    received_at: float | None = None
    retained: bool | None = None


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
    # Measurement only (never read by history). Counts and first/latest are
    # process-lifetime. A publication gap is sampled only between consecutive
    # non-retained messages inside one observation interval, which ends at MQTT
    # disconnect/reconnect and at LWT Offline; the first message of an interval
    # is only its baseline.
    live_messages: int = 0
    retained_messages: int = 0
    sentinel_messages: int = 0
    rejected_messages: int = 0
    first_live_at: float | None = None
    latest_live_at: float | None = None
    gap_baseline_at: float | None = None
    gap_count: int = 0
    gap_sum: float = 0.0
    max_live_gap: float | None = None


@dataclass
class OptionalSourceState:
    profile: HistoryProfile
    seen_live: bool = False
    value: float | None = None
    last_live_at: float | None = None


class Ingest:
    def __init__(self, stale_after: int):
        self.stale_after = stale_after
        self.physical_readings: dict[str, PhysicalReading] = {}
        self._physical_by_topic: dict[str, str] = {}
        for capability in effective_capabilities():
            reference = capability.reference
            if not capability.readable:
                continue  # SET/PCB command topics are never physical readings
            # Documented topics, core Source paths, and exact verified XTOP overrides
            # are resolved once by the effective capability model.
            topic = capability.topic
            self.physical_readings[reference.identity] = PhysicalReading(reference.identity, topic)
            if topic is not None:
                if topic in self._physical_by_topic:
                    raise ValueError(f"Readable capability topic mapped twice: {topic}")
                self._physical_by_topic[topic] = reference.identity
        self.sources: dict[str, SourceState] = {
            topic: SourceState(metric, source) for topic, (metric, source) in SOURCE_BY_TOPIC.items()
        }
        self.optional_sources: dict[str, OptionalSourceState] = {
            p.expected_topic: OptionalSourceState(p) for p in HISTORY_PROFILES
        }
        if len(self.optional_sources) != len(HISTORY_PROFILES):
            raise ValueError("duplicate optional history topic")
        self._optional_by_identity = {s.profile.identity: s for s in self.optional_sources.values()}
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
        self.clock_steps = 0  # detected backward CLOCK_REALTIME steps; each discarded confirmed evidence
        self.last_clock_step_at: float | None = None
        self.readings_generation = 0  # increases whenever physical receipt continuity is reset

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
            s.gap_baseline_at = None
        for s in self.optional_sources.values():
            s.seen_live = False
            s.value = None
            s.last_live_at = None
        self._clear_physical_readings()
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
        identity = self._physical_by_topic.get(topic)
        if identity is not None:
            self.physical_readings[identity] = PhysicalReading(
                identity, topic, normalize_payload(payload), t, retained
            )
        optional = self.optional_sources.get(topic)
        if optional is not None and not retained:
            optional.value, _ = parse_history_profile_value(optional.profile, payload)
            optional.seen_live = True
            optional.last_live_at = t
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
        s.latest_live_at = t
        if s.gap_baseline_at is not None and t > s.gap_baseline_at:
            # A non-positive delta means a backward wall-clock step landed between two live
            # messages of this source; that is not a publication gap sample, so it is skipped
            # rather than polluting the Stage 1 evidence trail with a negative or zero "gap".
            gap = t - s.gap_baseline_at
            s.gap_count += 1
            s.gap_sum += gap
            if s.max_live_gap is None or gap > s.max_live_gap:
                s.max_live_gap = gap
        s.gap_baseline_at = t
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

    def live(self, metric_key: str, t: float) -> LiveValue:
        """Canonical current value: a confirmed source, else a retained cache, else nothing.

        The confirmed path *is* ``historical``, so live display and historical
        accumulation can never disagree about source priority, validity or
        freshness. Only when it selects nothing may a retained delivery be
        shown, labelled as such: it still proves no source life, sets no
        ``seen_live`` and enters no minute. A stale non-retained value is not a
        fallback — the latest delivery of a source is either its retained cache
        or it is not.
        """
        s = self.historical(metric_key, t)
        if s is not None:
            return LiveValue(s.value, "live", s.source.id, s.source.topic, s.last_live_at)
        for s in self._by_metric[metric_key]:
            if s.last_retained and s.last_outcome is Outcome.VALID and s.last_value is not None:
                return LiveValue(s.last_value, "retained", s.source.id, s.source.topic, s.last_received_at)
        return NO_LIVE

    def live_snapshot(self, t: float) -> dict[str, LiveValue]:
        """Every catalog metric exactly once, in catalog order."""
        return {m.key: self.live(m.key, t) for m in METRICS}

    def physical_snapshot(self) -> tuple[PhysicalReading, ...]:
        """All readable slots in reference order, including absent/unresolved ones."""
        return tuple(self.physical_readings.values())

    def next_expiry_after(self, t: float) -> float | None:
        """Earliest moment after ``t`` at which a historical value stops being fresh."""
        expiries = [
            s.last_live_at + self.stale_after
            for s in self.sources.values()
            if s.value is not None and s.last_live_at + self.stale_after > t
        ]
        return min(expiries, default=None)

    def optional_historical(self, identity: str, t: float) -> OptionalSourceState | None:
        if not self.connected or self.lwt == LWT_OFFLINE:
            return None
        profile = self._optional_by_identity.get(identity)
        if (profile is not None and profile.seen_live and profile.value is not None
                and t < profile.last_live_at + self.stale_after):
            return profile
        return None

    def optional_next_expiry_after(self, t: float) -> float | None:
        expiries = [s.last_live_at + self.stale_after for s in self.optional_sources.values()
                    if s.value is not None and s.last_live_at is not None
                    and s.last_live_at + self.stale_after > t]
        return min(expiries, default=None)

    # ------------------------------------------------------------------ internals

    def clock_stepped_back(self, t: float) -> None:
        """A backward ``CLOCK_REALTIME`` step: every confirmed timestamp predates the correction.

        Freshness is an elapsed-time question, so both of its operands must be
        readings of the same clock on the same side of a correction. A backward
        step invalidates that for every stamp taken before it: those stamps
        belong to a timeline the system has just been told was wrong, and
        subtracting a post-step ``now`` from one of them understates the age by
        the size of the step. They are therefore discarded exactly as a
        disconnect discards them — the sources need new non-retained evidence,
        which their next ordinary message supplies. ``seen_live`` and the
        connection epoch are untouched: the broker connection did not change.
        Retained provenance is untouched too; it never measured freshness.
        """
        self.clock_steps += 1
        self.last_clock_step_at = t
        self._invalidate()

    def _invalidate(self) -> None:
        """Disconnect/Offline/clock step: values and source life need new non-retained evidence."""
        self._clear_physical_readings()
        for s in self.sources.values():
            s.value = None
            s.gap_baseline_at = None
        for s in self.optional_sources.values():
            s.value = None
        self.last_live_at = None
        self.alive_since = None

    def _clear_physical_readings(self) -> None:
        self.readings_generation += 1
        for identity, reading in self.physical_readings.items():
            self.physical_readings[identity] = PhysicalReading(identity, reading.topic)

    def _expire(self, t: float) -> None:
        """Lazily end an alive interval that went stale before ``t``."""
        if self.last_live_at is not None and t - self.last_live_at > self.stale_after:
            self.alive_since = None

    def _refresh_alive(self, t: float) -> None:
        if not self.alive_at(t):
            self.alive_since = None
        elif self.alive_since is None:
            self.alive_since = t
