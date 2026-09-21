"""Canonical minute accumulation: timestamped ingest state → ``MinuteRow``.

Time is walked forward in segments that break at events, at freshness expiries
and at minute boundaries. Inside a segment the historical state is constant, so
mean metrics are exact time-weighted integrals and ``last`` is the value of the
final segment of the minute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .catalog import RECORDED, RECORDED_KEYS
from .ingest import Ingest

MINUTE = 60


@dataclass(frozen=True)
class MinuteRow:
    ts: int  # UTC Unix seconds, ts % 60 == 0
    values: dict[str, float | None]  # every recorded metric; None = unknown in a recorded minute


def floor_minute(t: float) -> int:
    return math.floor(t / MINUTE) * MINUTE


def iso_utc(t: float | None) -> str | None:
    """API timestamp form: ISO 8601 UTC with ``Z``."""
    if t is None:
        return None
    dt = datetime.fromtimestamp(t, timezone.utc)
    spec = "seconds" if dt.microsecond == 0 else "milliseconds"
    return dt.isoformat(timespec=spec).replace("+00:00", "Z")


class MinuteAccumulator:
    """Closes minutes from ``Ingest`` state.

    The caller must call ``advance(t)`` before applying an event at ``t`` to the
    ingest state, and must never apply events earlier than ``cursor``.
    """

    def __init__(self, ingest: Ingest, process_start: float):
        self.ingest = ingest
        self.process_start = process_start
        self.cursor = process_start
        self.last_closed_minute: int | None = None
        self._open(floor_minute(process_start))

    def advance(self, t: float) -> list[MinuteRow]:
        rows: list[MinuteRow] = []
        while self.cursor < t:
            end = self.minute_start + MINUTE
            if not self.ingest.alive_at(self.cursor) and t >= end:
                # Without source life no row can close before the next event,
                # which is at or after t: skip whole minutes in one step.
                self.last_closed_minute = floor_minute(t) - MINUTE
                self.cursor = t
                self._open(floor_minute(t))
                continue
            stop = min(t, end)
            expiry = self.ingest.next_expiry_after(self.cursor)
            if expiry is not None and expiry < stop:
                stop = expiry
            self._integrate(self.cursor, stop)
            self.cursor = stop
            if stop == end:
                row = self._close()
                if row is not None:
                    rows.append(row)
                self._open(end)
        return rows

    def _open(self, start: int) -> None:
        self.minute_start = start
        # A minute entered after its start (process start, skipped gap) cannot
        # have a complete mean; the row rule also refuses it.
        complete = self.cursor == start
        self._sum = {k: 0.0 for k in RECORDED_KEYS}
        self._whole = {k: complete for k in RECORDED_KEYS}
        self._end_value: dict[str, float | None] = {k: None for k in RECORDED_KEYS}
        self._valid = True

    def discard_open(self) -> None:
        """Poison the open minute after a detected clock discontinuity.

        A minute that already integrated any pre-correction state can never be
        combined with post-correction evidence on one timeline, so it must
        never become a ``MinuteRow``: not a null metric, not a recalculation,
        a whole discarded minute. A minute that has not integrated anything
        yet (``cursor == minute_start``) holds no pre-correction state, so it
        is left usable and may still close normally from fresh evidence.
        """
        if self.cursor != self.minute_start:
            self._valid = False

    def _integrate(self, a: float, b: float) -> None:
        dt = b - a
        if dt <= 0:
            return
        for key in RECORDED_KEYS:
            s = self.ingest.historical(key, a)
            if s is None:
                self._whole[key] = False
                self._end_value[key] = None
            else:
                self._sum[key] += s.value * dt
                self._end_value[key] = s.value

    def _close(self) -> MinuteRow | None:
        m = self.minute_start
        self.last_closed_minute = m  # sequencing fact, independent of the minute's own validity
        if not self._valid:
            return None  # poisoned by a detected clock discontinuity: a gap, never a mixed row
        if m < self.process_start:
            return None  # never a minute beginning before process start
        if not self.ingest.alive_through(m, m + MINUTE):
            return None
        values: dict[str, float | None] = {}
        for metric in RECORDED:
            k = metric.key
            if metric.kind == "mean":
                # Rounding only removes float noise from segment sums.
                values[k] = round(self._sum[k] / MINUTE, 6) if self._whole[k] else None
            else:
                values[k] = self._end_value[k]
        return MinuteRow(ts=m, values=values)
