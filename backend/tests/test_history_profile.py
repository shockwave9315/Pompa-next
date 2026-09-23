"""Stage 4B checkpoint B: the ``HistoryProfile`` domain model (docs/ARCHITECTURE.md §25.2.1).

Pure domain tests; no database. The exact initial identity set, its topics,
kinds and evidence are asserted here so any future drift is caught at once.
"""

from pompa.capabilities import effective_capabilities
from pompa.catalog import METRICS
from pompa.history_profile import (
    HISTORY_PROFILES, HISTORY_PROFILES_BY_IDENTITY, capability_topics, drift_reason,
    history_profile_dict, parse_history_profile_value,
)

EXPECTED_IDENTITIES = {
    "TOP21", "TOP50", "TOP51", "TOP52", "TOP53", "TOP55", "TOP63", "TOP64", "TOP66",
    "TOP90", "TOP91", "TOP93", "TOP142", "XTOP1", "XTOP4",
}

EXPECTED_TOPICS = {
    "TOP21": "main/Outside_Pipe_Temp",
    "TOP50": "main/Discharge_Temp",
    "TOP51": "main/Inside_Pipe_Temp",
    "TOP52": "main/Defrost_Temp",
    "TOP53": "main/Eva_Outlet_Temp",
    "TOP55": "main/Ipm_Temp",
    "TOP63": "main/Fan2_Motor_Speed",
    "TOP64": "main/High_Pressure",
    "TOP66": "main/Low_Pressure",
    "TOP90": "main/Room_Heater_Operations_Hours",
    "TOP91": "main/DHW_Heater_Operations_Hours",
    "TOP93": "main/Pump_Duty",
    "TOP142": "main/Expansion_Valve",
    "XTOP1": "extra/Cool_Power_Consumption_Extra",
    "XTOP4": "extra/Cool_Power_Production_Extra",
}

# docs/reference/heishamon/MQTT-Topics.md documents these as continuous "(°C)" readings; the
# legacy explicit catalog fact assigns TEMPERATURE_SENTINELS to every one of them, matching
# Pompa Next's own canonical TEMP_SENTINELS convention.
TEMPERATURE_IDENTITIES = {"TOP21", "TOP50", "TOP51", "TOP52", "TOP53", "TOP55"}
COUNTER_IDENTITIES = {"TOP90", "TOP91"}  # cumulative operating-time counters -> kind="last"


def test_exact_initial_identity_set():
    assert {p.identity for p in HISTORY_PROFILES} == EXPECTED_IDENTITIES
    assert len(HISTORY_PROFILES) == len(EXPECTED_IDENTITIES)  # no duplicate identity


def test_no_duplicate_identity():
    assert len(HISTORY_PROFILES_BY_IDENTITY) == len(HISTORY_PROFILES)


def test_exact_expected_topics():
    for profile in HISTORY_PROFILES:
        assert profile.expected_topic == EXPECTED_TOPICS[profile.identity], profile.identity


def test_current_effective_capability_join():
    """Every profile's ``expected_topic`` matches the live Stage 4A effective capability topic."""
    topics = capability_topics(effective_capabilities())
    for profile in HISTORY_PROFILES:
        assert topics.get(profile.identity) == profile.expected_topic, profile.identity


def test_no_overlap_with_canonical_metric_source_identities():
    """Asserted from the actual canonical catalog, never a hand-copied second denylist."""
    canonical_ids = {source.id for metric in METRICS for source in metric.sources}
    profile_ids = {p.identity for p in HISTORY_PROFILES}
    assert canonical_ids & profile_ids == set()


def test_profile_versions_are_positive():
    for profile in HISTORY_PROFILES:
        assert profile.profile_version >= 1


def test_mean_vs_last_semantics():
    for profile in HISTORY_PROFILES:
        expected_kind = "last" if profile.identity in COUNTER_IDENTITIES else "mean"
        assert profile.kind == expected_kind, profile.identity
        expected_semantic = "counter" if profile.identity in COUNTER_IDENTITIES else "measurement"
        assert profile.semantic_type == expected_semantic, profile.identity


def test_temperature_sentinels_match_canonical_convention():
    for identity in TEMPERATURE_IDENTITIES:
        profile = HISTORY_PROFILES_BY_IDENTITY[identity]
        assert profile.sentinels == frozenset({-78.0, -128.0}), identity
        assert profile.unit == "°C"
        assert profile.min_value is None and profile.max_value is None


def test_non_temperature_profiles_have_empty_sentinels_and_no_documented_range():
    """No sentinel or min/max is documented or observed for any of these identities;
    unknown evidence stays unknown rather than an invented, physically-plausible bound."""
    for identity in EXPECTED_IDENTITIES - TEMPERATURE_IDENTITIES:
        profile = HISTORY_PROFILES_BY_IDENTITY[identity]
        assert profile.sentinels == frozenset(), identity
        assert profile.min_value is None and profile.max_value is None, identity


def test_xtop_verified_topics_and_power_energy_metadata():
    xtop1, xtop4 = HISTORY_PROFILES_BY_IDENTITY["XTOP1"], HISTORY_PROFILES_BY_IDENTITY["XTOP4"]
    assert xtop1.expected_topic == "extra/Cool_Power_Consumption_Extra"
    assert xtop4.expected_topic == "extra/Cool_Power_Production_Extra"
    assert xtop1.unit == xtop4.unit == "W"
    assert xtop1.energy and xtop4.energy
    # Deliberately empty: our own canonical catalog never assigns TOP_POWER_SENTINELS to any
    # XTOP source either, and no XTOP sentinel reading has been directly observed.
    assert xtop1.sentinels == xtop4.sentinels == frozenset()


def test_only_two_identities_carry_energy_true():
    assert {p.identity for p in HISTORY_PROFILES if p.energy} == {"XTOP1", "XTOP4"}


def test_history_profile_rejects_a_canonical_source_identity():
    from pompa.history_profile import HistoryProfile

    try:
        HistoryProfile("TOP16", "main/Heat_Power_Consumption", 1, "x", "W", "mean", "measurement",
                       frozenset(), None, None, False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a canonical-source identity")


def test_history_profile_rejects_non_positive_profile_version():
    from pompa.history_profile import HistoryProfile

    try:
        HistoryProfile("TOP21", "main/Outside_Pipe_Temp", 0, "x", "°C", "mean", "measurement",
                       frozenset(), None, None, False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for profile_version=0")


# ------------------------------------------------------------------ drift/blocking


def test_drift_reason_none_when_everything_matches():
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    topics = {"TOP21": profile.expected_topic}
    assert drift_reason("TOP21", profile.expected_topic, profile.profile_version,
                        HISTORY_PROFILES_BY_IDENTITY, topics) is None


def test_drift_reason_profile_missing():
    assert drift_reason("TOP21", "main/Outside_Pipe_Temp", 1, {}, {}) == "profile_missing"


def test_drift_reason_profile_version_changed():
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    topics = {"TOP21": profile.expected_topic}
    assert drift_reason("TOP21", profile.expected_topic, 99,
                        HISTORY_PROFILES_BY_IDENTITY, topics) == "profile_version_changed"


def test_drift_reason_topic_changed():
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    topics = {"TOP21": profile.expected_topic}
    assert drift_reason("TOP21", "main/Some_Other_Topic", profile.profile_version,
                        HISTORY_PROFILES_BY_IDENTITY, topics) == "topic_changed"


def test_drift_reason_capability_topic_changed():
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    topics = {"TOP21": "main/Something_Else_Now"}
    assert drift_reason("TOP21", profile.expected_topic, profile.profile_version,
                        HISTORY_PROFILES_BY_IDENTITY, topics) == "capability_topic_changed"


def test_history_profile_dict_shape_excludes_sentinels_and_range():
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP52"]
    topics = capability_topics(effective_capabilities())
    entry = history_profile_dict(profile, topics)
    assert set(entry) == {
        "identity", "topic", "profile_version", "label", "unit", "kind", "semantic_type",
        "energy", "selectable", "blocked_reason",
    }
    assert entry["selectable"] is True
    assert entry["blocked_reason"] is None


# ------------------------------------------------------------------ parser primitive


def test_parse_history_profile_value_uses_the_shared_primitive():
    from pompa.catalog import Outcome

    profile = HISTORY_PROFILES_BY_IDENTITY["TOP52"]  # sentinels {-78, -128}
    assert parse_history_profile_value(profile, "23.5") == (23.5, Outcome.VALID)
    assert parse_history_profile_value(profile, "-128") == (None, Outcome.SENTINEL)
    assert parse_history_profile_value(profile, "nan") == (None, Outcome.REJECTED)
    assert parse_history_profile_value(profile, "not a number") == (None, Outcome.REJECTED)
