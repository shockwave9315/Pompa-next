"""Independent optional history accumulation over continuously observed profiles."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .history_profile import HISTORY_PROFILES
from .ingest import Ingest
from .minute import MINUTE, floor_minute


@dataclass(frozen=True)
class OptionalMinute:
    ts: int
    values: Mapping[str, float | None]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


class OptionalAccumulator:
    """Own cursor, expiry walk and minute facts; never participates in canonical segmentation."""

    def __init__(self, ingest: Ingest, process_start: float):
        self.ingest = ingest
        self.process_start = process_start
        self.cursor = process_start
        self._open(floor_minute(process_start))

    def _open(self, start: int) -> None:
        self.minute_start = start
        complete = self.cursor == start
        self._sum = {p.identity: 0.0 for p in HISTORY_PROFILES}
        self._whole = {p.identity: complete for p in HISTORY_PROFILES}
        self._end_value = {p.identity: None for p in HISTORY_PROFILES}
        self._valid = True

    def discard_open(self) -> None:
        if self.cursor != self.minute_start:
            self._valid = False

    def advance(self, t: float) -> list[OptionalMinute]:
        rows: list[OptionalMinute] = []
        while self.cursor < t:
            end = self.minute_start + MINUTE
            if (self.cursor == self.minute_start
                    and not any(self.ingest.optional_historical(p.identity, self.cursor)
                                for p in HISTORY_PROFILES) and t >= end):
                self.cursor = t
                self._open(floor_minute(t))
                continue
            stop = min(t, end)
            expiry = self.ingest.optional_next_expiry_after(self.cursor)
            if expiry is not None and expiry < stop:
                stop = expiry
            dt = stop - self.cursor
            if dt > 0:
                for p in HISTORY_PROFILES:
                    source = self.ingest.optional_historical(p.identity, self.cursor)
                    if source is None:
                        self._whole[p.identity] = False
                        self._end_value[p.identity] = None
                    else:
                        self._sum[p.identity] += source.value * dt
                        self._end_value[p.identity] = source.value
            self.cursor = stop
            if stop == end:
                if self._valid and self.minute_start >= self.process_start:
                    values = {
                        p.identity: (round(self._sum[p.identity] / MINUTE, 6)
                                     if self._whole[p.identity] else None)
                        if p.kind == "mean" else self._end_value[p.identity]
                        for p in HISTORY_PROFILES
                    }
                    rows.append(OptionalMinute(self.minute_start, values))
                self._open(end)
        return rows
