"""Stage 4B checkpoint B: production policy schema against real MariaDB (docs/ARCHITECTURE.md §25.2.1).

Skipped unless ``POMPA_TEST_DB_HOST`` is set (see ``conftest.mariadb``).
"""

import json

import pytest

from conftest import row
import pompa.optional_policy as op
from pompa.history_profile import HistoryProfile
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


def _profile(**overrides) -> HistoryProfile:
    base = dict(identity="TOP21", expected_topic="main/Outside_Pipe_Temp", profile_version=1,
               label="Temperatura rury zewnętrznej", unit="°C", kind="mean",
               semantic_type="measurement", sentinels=frozenset({-78.0, -128.0}),
               min_value=None, max_value=None, energy=False)
    base.update(overrides)
    return HistoryProfile(**base)


def test_resolve_series_id_reuses_an_exact_semantic_match(mariadb):
    mariadb.ensure_schema()
    profile = _profile()
    with mariadb.session() as s:
        first = op.resolve_series_id(s, profile, 1000)
        second = op.resolve_series_id(s, profile, 2000)
    assert first == second


def test_resolve_series_id_reuses_across_a_label_only_change(mariadb):
    """Label is presentation only; changing it never creates a new series or a conflict,
    and the persisted row keeps its original stored label."""
    mariadb.ensure_schema()
    with mariadb.session() as s:
        first = op.resolve_series_id(s, _profile(label="Original"), 1000)
        second = op.resolve_series_id(s, _profile(label="Different label entirely"), 2000)
    assert first == second
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT label FROM optional_series WHERE id = %s", (first,))
        assert cur.fetchone()[0] == "Original"


@pytest.mark.parametrize("overrides", [
    {"unit": "K"},
    {"kind": "last"},
    {"semantic_type": "counter"},
    {"sentinels": frozenset({-1.0})},
    {"min_value": 0.0},
    {"max_value": 100.0},
    {"energy": True},
])
def test_resolve_series_id_fails_closed_on_semantic_conflict(mariadb, overrides):
    mariadb.ensure_schema()
    with mariadb.session() as s:
        op.resolve_series_id(s, _profile(), 1000)
    with pytest.raises(op.SeriesDefinitionConflict):
        with mariadb.session() as s:
            op.resolve_series_id(s, _profile(**overrides), 2000)
    # The conflict must never mutate the originally persisted row.
    with mariadb.session() as s:
        stored = op.resolve_series_id(s, _profile(), 3000)
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT unit, kind, semantic_type, sentinels_json, min_value, max_value, energy"
                    " FROM optional_series WHERE id = %s", (stored,))
        unit, kind, semantic_type, sentinels_json, min_value, max_value, energy = cur.fetchone()
        assert (unit, kind, semantic_type, bool(energy)) == ("°C", "mean", "measurement", False)
        assert json.loads(sentinels_json) == sorted({-78.0, -128.0})
        assert min_value is None and max_value is None


def test_binary_identity_topic_collation_is_exact(mariadb):
    """Protocol identity/topic text compares exactly at the storage level, independent of the
    database's default collation: a case difference is a different ``(identity, expected_topic,
    profile_version)`` row, never an alias. Exercised directly through ``get_or_create_series``
    (the raw storage primitive), not ``resolve_series_id``: the domain layer's own
    identity+version invariant (below) correctly refuses to treat two different topics under the
    same ``(identity, profile_version)`` as separate selectable series -- that is a conflict, not
    a collation question."""
    mariadb.ensure_schema()
    sentinels = json.dumps([-128.0, -78.0], separators=(",", ":"))
    with mariadb.session() as s:
        lower_id = s.get_or_create_series("TOP21", "main/Ipm_Temp", 1, "x", "°C", "mean",
                                          "measurement", sentinels, None, None, False, 1000)
        upper_id = s.get_or_create_series("TOP21", "main/IPM_TEMP", 1, "x", "°C", "mean",
                                          "measurement", sentinels, None, None, False, 1000)
    assert lower_id != upper_id

    with mariadb.session() as s:
        a_id = s.get_or_create_series("TOP21", "main/Outside_Pipe_Temp", 1, "x", "°C", "mean",
                                      "measurement", sentinels, None, None, False, 1000)
        b_id = s.get_or_create_series("top21", "main/Outside_Pipe_Temp", 1, "x", "°C", "mean",
                                      "measurement", sentinels, None, None, False, 1000)
    assert a_id != b_id


def test_resolve_series_id_conflicts_on_topic_change_without_version_bump(mariadb):
    """One (identity, profile_version) is exactly one semantic lineage (§25.2.8): a topic change
    under an unbumped profile_version must fail closed, never silently create a second,
    independent series for the same identity+version."""
    mariadb.ensure_schema()
    with mariadb.session() as s:
        original_id = op.resolve_series_id(s, _profile(expected_topic="main/Outside_Pipe_Temp"), 1000)

    with pytest.raises(op.SeriesDefinitionConflict):
        with mariadb.session() as s:
            op.resolve_series_id(s, _profile(expected_topic="main/New_Outside_Pipe_Temp"), 2000)

    # No second row was created, and the original row is untouched.
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, expected_topic FROM optional_series WHERE identity = 'TOP21'"
                    " AND profile_version = 1")
        assert cur.fetchall() == ((original_id, "main/Outside_Pipe_Temp"),)


def test_resolve_series_id_allows_a_new_topic_under_a_bumped_version(mariadb):
    """The versioning rule is structurally satisfiable: a genuinely new profile_version for the
    same identity may carry a different expected_topic. (This v2 fixture is a test-only proof of
    the storage/domain layer's rule, never added to the real HISTORY_PROFILES catalog.)"""
    mariadb.ensure_schema()
    with mariadb.session() as s:
        v1_id = op.resolve_series_id(s, _profile(expected_topic="main/Outside_Pipe_Temp",
                                                 profile_version=1), 1000)
        v2_id = op.resolve_series_id(s, _profile(expected_topic="main/New_Outside_Pipe_Temp",
                                                 profile_version=2), 2000)
    assert v1_id != v2_id
    with mariadb._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT identity, expected_topic, profile_version FROM optional_series"
                    " WHERE identity = 'TOP21' ORDER BY profile_version")
        assert cur.fetchall() == (
            ("TOP21", "main/Outside_Pipe_Temp", 1),
            ("TOP21", "main/New_Outside_Pipe_Temp", 2),
        )


def test_lock_latest_minute_ts_reads_the_current_read_of_sample_1m(mariadb):
    mariadb.ensure_schema()
    with mariadb.session() as s:
        assert s.lock_latest_minute_ts() is None
    with mariadb.session() as s:
        s.upsert_minutes([row(1_800_000_000), row(1_800_000_120)])
    with mariadb.session() as s:
        assert s.lock_latest_minute_ts() == 1_800_000_120
