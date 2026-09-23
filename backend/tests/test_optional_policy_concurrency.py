"""Stage 4B checkpoint B: production policy-head concurrency, using the actual
``optional_policy.replace_selection`` domain function and the real
``optional_policy_head``/``optional_policy_revision`` tables against real MariaDB
(not the checkpoint A test-only stand-in tables, which are kept and still run separately).

Skipped unless ``POMPA_TEST_DB_HOST`` is set (see ``conftest.mariadb``).
"""

from __future__ import annotations

import threading

import pytest

from conftest import T0
import pompa.optional_policy as op
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder

WAIT_BOUND = 5.0
HOLD_SECONDS = 0.4


def _recorder(storage, start=T0):
    ingest = Ingest(600)
    return Recorder(ingest, MinuteAccumulator(ingest, start), storage, 60)


def _wait(event: threading.Event, label: str) -> None:
    assert event.wait(WAIT_BOUND), f"timed out waiting for {label}"


def _paused_clock(fixed_now: float, locked_event: threading.Event, may_proceed: threading.Event):
    """A clock whose *second* call (the one ``replace_selection`` makes right after
    ``lock_policy_head`` succeeds) blocks until released -- holding the real head-row lock
    open for a controlled window without touching any SQL directly."""
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        if calls["n"] == 2:
            locked_event.set()
            _wait(may_proceed, "the test to release the paused PUT")
        return fixed_now

    return clock


def _run_concurrent_puts(mariadb, second_identities):
    mariadb.ensure_schema()
    recorder = _recorder(mariadb)
    locked = threading.Event()
    may_proceed = threading.Event()
    t2_attempted = threading.Event()
    result: dict[str, object] = {}

    def first_put() -> None:
        clock = _paused_clock(float(T0 + 3600), locked, may_proceed)
        result["first"] = op.replace_selection(recorder, mariadb, 1, ["TOP21"], clock)

    def second_put() -> None:
        _wait(locked, "the first PUT to lock the head row")
        t2_attempted.set()
        try:
            result["second"] = op.replace_selection(recorder, mariadb, 1, second_identities,
                                                     lambda: float(T0 + 3600))
        except op.StaleBaseRevision as e:
            result["second_error"] = e

    t1 = threading.Thread(target=first_put)
    t2 = threading.Thread(target=second_put)
    t1.start()
    t2.start()
    _wait(t2_attempted, "the second PUT to at least attempt the lock")

    # Prove the second PUT is genuinely blocked before releasing the first: it must not have
    # produced a result yet, well past the point it started attempting the lock.
    import time

    time.sleep(HOLD_SECONDS)
    assert "second" not in result and "second_error" not in result, (
        "the second PUT completed before the first committed; the head lock did not serialize them"
    )

    may_proceed.set()
    t1.join(WAIT_BOUND)
    t2.join(WAIT_BOUND)
    assert not t1.is_alive() and not t2.is_alive()
    return result


def test_concurrent_puts_with_different_sets_second_gets_stale_conflict(mariadb):
    """Case: the first PUT wins the lock and commits; a genuinely different concurrent
    request against the same stale base is refused, never silently merged or dropped."""
    result = _run_concurrent_puts(mariadb, second_identities=["TOP50"])
    assert result["first"].idempotent_replay is False
    assert "second_error" in result, "a conflicting concurrent PUT must raise StaleBaseRevision"

    with mariadb.session() as s:
        head = s.read_policy_head()
        assert head == result["first"].revision.id  # only the first PUT's revision is head
        members = {m.identity for m in s.read_revision_members(head)}
        assert members == {"TOP21"}  # the second PUT's request never took effect


def test_concurrent_puts_with_identical_sets_second_is_idempotent_replay(mariadb):
    """Case: an ambiguous retry (same base, same resolved set) racing the original request
    must observe it as an already-successful replay, never a stale conflict or a duplicate."""
    result = _run_concurrent_puts(mariadb, second_identities=["TOP21"])
    assert result["first"].idempotent_replay is False
    assert "second" in result, result.get("second_error")
    assert result["second"].idempotent_replay is True
    assert result["second"].revision == result["first"].revision

    with mariadb.session() as s:
        head = s.read_policy_head()
        # Exactly one child of genesis exists: the lock serialized the two attempts into one
        # real revision, never two competing revisions or a UNIQUE-constraint failure.
        assert s.read_revision(head)[1] == 1  # base_revision_id == genesis
