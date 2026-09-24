"""Stage 4B checkpoint A: MariaDB feasibility proof for policy-head concurrency.

Proves the required race property from ``docs/ARCHITECTURE.md`` S25.2 against
real MariaDB, with two independent connections/transactions coordinated by
``threading.Event`` and bounded waits, never a sleep-only race:

* a locking/current read (``SELECT ... FOR UPDATE``) on the singleton head row
  serializes a policy-replacement transaction against a minute-persistence
  transaction, even though both use this repository's
  ``START TRANSACTION WITH CONSISTENT SNAPSHOT`` (``pompa.storage.Storage.session``);
* whichever transaction commits first is observed by the other only through
  that locking read, never through an ordinary snapshot read taken before the
  commit -- each test asserts both, so swapping the locking read for a plain
  one *must* fail the assertion instead of accidentally passing.

No production Stage 4B table exists yet; this uses test-only tables
(``stage4b_policy_head``, ``stage4b_minute_progress``) that model exactly the
one fact under test -- the singleton head lock -- not the full policy schema.

Skipped unless ``POMPA_TEST_DB_HOST`` is set (see ``conftest.mariadb``).
"""

from __future__ import annotations

import threading
import time

import pymysql
import pytest

HEAD = "stage4b_policy_head"
PROGRESS = "stage4b_minute_progress"

WAIT_BOUND = 5.0  # generous bound for CI/dev-box scheduling jitter; never a sleep-only proof
HOLD_SECONDS = 0.4  # how long the lock holder deliberately keeps the row locked


def _connect(storage):
    """A raw, independent pymysql connection (this test needs two real connections, not sessions)."""
    return pymysql.connect(**storage._params)


@pytest.fixture
def tables(mariadb):
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {HEAD}")
        cur.execute(f"DROP TABLE IF EXISTS {PROGRESS}")
        cur.execute(f"CREATE TABLE {HEAD} (id TINYINT UNSIGNED NOT NULL PRIMARY KEY,"
                    " revision_id INT UNSIGNED NOT NULL) ENGINE=InnoDB")
        cur.execute(f"CREATE TABLE {PROGRESS} (id TINYINT UNSIGNED NOT NULL PRIMARY KEY,"
                    " last_minute_ts INT UNSIGNED NOT NULL) ENGINE=InnoDB")
        cur.execute(f"INSERT INTO {HEAD} (id, revision_id) VALUES (1, 1)")
        cur.execute(f"INSERT INTO {PROGRESS} (id, last_minute_ts) VALUES (1, 1000)")
        conn.commit()
    yield mariadb
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {HEAD}")
        cur.execute(f"DROP TABLE IF EXISTS {PROGRESS}")
        conn.commit()


def _begin(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")


def _lock_head(conn) -> int:
    """The locking/current read every real transaction (PUT or minute persistence) must use."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT revision_id FROM {HEAD} WHERE id = 1 FOR UPDATE")
        (revision_id,) = cur.fetchone()
        return revision_id


def _plain_read_progress(conn) -> int:
    """An ordinary, non-locking read: bound to this transaction's own consistent snapshot."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT last_minute_ts FROM {PROGRESS} WHERE id = 1")
        (value,) = cur.fetchone()
        return value


def _locking_read_progress(conn) -> int:
    """The current-read equivalent: what a resolving transaction must actually use."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT last_minute_ts FROM {PROGRESS} WHERE id = 1 FOR UPDATE")
        (value,) = cur.fetchone()
        return value


def _wait(event: threading.Event, label: str) -> None:
    assert event.wait(WAIT_BOUND), f"timed out waiting for {label}; blocking/serialization did not happen as required"


def test_case_a_minute_writer_wins_policy_put_observes_committed_frontier(tables):
    """CASE A: the minute-persistence transaction locks and commits first.

    The concurrent policy PUT must block on the same head row, and once
    unblocked must resolve from the *locking* read of the committed frontier,
    never from the plain snapshot read it could otherwise have taken while it
    was waiting for the lock.
    """
    storage = tables
    t1_locked = threading.Event()      # T1 (minute writer) holds the head lock
    t1_may_commit = threading.Event()  # T2 confirms it is blocked; T1 may now commit
    result: dict[str, int | bool] = {}

    def minute_writer() -> None:
        conn = _connect(storage)
        try:
            _begin(conn)
            _lock_head(conn)  # minute persistence also locks/reads the head (S25.2 step 2)
            t1_locked.set()
            _wait(t1_may_commit, "T2 to confirm it is blocked before T1 commits")
            time.sleep(HOLD_SECONDS)  # deliberately hold the lock so a fluke ordering cannot pass
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {PROGRESS} SET last_minute_ts = 2000 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()

    def policy_put() -> None:
        conn = _connect(storage)
        try:
            _wait(t1_locked, "T1 to acquire the head lock")
            _begin(conn)
            result["snapshot_before_lock"] = _plain_read_progress(conn)  # 1000: pre-T1 snapshot

            # Attempt the same head-row lock T1 already holds: this, not the progress row,
            # is the single serialization point (S25.2 step 6/step 4 of the PUT sequence).
            attempt = threading.Thread(target=lambda: result.__setitem__("head_lock_read", _lock_head(conn)))
            attempt.start()
            # Prove T2 is actually blocked: the attempt thread must NOT finish while T1 still holds the lock.
            attempt.join(timeout=HOLD_SECONDS * 0.5)
            result["was_blocked"] = attempt.is_alive()
            t1_may_commit.set()
            attempt.join(WAIT_BOUND)
            assert not attempt.is_alive(), "policy PUT never unblocked after the minute writer committed"

            # Now that T2 holds the head lock (T1 has committed and released it), resolve the
            # frontier: a plain read is still bound to T2's pre-commit snapshot; only the
            # locking read observes what T1 actually committed.
            result["plain_after_unblock"] = _plain_read_progress(conn)  # still 1000: T2's own old snapshot
            result["locking_read"] = _locking_read_progress(conn)  # 2000: T1's committed frontier
            effective_from = result["locking_read"] + 60  # must land strictly after the committed minute
            result["effective_from"] = effective_from
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {HEAD} SET revision_id = 2 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()

    t1 = threading.Thread(target=minute_writer)
    t2 = threading.Thread(target=policy_put)
    t1.start()
    t2.start()
    t1.join(WAIT_BOUND * 2)
    t2.join(WAIT_BOUND * 2)
    assert not t1.is_alive() and not t2.is_alive()

    assert result["was_blocked"] is True, "the policy PUT must actually block on the head row lock"
    assert result["snapshot_before_lock"] == 1000
    # The trap the architecture explicitly warns against: an ordinary snapshot read never advances,
    # even after unblocking and even though the minute writer has since committed.
    assert result["plain_after_unblock"] == 1000
    # The property actually required: the locking read sees the committed frontier, not the snapshot.
    assert result["locking_read"] == 2000
    assert result["effective_from"] == 2060  # strictly after the committed minute frontier

    with storage.session() as s:
        s._cur.execute(f"SELECT last_minute_ts FROM {PROGRESS} WHERE id = 1")
        assert s._cur.fetchone()[0] == 2000
        s._cur.execute(f"SELECT revision_id FROM {HEAD} WHERE id = 1")
        assert s._cur.fetchone()[0] == 2


def test_case_b_policy_put_wins_minute_writer_observes_new_revision(tables):
    """CASE B: the policy PUT locks, replaces the head and commits first.

    The concurrent minute-persistence transaction must block on the same head
    row, and once unblocked must resolve the new revision from its locking
    read, never from a plain read taken from its own pre-commit snapshot.
    """
    storage = tables
    t2_locked = threading.Event()
    t1_ready = threading.Event()
    t2_may_commit = threading.Event()
    result: dict[str, int | bool] = {}

    def policy_put() -> None:
        conn = _connect(storage)
        try:
            _begin(conn)
            _lock_head(conn)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {HEAD} SET revision_id = 2 WHERE id = 1")
            t2_locked.set()
            _wait(t1_ready, "T1 to begin its own transaction before T2 commits")
            _wait(t2_may_commit, "T1 to confirm it is blocked before T2 commits")
            time.sleep(HOLD_SECONDS)
            conn.commit()
        finally:
            conn.close()

    def minute_writer() -> None:
        conn = _connect(storage)
        try:
            _begin(conn)
            result["plain_before_wait"] = _read_head_plain(conn)  # 1: T1's own snapshot, taken before T2 commits
            t1_ready.set()
            _wait(t2_locked, "T2 to acquire the head lock")

            attempt = threading.Thread(target=lambda: result.__setitem__("locking_read", _lock_head(conn)))
            attempt.start()
            attempt.join(timeout=HOLD_SECONDS * 0.5)
            result["was_blocked"] = attempt.is_alive()
            t2_may_commit.set()
            attempt.join(WAIT_BOUND)
            assert not attempt.is_alive(), "minute writer never unblocked after the policy PUT committed"

            result["plain_after_unblock"] = _read_head_plain(conn)  # still 1: same old snapshot
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {PROGRESS} SET last_minute_ts = 3000 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()

    def _read_head_plain(conn) -> int:
        with conn.cursor() as cur:
            cur.execute(f"SELECT revision_id FROM {HEAD} WHERE id = 1")
            (value,) = cur.fetchone()
            return value

    t2 = threading.Thread(target=policy_put)
    t1 = threading.Thread(target=minute_writer)
    t2.start()
    t1.start()
    t2.join(WAIT_BOUND * 2)
    t1.join(WAIT_BOUND * 2)
    assert not t1.is_alive() and not t2.is_alive()

    assert result["was_blocked"] is True, "the minute writer must actually block on the head row lock"
    assert result["plain_before_wait"] == 1
    # The trap: an ordinary snapshot read never advances even after T2 committed and T1 unblocked.
    assert result["plain_after_unblock"] == 1
    # The property required: the locking read observes the new policy truth for this minute.
    assert result["locking_read"] == 2

    with storage.session() as s:
        s._cur.execute(f"SELECT revision_id FROM {HEAD} WHERE id = 1")
        assert s._cur.fetchone()[0] == 2
        s._cur.execute(f"SELECT last_minute_ts FROM {PROGRESS} WHERE id = 1")
        assert s._cur.fetchone()[0] == 3000
