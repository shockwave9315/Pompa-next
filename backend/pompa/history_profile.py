"""Stage 4B checkpoint B: the ``HistoryProfile`` domain model (``docs/ARCHITECTURE.md`` §25.2.1/§25.2.8).

A ``HistoryProfile`` is never a canonical ``Metric``: canonical logical metric
keys, physical capability identities (Stage 4A, ``capabilities.py``) and
history-profile identities are three distinct namespaces. Nothing here is
inferred from a topic or description substring, a payload's generic
Stage 4A ``kind``, its ``available`` fact, or a legacy heuristic; every field
below is either directly evidenced by the tracked reference
(``docs/reference/heishamon/``), an explicit, non-heuristic catalog fact from
the legacy repository, or an explicitly labelled project design choice, and
unevidenced sentinel/range fields are left empty/``None`` rather than guessed.

One code-side ``HistoryProfile`` version is exactly one immutable *semantic*
definition (``ProfileSemantics``, everything except ``label``): changing a
presentation label never requires a new ``profile_version``, but changing
``expected_topic``, ``unit``, ``kind``, ``semantic_type``, ``sentinels``,
``min_value``, ``max_value`` or ``energy`` on an already-selected identity
does. ``label`` is presentation metadata / the first-seen historical
presentation snapshot; an already-persisted series keeps its own stored
label forever, regardless of later code changes.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal, Mapping

from .capabilities import Capability, effective_capabilities
from .catalog import METRICS, Outcome, parse_numeric

Kind = Literal["mean", "last"]
SemanticType = Literal["measurement", "counter"]

# Identities already serving a canonical logical metric (any priority source of any of the
# 21 recorded metrics) can never also be an optional-history selection; asserted from the
# actual canonical catalog, never hand-copied as a second denylist.
_CANONICAL_SOURCE_IDENTITIES: frozenset[str] = frozenset(
    source.id for metric in METRICS for source in metric.sources
)


@dataclass(frozen=True, slots=True)
class HistoryProfile:
    identity: str  # physical identity, e.g. "TOP21" or "XTOP1" (Stage 4A capability namespace)
    expected_topic: str  # the Stage 4A effective capability topic this profile's meaning binds to
    profile_version: int  # positive; a semantic change requires a new version, never a mutation
    label: str  # presentation only; never part of the semantic definition (see module docstring)
    unit: str | None
    kind: Kind
    semantic_type: SemanticType
    sentinels: frozenset[float]
    min_value: float | None
    max_value: float | None
    energy: bool

    def __post_init__(self) -> None:
        if self.identity in _CANONICAL_SOURCE_IDENTITIES:
            raise ValueError(f"{self.identity} already serves a canonical metric source")
        if self.profile_version < 1:
            raise ValueError(f"{self.identity}: profile_version must be positive")
        if self.energy and self.kind != "mean":
            raise ValueError(f"{self.identity}: energy requires kind='mean'")
        if not all(math.isfinite(s) for s in self.sentinels):
            raise ValueError(f"{self.identity}: sentinels must be finite")
        if self.min_value is not None and not math.isfinite(self.min_value):
            raise ValueError(f"{self.identity}: min_value must be finite")
        if self.max_value is not None and not math.isfinite(self.max_value):
            raise ValueError(f"{self.identity}: max_value must be finite")
        if (self.min_value is not None and self.max_value is not None
                and self.min_value > self.max_value):
            raise ValueError(f"{self.identity}: min_value must not exceed max_value")


def _temp_profile(identity: str, topic: str, label: str) -> HistoryProfile:
    # Evidence, kept precisely separated by source:
    # - docs/reference/heishamon/MQTT-Topics.md documents this identity, its topic and its "(°C)"
    #   meaning; it does NOT document any sentinel value for it.
    # - the legacy repository's explicit (non-heuristic) catalog fact assigns
    #   TEMPERATURE_SENTINELS = (-78, -128) to all six of these same identities.
    # - docs/reference/heishamon/realne_dane.md directly observes -128 on two of them
    #   (Defrost_Temp, Ipm_Temp) in its one checked-in snapshot; -78 is not directly observed there.
    # Using {-78, -128} for all six is therefore a project decision supported by that evidence and
    # by Pompa Next's own existing canonical temperature convention (catalog.TEMP_SENTINELS applied
    # uniformly to every canonical temperature source), not a claim that the tracked reference
    # itself documents these sentinel values. No documented min/max exists for any of them.
    return HistoryProfile(identity, topic, 1, label, "°C", "mean", "measurement",
                          frozenset({-78.0, -128.0}), None, None, False)


HISTORY_PROFILES: tuple[HistoryProfile, ...] = (
    _temp_profile("TOP21", "main/Outside_Pipe_Temp", "Temperatura rury zewnętrznej"),
    _temp_profile("TOP50", "main/Discharge_Temp", "Temperatura tłoczenia sprężarki"),
    _temp_profile("TOP51", "main/Inside_Pipe_Temp", "Temperatura rury wewnętrznej"),
    _temp_profile("TOP52", "main/Defrost_Temp", "Temperatura odszraniania"),
    _temp_profile("TOP53", "main/Eva_Outlet_Temp", "Temperatura wylotu parownika"),
    _temp_profile("TOP55", "main/Ipm_Temp", "Temperatura modułu IPM"),
    # main/Fan2_Motor_Speed "(R/Min)": no sentinel/range documented or observed for this identity.
    HistoryProfile("TOP63", "main/Fan2_Motor_Speed", 1, "Obroty wentylatora 2", "r/min", "mean",
                   "measurement", frozenset(), None, None, False),
    # main/High_Pressure and main/Low_Pressure "(Kgf/Cm2)": no sentinel/range documented or observed.
    HistoryProfile("TOP64", "main/High_Pressure", 1, "Ciśnienie wysokie", "kgf/cm2", "mean",
                   "measurement", frozenset(), None, None, False),
    HistoryProfile("TOP66", "main/Low_Pressure", 1, "Ciśnienie niskie", "kgf/cm2", "mean",
                   "measurement", frozenset(), None, None, False),
    # main/Room_Heater_Operations_Hours, main/DHW_Heater_Operations_Hours "(Hour)": cumulative
    # operating-time counters, like canonical operations_hours (kind="last"); no sentinel documented.
    HistoryProfile("TOP90", "main/Room_Heater_Operations_Hours", 1, "Czas pracy grzałki CO", "h",
                   "last", "counter", frozenset(), None, None, False),
    HistoryProfile("TOP91", "main/DHW_Heater_Operations_Hours", 1, "Czas pracy grzałki CWU", "h",
                   "last", "counter", frozenset(), None, None, False),
    # main/Pump_Duty: no unit given in the tracked reference; the legacy explicit catalog fact
    # records unit="duty" for this same identity, which we reuse as-is rather than inventing "%".
    HistoryProfile("TOP93", "main/Pump_Duty", 1, "Wysterowanie pompy", "duty", "mean",
                   "measurement", frozenset(), None, None, False),
    # main/Expansion_Valve "(Steps)": no sentinel/range documented or observed.
    HistoryProfile("TOP142", "main/Expansion_Valve", 1, "Zawór rozprężny", "steps", "mean",
                   "measurement", frozenset(), None, None, False),
    # XTOP1/XTOP4 v1 (owner decision, frozen): evidence is kept precisely separated by claim.
    # - W identity/meaning ("Cool_Power_Consumption_Extra"/"Cool_Power_Production_Extra", observed
    #   0 W in realne_dane.md; exact topics from Stage 4A's verified capability model): tracked
    #   evidence.
    # - -200 sentinel: an explicit, non-heuristic legacy product catalog fact (POWER_SENTINELS
    #   assigned to these same two identities). docs/reference/heishamon/MQTT-Topics.md does NOT
    #   document -200 for XTOP identities; this is legacy catalog evidence, not tracked-reference
    #   evidence.
    # - min_value=0.0 (rejecting any other negative reading): a PROJECT DESIGN CHOICE, not
    #   evidence from either source, deliberately aligned with Pompa Next's own existing canonical
    #   power algebra (catalog._power(..., min_value=0.0)). It exists so an unknown negative
    #   cooling-power value can never automatically become valid historical energy: fail closed
    #   on the unevidenced case instead of silently accepting every finite number. No optional
    #   data has ever been persisted for these identities, so this v1 definition needed no
    #   migration to correct.
    HistoryProfile("XTOP1", "extra/Cool_Power_Consumption_Extra", 1, "Pobór mocy chłodzenia", "W",
                   "mean", "measurement", frozenset({-200.0}), 0.0, None, True),
    HistoryProfile("XTOP4", "extra/Cool_Power_Production_Extra", 1, "Moc chłodnicza", "W",
                   "mean", "measurement", frozenset({-200.0}), 0.0, None, True),
)

HISTORY_PROFILES_BY_IDENTITY: dict[str, HistoryProfile] = {p.identity: p for p in HISTORY_PROFILES}
assert len(HISTORY_PROFILES_BY_IDENTITY) == len(HISTORY_PROFILES), "duplicate HistoryProfile identity"


def parse_history_profile_value(profile: HistoryProfile, payload: str) -> tuple[float | None, Outcome]:
    """Parse one payload against a ``HistoryProfile``, via the shared primitive.

    No production caller exists yet (checkpoint C adds ``OptionalAccumulator``);
    this is proved correct now so that future caller can be trusted immediately.
    """
    return parse_numeric(payload, profile.sentinels, profile.min_value, profile.max_value)


def capability_topics(capabilities: tuple[Capability, ...] | None = None) -> dict[str, str | None]:
    """The current live topic for every effective capability identity."""
    caps = effective_capabilities() if capabilities is None else capabilities
    return {c.reference.identity: c.topic for c in caps}


# ------------------------------------------------------------------ semantic definition


@dataclass(frozen=True, slots=True)
class ProfileSemantics:
    """The comparable, versioned *meaning* of a ``HistoryProfile``: everything except its
    presentation ``label``. Two profiles with equal ``ProfileSemantics`` are the same
    historical meaning; a label may differ freely without creating a new series or
    requiring a ``profile_version`` bump (see module docstring)."""

    identity: str
    expected_topic: str
    profile_version: int
    unit: str | None
    kind: Kind
    semantic_type: SemanticType
    sentinels: frozenset[float]
    min_value: float | None
    max_value: float | None
    energy: bool


def profile_semantics(profile: HistoryProfile) -> ProfileSemantics:
    return ProfileSemantics(profile.identity, profile.expected_topic, profile.profile_version,
                            profile.unit, profile.kind, profile.semantic_type, profile.sentinels,
                            profile.min_value, profile.max_value, profile.energy)


def semantic_fingerprint(profile: HistoryProfile) -> str:
    """A deterministic digest of one profile's semantic definition (label excluded).

    Golden-value guard (docs/ARCHITECTURE.md §25.2.8): a test pins the expected digest
    of every existing ``HistoryProfile``. If this ever changes for an *existing*
    ``(identity, profile_version)`` pair, the fix is to increment ``profile_version``
    on the intended new meaning, never to update the test's expected digest — that
    would silently accept a stored-series definition conflict (§25.2.8, ``PUT``/``GET``
    drift as ``profile_definition_changed``).
    """
    semantics = profile_semantics(profile)
    payload = json.dumps(
        {
            "identity": semantics.identity,
            "expected_topic": semantics.expected_topic,
            "profile_version": semantics.profile_version,
            "unit": semantics.unit,
            "kind": semantics.kind,
            "semantic_type": semantics.semantic_type,
            "sentinels": sorted(semantics.sentinels),
            "min_value": semantics.min_value,
            "max_value": semantics.max_value,
            "energy": semantics.energy,
        },
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


BlockReason = Literal["profile_missing", "profile_version_changed", "topic_changed",
                      "capability_topic_changed", "profile_definition_changed"]


def history_profile_dict(profile: HistoryProfile, topics: Mapping[str, str | None]) -> dict:
    """The factual public projection of one code-side ``HistoryProfile`` (§25.2, Part 10).

    ``selectable``/``blocked_reason`` here describe the *current code-side profile and
    current Stage 4A capability* only — this call never compares against a persisted
    ``optional_series`` row, so ``profile_definition_changed`` never appears here and
    this endpoint stays free of any database dependency. A stored-policy conflict for an
    already-selected series is database state, reported only by the selection GET/PUT
    surface (`GET`/`PUT /api/v1/optional-history/selection`).

    Sentinels and min/max stay backend-internal: no concrete frontend/product
    need has been identified for exposing them yet.
    """
    reason = drift_reason(profile.identity, profile.expected_topic, profile.profile_version,
                          HISTORY_PROFILES_BY_IDENTITY, topics)
    return {
        "identity": profile.identity,
        "topic": profile.expected_topic,
        "profile_version": profile.profile_version,
        "label": profile.label,
        "unit": profile.unit,
        "kind": profile.kind,
        "semantic_type": profile.semantic_type,
        "energy": profile.energy,
        "selectable": reason is None,
        "blocked_reason": reason,
    }


def drift_reason(identity: str, expected_topic: str, profile_version: int,
                 profiles: Mapping[str, HistoryProfile], topics: Mapping[str, str | None],
                 persisted_semantics: ProfileSemantics | None = None) -> BlockReason | None:
    """Whether a persisted (or about-to-be-persisted) series meaning is still selectable.

    Compares one immutable historical meaning ``(identity, expected_topic,
    profile_version)`` against the *current* code (``profiles``) and the
    *current* Stage 4A effective capability topics (``topics``). ``None``
    means selectable; a reason means blocked (``docs/ARCHITECTURE.md``
    §25.2.1/§25.2.8) without mutating or deleting anything the caller already
    persisted.

    A capability that is currently missing from the effective catalog
    entirely surfaces as ``capability_topic_changed`` (its topic lookup
    returns ``None``, which can never equal a real ``expected_topic``); a
    dedicated ``capability_missing`` reason is not worth a fifth vocabulary
    entry unless it later needs to be told apart from an ordinary topic
    change.

    ``persisted_semantics``, when given, is the *actual* stored
    ``ProfileSemantics`` of an existing ``optional_series`` row for this
    exact ``(identity, expected_topic, profile_version)`` tuple. When it
    disagrees with the current code's own semantics for that tuple —
    identity/topic/version match, but ``unit``/``kind``/``semantic_type``/
    ``sentinels``/``min_value``/``max_value``/``energy`` do not — that is
    ``profile_definition_changed``: a code-definition error (a semantic
    field changed without a ``profile_version`` bump), not a fact about the
    device. Omitted (``None``) for the pre-persistence selectability check,
    which has no existing row to compare against.
    """
    profile = profiles.get(identity)
    if profile is None:
        return "profile_missing"
    if profile.profile_version != profile_version:
        return "profile_version_changed"
    if profile.expected_topic != expected_topic:
        return "topic_changed"
    if topics.get(identity) != expected_topic:
        return "capability_topic_changed"
    if persisted_semantics is not None and persisted_semantics != profile_semantics(profile):
        return "profile_definition_changed"
    return None
