"""Stage 4B checkpoint B: ``/api/v1/optional-history/selection`` against real MariaDB.

Skipped unless ``POMPA_TEST_DB_HOST`` is set (see ``conftest.mariadb``). No
optional_sample_1m table exists yet and no OptionalAccumulator exists;
canonical sample_1m/rollup_1h rows and rollups are untouched by every GET/PUT
exercised here.
"""

from fastapi.testclient import TestClient

from conftest import T0
from pompa.api import create_app
from pompa.history_profile import HISTORY_PROFILES_BY_IDENTITY
from pompa.ingest import Ingest
from pompa.minute import MinuteAccumulator
from pompa.recorder import Recorder


class RealApi:
    """Like ``conftest.Api``, but wired to real MariaDB instead of ``FakeStorage``."""

    def __init__(self, storage, start=T0):
        storage.ensure_schema()
        self.storage = storage
        self.ingest = Ingest(600)
        self.recorder = Recorder(self.ingest, MinuteAccumulator(self.ingest, start), storage, 60)
        self.now = float(start)
        self.client = TestClient(create_app(self.recorder, storage, clock=lambda: self.now))

    def get(self, path, t=None, **params):
        if t is not None:
            self.now = float(t)
        return self.client.get(path, params=params)

    def put(self, path, json_body, t=None):
        if t is not None:
            self.now = float(t)
        return self.client.put(path, json=json_body)

    def body(self, path, t=None, **params):
        r = self.get(path, t, **params)
        assert r.status_code == 200, r.text
        return r.json()


def test_get_fresh_db_is_empty_genesis(mariadb):
    api = RealApi(mariadb)
    body = api.body("/api/v1/optional-history/selection")
    assert body["active_revision"] == body["head_revision"] == {"id": 1, "effective_from": "1970-01-01T00:00:00Z"}
    assert body["pending"] is False
    assert body["active_members"] == body["head_members"] == []


def test_first_put_creates_a_pending_revision(mariadb):
    api = RealApi(mariadb, start=T0)
    r = api.put("/api/v1/optional-history/selection",
               {"base_revision": 1, "identities": ["TOP21", "XTOP1"]}, t=T0 + 3600)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["idempotent_replay"] is False
    assert body["revision"]["id"] == 2

    selection = api.body("/api/v1/optional-history/selection")
    assert selection["pending"] is True
    assert selection["active_members"] == []  # not yet effective
    assert {m["identity"] for m in selection["head_members"]} == {"TOP21", "XTOP1"}


def test_empty_replacement_is_legal(mariadb):
    api = RealApi(mariadb, start=T0)
    api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]}, t=T0)
    head = api.body("/api/v1/optional-history/selection")["head_revision"]["id"]
    r = api.put("/api/v1/optional-history/selection", {"base_revision": head, "identities": []})
    assert r.status_code == 200, r.text
    selection = api.body("/api/v1/optional-history/selection")
    assert selection["head_members"] == []


def test_multiple_revisions_and_activation_boundary(mariadb):
    api = RealApi(mariadb, start=T0)
    r1 = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]}, t=T0)
    head1 = r1.json()["revision"]["id"]

    r2 = api.put("/api/v1/optional-history/selection",
               {"base_revision": head1, "identities": ["TOP21", "TOP50"]})
    assert r2.status_code == 200, r2.text
    head2 = r2.json()["revision"]["id"]
    assert head2 != head1

    # Before the boundary: active selection is still the empty genesis.
    before = api.body("/api/v1/optional-history/selection")
    assert before["pending"] is True
    assert before["active_members"] == []

    # Advance the clock past every revision's effective_from_minute.
    far_future = T0 + 3 * 3600
    after = api.body("/api/v1/optional-history/selection", t=far_future)
    assert after["pending"] is False
    assert after["active_revision"]["id"] == head2
    assert {m["identity"] for m in after["active_members"]} == {"TOP21", "TOP50"}


def test_stale_base_revision_is_409(mariadb):
    api = RealApi(mariadb, start=T0)
    api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]}, t=T0)
    r = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP50"]})
    assert r.status_code == 409, r.text


def test_duplicate_identity_is_400(mariadb):
    api = RealApi(mariadb, start=T0)
    r = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21", "TOP21"]})
    assert r.status_code == 400, r.text


def test_unknown_identity_is_400(mariadb):
    api = RealApi(mariadb, start=T0)
    r = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["NOPE"]})
    assert r.status_code == 400, r.text


def test_canonical_source_identity_is_400_unknown(mariadb):
    """TOP16 already serves a canonical metric source: never a valid optional-history identity."""
    api = RealApi(mariadb, start=T0)
    r = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP16"]})
    assert r.status_code == 400, r.text


def test_database_unavailable_is_503(mariadb):
    api = RealApi(mariadb, start=T0)
    from pompa.storage import Storage

    broken = Storage(host="127.0.0.1", port=1, user="pompa", password="pompa", database="pompa_next_test")
    api.storage = broken
    api.client = TestClient(create_app(api.recorder, broken, clock=lambda: api.now))
    assert api.get("/api/v1/optional-history/selection").status_code == 503
    r = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": []})
    assert r.status_code == 503


def test_ambiguous_retry_with_same_base_and_set_is_idempotent_200(mariadb):
    api = RealApi(mariadb, start=T0)
    r1 = api.put("/api/v1/optional-history/selection",
               {"base_revision": 1, "identities": ["TOP21", "TOP50"]}, t=T0)
    assert r1.status_code == 200 and r1.json()["idempotent_replay"] is False
    # The client never saw r1's response (lost acknowledgement) and retries the identical request.
    r2 = api.put("/api/v1/optional-history/selection",
               {"base_revision": 1, "identities": ["TOP21", "TOP50"]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["revision"] == r1.json()["revision"]


def test_conflicting_stale_retry_is_409_not_idempotent(mariadb):
    api = RealApi(mariadb, start=T0)
    api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]}, t=T0)
    # A genuinely different concurrent client also claimed base=1, but with a different set.
    r = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP50"]})
    assert r.status_code == 409, r.text


def test_canonical_history_and_metrics_are_unaffected_by_selection_put(mariadb):
    api = RealApi(mariadb, start=T0)
    default_metrics = api.body("/api/v1/metrics")
    default_capabilities = api.body("/api/v1/metrics", include="capabilities")
    api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21", "XTOP1"]}, t=T0)
    assert api.body("/api/v1/metrics") == default_metrics
    assert api.body("/api/v1/metrics", include="capabilities") == default_capabilities
    with api.storage.session() as s:
        assert s.minute_bounds() == (None, None)  # canonical sample_1m is untouched
        assert s.rolled_until() is None


def test_no_optional_sample_1m_table_exists_yet(mariadb):
    api = RealApi(mariadb, start=T0)
    api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]}, t=T0)
    with api.storage._connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            " AND TABLE_NAME IN ('optional_sample_1m', 'optional_rollup_1h')")
        assert cur.fetchone()[0] == 0


# ------------------------------------------------------------------ stored semantic-definition conflict


def _seed_stale_member(storage, identity, expected_topic, profile_version):
    """Simulate an optional_series row persisted by an *older* code version, before its
    semantic definition changed -- without going through replace_selection, which would
    itself now correctly refuse to create a conflicting row (§25.2.8)."""
    with storage.session() as s:
        head_id = s.lock_policy_head()
        series_id = s.get_or_create_series(identity, expected_topic, profile_version, "old label",
                                           "K", "last", "counter", "[-1.0]", None, None, False, 1000)
        new_revision = s.insert_revision(head_id, 60, 1000)
        s.insert_revision_members(new_revision, [series_id])
        s.update_policy_head(new_revision)


def test_get_reports_profile_definition_changed(mariadb):
    api = RealApi(mariadb, start=T0)
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    _seed_stale_member(api.storage, "TOP21", profile.expected_topic, profile.profile_version)

    body = api.body("/api/v1/optional-history/selection")
    assert [m["identity"] for m in body["head_members"]] == ["TOP21"]
    assert body["head_members"][0]["blocked_reason"] == "profile_definition_changed"
    # The historical row remains selected by the policy timeline; it is blocked, not removed.
    assert [m["identity"] for m in body["active_members"]] == ["TOP21"]


def test_put_fails_closed_on_stored_definition_conflict(mariadb):
    api = RealApi(mariadb, start=T0)
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    _seed_stale_member(api.storage, "TOP21", profile.expected_topic, profile.profile_version)
    head_before = api.body("/api/v1/optional-history/selection")["head_revision"]["id"]

    r = api.put("/api/v1/optional-history/selection", {"base_revision": head_before, "identities": ["TOP21"]})
    assert r.status_code == 409, r.text

    after = api.body("/api/v1/optional-history/selection")
    assert after["head_revision"]["id"] == head_before  # no new revision, head unmoved
    with api.storage._connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT unit, kind FROM optional_series WHERE identity = 'TOP21'")
        assert cur.fetchone() == ("K", "last")  # the old row was never mutated


def test_ambiguous_replay_fails_closed_when_stored_definition_was_altered(mariadb):
    """An ordinary idempotent replay (§25.2.8 Part 13) must not bypass the semantic check:
    if the persisted series no longer matches current code, the retry fails closed, never
    a false idempotent 200."""
    api = RealApi(mariadb, start=T0)
    r1 = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]}, t=T0)
    assert r1.status_code == 200, r1.text

    # Simulate the code's TOP21 semantics having changed since this row was written (a version
    # bump the deployer forgot), by altering the already-persisted row directly.
    with api.storage._connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE optional_series SET unit = 'K' WHERE identity = 'TOP21'")
        conn.commit()

    r2 = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]})
    assert r2.status_code == 409, r2.text


def test_ambiguous_replay_fails_closed_when_stored_topic_was_altered_without_a_version_bump(mariadb):
    """The identity+version guard cannot be bypassed by the idempotent-replay path either: a
    persisted topic change under the same (identity, profile_version) must never replay as a
    false 200, whichever specific conflict/stale-base mechanism reports it."""
    api = RealApi(mariadb, start=T0)
    r1 = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]}, t=T0)
    assert r1.status_code == 200, r1.text

    # Simulate a code change that altered TOP21's expected_topic without bumping profile_version,
    # by altering the already-persisted row's topic directly.
    with api.storage._connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE optional_series SET expected_topic = 'main/New_Outside_Pipe_Temp'"
                    " WHERE identity = 'TOP21'")
        conn.commit()

    r2 = api.put("/api/v1/optional-history/selection", {"base_revision": 1, "identities": ["TOP21"]})
    assert r2.status_code == 409, r2.text  # never a false idempotent 200
