"""Stage 4B checkpoint B: the ``HistoryProfile`` domain model (docs/ARCHITECTURE.md §25.2.1).

Pure domain tests; no database. The exact initial identity set, its topics,
kinds and evidence are asserted here so any future drift is caught at once.
"""

import pytest

from pompa.capabilities import effective_capabilities
from pompa.catalog import METRICS, Outcome
from pompa.history_profile import (
    HISTORY_PROFILES, HISTORY_PROFILES_BY_IDENTITY, HistoryProfile, capability_topics,
    drift_reason, history_profile_dict, parse_history_profile_value, profile_semantics,
    semantic_fingerprint,
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

# docs/reference/heishamon/MQTT-Topics.md documents these six as continuous "(°C)" readings and
# their identities/topics only; the legacy explicit catalog fact assigns TEMPERATURE_SENTINELS to
# all six, matching Pompa Next's own canonical TEMP_SENTINELS convention (the tracked reference
# itself does not document a sentinel value for any of them -- see history_profile._temp_profile).
TEMPERATURE_IDENTITIES = {"TOP21", "TOP50", "TOP51", "TOP52", "TOP53", "TOP55"}
COUNTER_IDENTITIES = {"TOP90", "TOP91"}  # cumulative operating-time counters -> kind="last"
# XTOP1/XTOP4 v1 (owner decision, §25.2.8): -200 sentinel from an explicit legacy catalog fact,
# min_value=0.0 a project design choice aligned with canonical power algebra -- not documented by
# the tracked reference and not evidence, deliberately distinct from every other empty-evidence case.
POWER_IDENTITIES = {"XTOP1", "XTOP4"}


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


def test_non_temperature_non_power_profiles_have_empty_sentinels_and_no_documented_range():
    """No sentinel or min/max is documented or observed for any of these identities;
    unknown evidence stays unknown rather than an invented, physically-plausible bound."""
    for identity in EXPECTED_IDENTITIES - TEMPERATURE_IDENTITIES - POWER_IDENTITIES:
        profile = HISTORY_PROFILES_BY_IDENTITY[identity]
        assert profile.sentinels == frozenset(), identity
        assert profile.min_value is None and profile.max_value is None, identity


def test_xtop_verified_topics_and_power_energy_metadata():
    xtop1, xtop4 = HISTORY_PROFILES_BY_IDENTITY["XTOP1"], HISTORY_PROFILES_BY_IDENTITY["XTOP4"]
    assert xtop1.expected_topic == "extra/Cool_Power_Consumption_Extra"
    assert xtop4.expected_topic == "extra/Cool_Power_Production_Extra"
    assert xtop1.unit == xtop4.unit == "W"
    assert xtop1.energy and xtop4.energy
    # v1 owner decision (§25.2.8): -200 sentinel from an explicit legacy catalog fact; min_value=0.0
    # is a project design choice, not evidence, so an unevidenced negative value fails closed.
    assert xtop1.sentinels == xtop4.sentinels == frozenset({-200.0})
    assert xtop1.min_value == xtop4.min_value == 0.0
    assert xtop1.max_value is None and xtop4.max_value is None


@pytest.mark.parametrize("payload, expected", [
    ("0", Outcome.VALID),
    ("50", Outcome.VALID),
    ("-200", Outcome.SENTINEL),
    ("-1", Outcome.REJECTED),
    ("nan", Outcome.REJECTED),
    ("inf", Outcome.REJECTED),
])
def test_xtop_power_v1_parse_semantics(payload, expected):
    for identity in POWER_IDENTITIES:
        outcome = parse_history_profile_value(HISTORY_PROFILES_BY_IDENTITY[identity], payload)[1]
        assert outcome is expected, (identity, payload)


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


@pytest.mark.parametrize("sentinels, min_value, max_value", [
    (frozenset({float("nan")}), None, None),
    (frozenset({float("inf")}), None, None),
    (frozenset(), float("nan"), None),
    (frozenset(), None, float("inf")),
    (frozenset(), 10.0, 5.0),  # min > max
])
def test_history_profile_rejects_non_finite_or_inverted_range(sentinels, min_value, max_value):
    with pytest.raises(ValueError):
        HistoryProfile("TOP21", "main/Outside_Pipe_Temp", 1, "x", "°C", "mean", "measurement",
                       sentinels, min_value, max_value, False)


def test_history_profile_accepts_equal_min_and_max():
    HistoryProfile("TOP21", "main/Outside_Pipe_Temp", 1, "x", "°C", "mean", "measurement",
                   frozenset(), 5.0, 5.0, False)  # must not raise


# ------------------------------------------------------------------ semantic snapshot / label policy


def test_label_is_not_semantic():
    a = HistoryProfile("TOP21", "main/Outside_Pipe_Temp", 1, "Label A", "°C", "mean", "measurement",
                       frozenset({-78.0}), None, None, False)
    b = HistoryProfile("TOP21", "main/Outside_Pipe_Temp", 1, "Label B", "°C", "mean", "measurement",
                       frozenset({-78.0}), None, None, False)
    assert a.label != b.label
    assert profile_semantics(a) == profile_semantics(b)
    assert semantic_fingerprint(a) == semantic_fingerprint(b)


@pytest.mark.parametrize("field, value", [
    ("unit", "K"),
    ("kind", "last"),
    ("semantic_type", "counter"),
    ("sentinels", frozenset({-1.0})),
    ("min_value", 0.0),
    ("max_value", 100.0),
    ("energy", True),
    ("expected_topic", "main/Something_Else"),
])
def test_any_semantic_field_change_changes_the_fingerprint(field, value):
    base = HistoryProfile("TOP21", "main/Outside_Pipe_Temp", 1, "x", "°C", "mean", "measurement",
                          frozenset({-78.0}), None, None, False)
    kwargs = {
        "identity": base.identity, "expected_topic": base.expected_topic,
        "profile_version": base.profile_version, "label": base.label, "unit": base.unit,
        "kind": base.kind, "semantic_type": base.semantic_type, "sentinels": base.sentinels,
        "min_value": base.min_value, "max_value": base.max_value, "energy": base.energy,
    }
    kwargs[field] = value
    changed = HistoryProfile(**kwargs)
    assert profile_semantics(base) != profile_semantics(changed)
    assert semantic_fingerprint(base) != semantic_fingerprint(changed)


# ------------------------------------------------------------------ golden semantic-version guard
#
# Pins the current semantic definition of every existing HistoryProfile. If a future code change
# makes any of these digests differ, the correct fix is a new profile_version on the changed
# meaning -- never updating the expected digest below, which would silently accept a stored-series
# definition conflict (docs/ARCHITECTURE.md §25.2.8).

EXPECTED_SEMANTIC_FINGERPRINTS = {
    "TOP21": "ea78e7869dfcd83221367ffe9d329d63834a1f610e9d151b20be951a3f3565b2",
    "TOP50": "483fe55d83472be4a5d591abe84ccb0e362e3d1667c25a79f99579391039b865",
    "TOP51": "595deb19ab0740a78a9ab8f2a3b03bd474f248099a0a63a66caabfb68f4c83f9",
    "TOP52": "6d2ddc08c35d6fca568ff291b122983d12515ec0dc2d4b96251b17751a5bbdec",
    "TOP53": "40bf665860310b6cef1ccd6c830c43ad1ad89b882167830b11afb53b4829bae3",
    "TOP55": "81b6b9ff1e288507971d14af73711b4ee8ba8dd47b05af901616e5d672801baa",
    "TOP63": "aeb13b791e0fca73b618f5b03827ca791575e1a47891cc86ed09df8c13db080b",
    "TOP64": "05d720e2d2c4df981424ca4b89f5322263668b3b9b1cb303bd6f925db4fd4427",
    "TOP66": "222fd89f26a4e4045f1584e70173afd934b528098a44058dd0bad515cf8646ee",
    "TOP90": "6a8c566952d8949995496c6acb8fd28c61c13e7728848ee816ad6f58449e8f91",
    "TOP91": "43c5f179af1b4aa55b9e6bcae0fbe4a426ee41ae7d3f425b67913985626b35ad",
    "TOP93": "ba559291359c4766c12baf5280cd7aee389590497a9cd4281a4b5e9213fe31f7",
    "TOP142": "d40c29f4a7e98cd25748ad0ec560f7bf5247dfbb6be924fdee71efa088f71eae",
    "XTOP1": "a7825362a4a11bc619c3f9359a8e5cca2427469fd98ab5b00091b27a0e006978",
    "XTOP4": "c06040a5a52b99f62427cc756bd03143c9252b4672d6c5e7970b17399e99402c",
}


def test_golden_semantic_fingerprints_are_pinned():
    assert set(EXPECTED_SEMANTIC_FINGERPRINTS) == EXPECTED_IDENTITIES
    for identity, expected in EXPECTED_SEMANTIC_FINGERPRINTS.items():
        got = semantic_fingerprint(HISTORY_PROFILES_BY_IDENTITY[identity])
        assert got == expected, (
            f"{identity} semantic definition changed. If this is intentional, increment its"
            f" profile_version -- do not update this expected digest ({got!r})."
        )


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


def test_drift_reason_profile_definition_changed():
    """Same (identity, topic, version), but the persisted semantic snapshot disagrees with
    current code: a code-definition error, not a fact about the device or capability."""
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    topics = {"TOP21": profile.expected_topic}
    stale = profile_semantics(HistoryProfile(
        "TOP21", profile.expected_topic, profile.profile_version, "old label", "K", "mean",
        "measurement", frozenset({-1.0}), None, None, False))
    assert drift_reason("TOP21", profile.expected_topic, profile.profile_version,
                        HISTORY_PROFILES_BY_IDENTITY, topics,
                        persisted_semantics=stale) == "profile_definition_changed"


def test_drift_reason_ignores_persisted_semantics_when_coarse_fields_already_differ():
    """profile_missing/version/topic/capability take priority; they are checked before any
    semantic-snapshot comparison, so a mismatched coarse field is reported precisely."""
    profile = HISTORY_PROFILES_BY_IDENTITY["TOP21"]
    stale = profile_semantics(profile)
    assert drift_reason("TOP21", profile.expected_topic, 99, HISTORY_PROFILES_BY_IDENTITY,
                        {"TOP21": profile.expected_topic},
                        persisted_semantics=stale) == "profile_version_changed"


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
