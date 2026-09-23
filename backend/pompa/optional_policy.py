"""Stage 4B checkpoint B: the optional-history policy domain (``docs/ARCHITECTURE.md`` §25.2.1).

SQL mechanics live in ``storage.Session``; this module owns policy *meaning*:
resolving ``effective_from_minute``, replacing the selection under the one
database serialization point, and reporting active-versus-pending selection
with per-member drift/blocking. No optional value is read, written or
computed here — checkpoint B adds no ``OptionalAccumulator`` and no
``optional_sample_1m`` persistence.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .history_profile import (
    HISTORY_PROFILES_BY_IDENTITY, BlockReason, HistoryProfile, ProfileSemantics, capability_topics,
    drift_reason, profile_semantics,
)
from .minute import MINUTE, floor_minute
from .storage import SeriesRow, Storage


class StaleBaseRevision(Exception):
    """The supplied ``base_revision`` is not (and was never) the current head; retry with GET."""


class SeriesDefinitionConflict(Exception):
    """A persisted ``optional_series`` row's semantic definition no longer matches the current
    code-side ``HistoryProfile`` of the same ``(identity, expected_topic, profile_version)``.

    The stored row is never mutated or reused; a semantic field changed without a
    ``profile_version`` bump is a code-definition error, and the fix is to increment
    ``profile_version`` before the changed meaning can be selected (docs/ARCHITECTURE.md §25.2.8).
    """

    def __init__(self, identity: str, expected_topic: str, profile_version: int):
        super().__init__(
            f"{identity} (topic={expected_topic!r}, profile_version={profile_version}) has a"
            " persisted semantic definition that no longer matches the current code; increment"
            " profile_version before this changed meaning can be selected"
        )
        self.identity = identity
        self.expected_topic = expected_topic
        self.profile_version = profile_version


def _sentinels_json(sentinels: frozenset[float]) -> str:
    """The checkpoint-A-proven deterministic encoding (docs/ARCHITECTURE.md §25.2.2), for a set."""
    return json.dumps(sorted(sentinels), separators=(",", ":"), allow_nan=False)


def series_semantics(row: SeriesRow) -> ProfileSemantics:
    """The persisted counterpart of ``history_profile.profile_semantics``, decoded from storage."""
    return ProfileSemantics(row.identity, row.expected_topic, row.profile_version, row.unit,
                            row.kind, row.semantic_type, frozenset(json.loads(row.sentinels_json)),
                            row.min_value, row.max_value, row.energy)


def resolve_series_id(session, profile: HistoryProfile, created_at: int) -> int:
    """Get-or-create the series row for ``profile``, failing closed on any semantic conflict.

    ``Session.get_or_create_series`` recovers an existing row's id under the
    unique ``(identity, expected_topic, profile_version)`` constraint without
    ever mutating that row (§25.2.8: an existing row's ``label`` in
    particular is never touched). The recovered id alone proves nothing about
    that row's *other* columns, so this always follows up with a locking/
    current read (``lock_series``) and verifies the complete stored semantic
    definition equals the current code's, before the id is trusted for a new
    revision. A prior code change to an existing ``(identity, topic,
    version)``'s semantics without a version bump raises
    ``SeriesDefinitionConflict`` instead of silently reusing the stale row.
    """
    sentinels_json = _sentinels_json(profile.sentinels)
    series_id = session.get_or_create_series(
        profile.identity, profile.expected_topic, profile.profile_version, profile.label,
        profile.unit, profile.kind, profile.semantic_type, sentinels_json, profile.min_value,
        profile.max_value, profile.energy, created_at)
    stored = session.lock_series(series_id)
    assert stored is not None, "get_or_create_series returned an id with no row"
    if series_semantics(stored) != profile_semantics(profile):
        raise SeriesDefinitionConflict(profile.identity, profile.expected_topic,
                                       profile.profile_version)
    return series_id


def resolve_effective_from_minute(recorder_safe_from: int, clock_after_lock: float,
                                  latest_committed_minute: int | None,
                                  head_effective_from: int) -> int:
    """Pure §25.2.1 Part 8 formula: the later of every safety floor, minute-aligned."""
    candidates = [recorder_safe_from, floor_minute(clock_after_lock) + MINUTE, head_effective_from]
    if latest_committed_minute is not None:
        candidates.append(latest_committed_minute + MINUTE)
    return max(candidates)


@dataclass(frozen=True, slots=True)
class RevisionInfo:
    id: int
    effective_from_minute: int


@dataclass(frozen=True, slots=True)
class MemberInfo:
    identity: str
    expected_topic: str
    profile_version: int
    label: str
    unit: str | None
    kind: str
    semantic_type: str
    energy: bool
    blocked_reason: BlockReason | None


@dataclass(frozen=True, slots=True)
class SelectionView:
    active_revision: RevisionInfo
    head_revision: RevisionInfo
    pending: bool
    active_members: tuple[MemberInfo, ...]
    head_members: tuple[MemberInfo, ...]


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    revision: RevisionInfo
    idempotent_replay: bool


def _member_info(row: SeriesRow, topics: dict[str, str | None]) -> MemberInfo:
    reason = drift_reason(row.identity, row.expected_topic, row.profile_version,
                          HISTORY_PROFILES_BY_IDENTITY, topics,
                          persisted_semantics=series_semantics(row))
    return MemberInfo(row.identity, row.expected_topic, row.profile_version, row.label, row.unit,
                      row.kind, row.semantic_type, row.energy, reason)


def _resolve_active_revision_id(storage_session, head_id: int, minute_ts: int) -> int:
    """Timeline truth (§25.2.1): the latest revision in the chain whose
    ``effective_from_minute <= minute_ts``; ties resolve to the latest descendant."""
    revision_id = head_id
    while True:
        row = storage_session.read_revision(revision_id)
        if row is None:
            raise RuntimeError(f"optional policy revision chain broken at revision {revision_id}")
        rid, base_id, effective_from, _created_at = row
        if effective_from <= minute_ts or base_id is None:
            return rid
        revision_id = base_id


def read_selection(storage: Storage, now: float) -> SelectionView:
    """``GET /api/v1/optional-history/selection``: active-vs-pending, DB-backed, database-truth only."""
    minute_ts = floor_minute(now)
    topics = capability_topics()
    with storage.session() as s:
        head_id = s.read_policy_head()
        head_row = s.read_revision(head_id)
        assert head_row is not None, "policy head points at a missing revision"
        active_id = _resolve_active_revision_id(s, head_id, minute_ts)
        active_row = s.read_revision(active_id) if active_id != head_id else head_row
        assert active_row is not None
        active_members = tuple(_member_info(r, topics) for r in s.read_revision_members(active_id))
        head_members = (active_members if active_id == head_id
                        else tuple(_member_info(r, topics) for r in s.read_revision_members(head_id)))
    return SelectionView(
        active_revision=RevisionInfo(active_row[0], active_row[2]),
        head_revision=RevisionInfo(head_row[0], head_row[2]),
        pending=active_id != head_id,
        active_members=active_members,
        head_members=head_members,
    )


def _resolve_profiles(identities: Sequence[str]) -> list[HistoryProfile]:
    if len(set(identities)) != len(identities):
        raise ValueError("'identities' contains duplicates")
    profiles = []
    for identity in identities:
        profile = HISTORY_PROFILES_BY_IDENTITY.get(identity)
        if profile is None:
            raise ValueError(f"unknown identity: {identity}")
        profiles.append(profile)
    topics = capability_topics()
    for profile in profiles:
        reason = drift_reason(profile.identity, profile.expected_topic, profile.profile_version,
                              HISTORY_PROFILES_BY_IDENTITY, topics)
        if reason is not None:
            raise ValueError(f"{profile.identity} is not currently selectable: {reason}")
    return profiles


def replace_selection(recorder, storage: Storage, base_revision: int, identities: Sequence[str],
                      clock: Callable[[], float]) -> ReplaceResult:
    """``PUT /api/v1/optional-history/selection``: whole-selection replacement (§25.2.1 Part 8/13).

    ``recorder`` is a ``pompa.recorder.Recorder``; its lock is acquired and
    released by ``safe_future_minute`` *before* any database I/O here, never
    held across it.
    """
    profiles = _resolve_profiles(identities)
    requested_keys = sorted((p.identity, p.expected_topic, p.profile_version) for p in profiles)

    recorder_safe_from = recorder.safe_future_minute(clock())

    with storage.session() as s:
        head_id = s.lock_policy_head()
        # lock_revision/lock_revision_members, not the plain read_* forms: lock_policy_head just
        # proved head_id current via a locking read, but this transaction's own snapshot may
        # still predate the commit that created it (docs/ARCHITECTURE.md §25.2.1) -- an ordinary
        # SELECT could show it as absent even though the lock already proved it committed.
        head_row = s.lock_revision(head_id)
        assert head_row is not None, "policy head points at a missing revision"
        _, head_base_id, head_effective_from, _ = head_row

        if head_id != base_revision:
            if head_base_id == base_revision:
                head_members = s.lock_revision_members(head_id)
                head_keys = sorted((m.identity, m.expected_topic, m.profile_version)
                                   for m in head_members)
                if head_keys == requested_keys:
                    # Coarse identity match alone is not enough (§25.2.8): a replay must not
                    # bypass the semantic-snapshot check. Every requested_keys entry came from a
                    # *current* profile, so an identity/topic/version match already rules out
                    # profile_missing/topic_changed/version_changed for every head member here;
                    # the only way one can still disagree is a semantic field changed in code
                    # without a version bump, which must fail closed, never replay as success.
                    profiles_by_key = {(p.identity, p.expected_topic, p.profile_version): p
                                       for p in profiles}
                    for member in head_members:
                        current = profiles_by_key[(member.identity, member.expected_topic,
                                                   member.profile_version)]
                        if series_semantics(member) != profile_semantics(current):
                            raise SeriesDefinitionConflict(member.identity, member.expected_topic,
                                                           member.profile_version)
                    return ReplaceResult(RevisionInfo(head_id, head_effective_from),
                                         idempotent_replay=True)
            raise StaleBaseRevision(
                f"base_revision {base_revision} is stale; current head is {head_id}")

        now = clock()
        latest_minute = s.lock_latest_minute_ts()
        effective_from = resolve_effective_from_minute(recorder_safe_from, now, latest_minute,
                                                        head_effective_from)
        created_at = int(now)

        series_ids = [resolve_series_id(s, p, created_at) for p in profiles]
        new_revision_id = s.insert_revision(head_id, effective_from, created_at)
        s.insert_revision_members(new_revision_id, series_ids)
        s.update_policy_head(new_revision_id)

    return ReplaceResult(RevisionInfo(new_revision_id, effective_from), idempotent_replay=False)
