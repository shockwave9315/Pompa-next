"""Stage 4B checkpoint B: production policy schema against real MariaDB (docs/ARCHITECTURE.md §25.2.1).

Skipped unless ``POMPA_TEST_DB_HOST`` is set (see ``conftest.mariadb``).
"""

import json

import pytest

from conftest import row
from pompa.storage import StorageUnavailable


def test_ensure_schema_creates_the_four_policy_tables_and_genesis(mariadb):
    mariadb.ensure_schema()
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            " AND TABLE_NAME IN ('optional_series', 'optional_policy_revision',"
            " 'optional_policy_member', 'optional_policy_head') ORDER BY TABLE_NAME")
        assert cur.fetchall() == (
            ("optional_policy_head",), ("optional_policy_member",),
            ("optional_policy_revision",), ("optional_series",),
        )
    with mariadb.session() as s:
        head = s.read_policy_head()
        assert head == 1
        revision = s.read_revision(head)
        assert revision == (1, None, 0, revision[3])  # genesis: no base, effective from time zero
        assert s.read_revision_members(head) == []  # default selection is empty


def test_ensure_schema_is_idempotent(mariadb):
    mariadb.ensure_schema()
    mariadb.ensure_schema()
    mariadb.ensure_schema()
    with mariadb.session() as s:
        assert s.read_policy_head() == 1
        assert s.read_revision(1) is not None


def test_genesis_head_points_at_genesis_across_a_second_ensure_schema_call(mariadb):
    mariadb.ensure_schema()
    with mariadb.session() as s:
        s.lock_policy_head()
        new_rev = s.insert_revision(1, 60, 1000)
        s.update_policy_head(new_rev)
    mariadb.ensure_schema()  # must not reset an already-advanced head back to genesis
    with mariadb.session() as s:
        assert s.read_policy_head() == new_rev


def test_unique_historical_series_meaning(mariadb):
    mariadb.ensure_schema()
    sentinels = json.dumps([-128.0, -78.0], separators=(",", ":"))
    with mariadb.session() as s:
        id_a = s.get_or_create_series("TOP21", "main/Outside_Pipe_Temp", 1, "x", "°C", "mean",
                                      "measurement", sentinels, None, None, False, 1000)
        id_b = s.get_or_create_series("TOP21", "main/Outside_Pipe_Temp", 1, "y", "°C", "mean",
                                      "measurement", sentinels, None, None, False, 2000)
        assert id_a == id_b  # same (identity, expected_topic, profile_version): the same series
        id_c = s.get_or_create_series("TOP21", "main/Outside_Pipe_Temp", 2, "x", "°C", "mean",
                                      "measurement", sentinels, None, None, False, 1000)
        assert id_c != id_a  # a different profile_version is a different historical meaning


def test_series_fields_are_immutable_after_insert(mariadb):
    """get_or_create never updates any column of an existing row (only the id is recovered)."""
    mariadb.ensure_schema()
    sentinels = json.dumps([-128.0, -78.0], separators=(",", ":"))
    with mariadb.session() as s:
        first_id = s.get_or_create_series("TOP21", "main/Outside_Pipe_Temp", 1, "Original label",
                                          "°C", "mean", "measurement", sentinels, None, None, False, 1000)
        second_id = s.get_or_create_series("TOP21", "main/Outside_Pipe_Temp", 1, "Different label",
                                           "°C", "mean", "measurement", sentinels, None, None, False, 2000)
        assert first_id == second_id
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT label, created_at FROM optional_series WHERE id = %s", (first_id,))
        label, created_at = cur.fetchone()
        assert label == "Original label"
        assert created_at == 1000


def test_effective_from_minute_must_be_minute_aligned(mariadb):
    mariadb.ensure_schema()
    with pytest.raises(ValueError):
        with mariadb.session() as s:
            s.lock_policy_head()
            s.insert_revision(1, 1801, 1000)  # not a multiple of 60


def test_database_check_constraint_also_rejects_unaligned_minutes(mariadb):
    """Real protection independent of the Python-side guard: raw SQL bypassing
    ``Session.insert_revision`` is still refused by the database itself."""
    mariadb.ensure_schema()
    with pytest.raises(StorageUnavailable):
        with mariadb._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO optional_policy_revision (base_revision_id, effective_from_minute,"
                " created_at) VALUES (1, 1801, 1000)")
            conn.commit()


def test_one_child_per_base_revision_is_enforced(mariadb):
    mariadb.ensure_schema()
    with mariadb.session() as s:
        s.lock_policy_head()
        s.insert_revision(1, 60, 1000)
    with pytest.raises(StorageUnavailable):
        with mariadb.session() as s:
            s.lock_policy_head()
            s.insert_revision(1, 120, 2000)  # a second child of base=1: refused


def test_dangling_member_series_reference_is_rejected(mariadb):
    """The member -> series foreign key is real protection, not decoration."""
    mariadb.ensure_schema()
    with pytest.raises(StorageUnavailable):
        with mariadb.session() as s:
            s.lock_policy_head()
            revision_id = s.insert_revision(1, 60, 1000)
            s.insert_revision_members(revision_id, [999999])  # no such series id


def test_lock_latest_minute_ts_reads_the_current_read_of_sample_1m(mariadb):
    mariadb.ensure_schema()
    with mariadb.session() as s:
        assert s.lock_latest_minute_ts() is None
    with mariadb.session() as s:
        s.upsert_minutes([row(1_800_000_000), row(1_800_000_120)])
    with mariadb.session() as s:
        assert s.lock_latest_minute_ts() == 1_800_000_120
