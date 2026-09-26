"""Stage 4E control runtime (docs/ARCHITECTURE.md §25.5.4, §25.5.7–§25.5.10).

The only runtime glue between the pure control domain (``pompa.control``), the existing live
readings observation and the MQTT adapter:

* ``controls()`` projects every definition onto one live observation (``GET /api/v1/controls``);
* ``execute()`` validates with ``control.prepare``, publishes at most once through the injected
  publisher and observes the readback (``POST /api/v1/controls/{key}``).

There is no database, command store, queue, retry, replay or reconciliation. The only state is
the in-memory set of keys whose request is still running. The recorder is used only through its
read-only observation and its generic change signal; it knows nothing about control.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from . import control
from .control import ABSENT, Control, ControlError, PreparedCommand, ReadingFact
from .minute import iso_utc
from .recorder import ReadingsObservation, Recorder

log = logging.getLogger(__name__)

# Provisional readback window W (§25.5.8): above two default HeishaMon query intervals plus
# command queuing. Stage 4E-D measures the real CT109 latency and may change it.
READBACK_WINDOW_SECONDS = 15.0

ERROR_STATUS = {
    "invalid_request": 400,
    "unknown_control": 404,
    "prerequisite_not_met": 409,
    "validation_context_unavailable": 409,
    "command_in_progress": 409,
    "invalid_value": 422,
    "mqtt_unavailable": 503,
}


class Publisher(Protocol):
    def publish_command(self, topic: str, payload: str) -> bool:
        """Publish one prepared command topic/payload; ``True`` only if the client accepted it."""


class ControlRequestError(Exception):
    """A refused request. Nothing was published for it."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = ERROR_STATUS[code]


def reading_facts(observation: ReadingsObservation) -> dict[str, ReadingFact]:
    return {identity: ReadingFact.from_reading(reading)
            for identity, reading in observation.readings.items()}


def _readback_dict(c: Control) -> dict | None:
    readback = c.readback
    if readback.kind == "none":
        return None
    if readback.fields is not None:
        return {"identity": None, "kind": readback.kind, "fields": dict(readback.fields)}
    return {"identity": readback.identity, "kind": readback.kind}


def _reading_state(c: Control, reading: Mapping) -> dict:
    return {"value": c.readback.value_of(reading["raw"]), "raw": reading["raw"],
            "mode": reading["mode"], "received_at": reading["received_at"],
            "available": reading["available"]}


def _state_dict(c: Control, observation: ReadingsObservation) -> dict | None:
    """Current readback state from heat-pump-published TOP readings only, in any mode."""
    readback = c.readback
    if readback.kind == "none":
        return None
    if readback.fields is None:
        return _reading_state(c, observation.readings[readback.identity])
    fields = {name: {"identity": identity, **_reading_state(c, observation.readings[identity])}
              for name, identity in readback.fields.items()}
    return {"value": {name: field["value"] for name, field in fields.items()}, "fields": fields}


def not_executable_because(c: Control, connected: bool, facts: Mapping[str, ReadingFact],
                           prerequisites: tuple[control.PrerequisiteResult, ...]) -> list[str]:
    reasons = []
    if not connected:
        reasons.append("mqtt_disconnected")
    if any(p.satisfied is False for p in prerequisites):
        reasons.append("prerequisite_not_met")
    if c.context_identity is not None and c.value.active(facts) is None:
        reasons.append("validation_context_unavailable")
    return reasons


def _prerequisites(results: tuple[control.PrerequisiteResult, ...]) -> list[dict]:
    return [{"id": p.id, "identity": p.identity, "satisfied": p.satisfied} for p in results]


def control_dict(c: Control, observation: ReadingsObservation, facts: Mapping[str, ReadingFact]) -> dict:
    prerequisites = control.evaluate_prerequisites(c, facts)
    reasons = not_executable_because(c, observation.connected, facts, prerequisites)
    return {
        "key": c.key,
        "identity": c.identity,
        "family": c.family,
        "class": c.klass,
        "service": c.service,
        "value": control.schema(c, facts),
        "readback": _readback_dict(c),
        "state": _state_dict(c, observation),
        "prerequisites": _prerequisites(prerequisites),
        "restrictions": list(c.restrictions),
        "executable": not reasons,
        "not_executable_because": reasons,
    }


def _same(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b  # never let True match 1


class _Readback:
    """Factual readback evidence of one published request (§25.5.8).

    Only a ``live``, ``available`` reading counts. A reading received before ``published_at``
    is pre-publish state; one received at or after it is a post-publish observation. The
    evidence is factual, never causal.
    """

    def __init__(self, prepared: PreparedCommand, published_at: float):
        readback = prepared.control.readback
        self.readback = readback
        self.published_at = published_at
        if readback.fields is not None:
            self.fields = {name: readback.fields[name] for name in prepared.expected}
            self.expected = dict(prepared.expected)
        else:
            self.fields = {None: readback.identity}
            self.expected = {None: prepared.expected}
        self.pre: dict = {}
        self.matched: dict = {}
        self.different: set = set()
        self.last: dict = {}

    def observe(self, observation: ReadingsObservation) -> None:
        for name, identity in self.fields.items():
            reading = observation.readings[identity]
            received = observation.received_at[identity]
            if reading["mode"] != "live" or not reading["available"] or received is None:
                continue
            seen = {"value": self.readback.value_of(reading["raw"]), "raw": reading["raw"],
                    "received_at": reading["received_at"]}
            self.last[name] = seen
            if received < self.published_at:
                self.pre[name] = seen
            elif _same(seen["value"], self.expected[name]):
                self.matched.setdefault(name, seen)
            else:
                self.different.add(name)

    def complete(self) -> bool:
        return len(self.matched) == len(self.fields)

    def outcome(self) -> str:
        if self.complete():
            return "matched"
        if not self.different and all(
            name in self.pre and _same(self.pre[name]["value"], self.expected[name])
            for name in self.fields
        ):
            return "unchanged_match"
        return "not_observed"

    def observed(self, outcome: str) -> object:
        chosen = self.matched if outcome == "matched" else self.last
        if None in self.fields:
            return chosen.get(None)
        return {name: chosen.get(name) for name in self.fields}


class ControlRuntime:
    def __init__(self, recorder: Recorder, publisher: Publisher | None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 window_seconds: float = READBACK_WINDOW_SECONDS):
        self.recorder = recorder
        self.publisher = publisher
        self.clock = clock
        self.monotonic = monotonic
        self.window_seconds = window_seconds
        self._guard = threading.Lock()
        self._in_flight: set[str] = set()

    # ------------------------------------------------------------------ GET

    def controls(self) -> dict:
        """Every definition over one live observation. No publish, no DB, no state change."""
        observation = self.recorder.readings_observation(self.clock)
        facts = reading_facts(observation)
        return {
            "now": iso_utc(observation.now),
            "mqtt": {"connected": observation.connected},
            "readback_window_seconds": self.window_seconds,
            "controls": [control_dict(c, observation, facts) for c in control.definitions().values()],
        }

    # ----------------------------------------------------------------- POST

    def _claim(self, key: str) -> bool:
        with self._guard:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def _release(self, key: str) -> None:
        with self._guard:
            self._in_flight.discard(key)

    def execute(self, key: str, value: object) -> dict:
        """Validate, publish at most once and observe the readback, or raise
        :class:`ControlRequestError` (nothing published). ``value`` is ``ABSENT`` for ``{}``."""
        started = self.monotonic()
        result = "refused"
        outcome = None
        try:
            if key not in control.definitions():
                raise ControlRequestError("unknown_control", f"no control {key!r}")
            if not self._claim(key):
                raise ControlRequestError("command_in_progress",
                                          f"an earlier {key!r} request is still in its readback window")
            try:
                body = self._execute_claimed(key, value, started)
            finally:
                self._release(key)
            result, outcome = "sent", body["readback"]["outcome"]
            return body
        except ControlRequestError as error:
            result = error.code
            raise
        finally:
            log.info("control key=%s requested=%s publish=%s readback=%s elapsed=%.3fs",
                     key, "{}" if value is ABSENT else repr(value), result, outcome,
                     self.monotonic() - started)

    def _execute_claimed(self, key: str, value: object, started: float) -> dict:
        baseline = self.recorder.readings_observation(self.clock)
        try:
            prepared = control.prepare(key, value, reading_facts(baseline))
        except ControlError as error:
            raise ControlRequestError(error.code, error.detail) from None
        published_at = self.clock()
        if self.publisher is None or not self.publisher.publish_command(prepared.topic, prepared.payload):
            raise ControlRequestError("mqtt_unavailable", "MQTT is not connected or did not accept the publish")
        # From here on the command is sent: nothing below may turn it into an error or a retry.
        return {
            "key": key,
            "requested": prepared.requested,
            "publish": {"status": "sent", "at": iso_utc(published_at)},
            "prerequisites": _prerequisites(prepared.prerequisites),
            "readback": self._readback(prepared, baseline, published_at),
        }

    def _readback(self, prepared: PreparedCommand, baseline: ReadingsObservation,
                  published_at: float) -> dict:
        readback = prepared.control.readback
        if readback.kind == "none":
            return {"identity": None, "kind": None, "expected": None, "outcome": "not_applicable",
                    "observed": None, "window_seconds": self.window_seconds, "waited_seconds": 0.0}
        evidence = _Readback(prepared, published_at)
        evidence.observe(baseline)
        began = self.monotonic()
        deadline = began + self.window_seconds
        while True:
            observation = self.recorder.readings_observation(self.clock)
            evidence.observe(observation)
            if evidence.complete():
                break
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.recorder.wait_for_change(observation.seq, remaining)
        outcome = evidence.outcome()
        body = {
            "identity": readback.identity,
            "kind": readback.kind,
            "expected": prepared.expected,
            "outcome": outcome,
            "observed": evidence.observed(outcome),
            "window_seconds": self.window_seconds,
            "waited_seconds": round(self.monotonic() - began, 3),
        }
        if readback.fields is not None:
            body["fields"] = dict(evidence.fields)
        return body
