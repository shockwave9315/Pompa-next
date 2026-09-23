"""Stage 4B checkpoint B: the ``HistoryProfile`` domain model (``docs/ARCHITECTURE.md`` §25.2.1).

A ``HistoryProfile`` is never a canonical ``Metric``: canonical logical metric
keys, physical capability identities (Stage 4A, ``capabilities.py``) and
history-profile identities are three distinct namespaces. Nothing here is
inferred from a topic or description substring, a payload's generic
Stage 4A ``kind``, its ``available`` fact, or a legacy heuristic; every field
below is either directly evidenced by the tracked reference
(``docs/reference/heishamon/``) or an explicit, non-heuristic catalog fact
from the legacy repository, and unevidenced sentinel/range fields are left
empty/``None`` rather than guessed.
"""

from __future__ import annotations

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
    profile_version: int  # positive; a topic/meaning change requires a new version, never a mutation
    label: str
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


def _temp_profile(identity: str, topic: str, label: str) -> HistoryProfile:
    # docs/reference/heishamon/MQTT-Topics.md documents each as "(°C)"; the legacy repository's
    # explicit (non-heuristic) catalog assigns TEMPERATURE_SENTINELS = (-78, -128) to each of these
    # same identities, matching Pompa Next's own canonical convention (catalog.TEMP_SENTINELS)
    # applied uniformly to every canonical temperature source; realne_dane.md directly observed
    # -128 on two of them (Defrost_Temp, Ipm_Temp). No documented min/max exists for any of them.
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
    # XTOP1/XTOP4: Stage 4A-verified extra/ topics (capabilities._VERIFIED_XTOP_TOPICS), observed
    # 0 W in realne_dane.md, factually cooling power in W (energy=True). The legacy explicit catalog
    # assigns POWER_SENTINELS (-200) to these extra/ identities, but Pompa Next's own canonical
    # catalog deliberately does not assign TOP_POWER_SENTINELS to any XTOP source (only to the TOP
    # fallback), and no XTOP sentinel reading has been directly observed — so this stays unknown
    # rather than importing the legacy assumption, per the "unknown evidence stays unknown" rule.
    # No min/max is documented for either identity, and canonical catalog.min_value=0.0 for the
    # four canonical power metrics is this project's own curated convention, not evidence about
    # these specific, never-canonical cooling channels; left None rather than assumed.
    HistoryProfile("XTOP1", "extra/Cool_Power_Consumption_Extra", 1, "Pobór mocy chłodzenia", "W",
                   "mean", "measurement", frozenset(), None, None, True),
    HistoryProfile("XTOP4", "extra/Cool_Power_Production_Extra", 1, "Moc chłodnicza", "W",
                   "mean", "measurement", frozenset(), None, None, True),
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


BlockReason = Literal["profile_missing", "profile_version_changed", "topic_changed",
                      "capability_topic_changed"]


def history_profile_dict(profile: HistoryProfile, topics: Mapping[str, str | None]) -> dict:
    """The factual public projection of one code-side ``HistoryProfile`` (§25.2, Part 10).

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
                 profiles: Mapping[str, HistoryProfile], topics: Mapping[str, str | None]
                 ) -> BlockReason | None:
    """Whether a persisted (or about-to-be-persisted) series meaning is still selectable.

    Compares one immutable historical meaning ``(identity, expected_topic,
    profile_version)`` against the *current* code (``profiles``) and the
    *current* Stage 4A effective capability topics (``topics``). ``None``
    means selectable; a reason means blocked (``docs/ARCHITECTURE.md``
    §25.2.1) without mutating or deleting anything the caller already
    persisted.
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
    return None
