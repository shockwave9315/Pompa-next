"""Aggregation algebra: Stats, derived series, energy, paired COP, coverage."""

import random

import pytest

from pompa.aggregation import (
    DERIVED_KEYS, SERIES, Stats, combine, combine_maps, cop, coverage_percent, derive, energy_kwh,
    fold, fold_minutes,
)
from pompa.catalog import RECORDED_KEYS

T0 = 1_800_000_000


def minute(**values):
    return {k: values.get(k) for k in RECORDED_KEYS}


def power(co_in=None, co_out=None, dhw_in=None, dhw_out=None):
    return minute(co_power_consumption=co_in, co_power_production=co_out,
                  dhw_power_consumption=dhw_in, dhw_power_production=dhw_out)


# ------------------------------------------------------------------ Stats


def test_of_and_combine():
    assert Stats.of(4.0) == Stats(1, 4.0, 4.0, 4.0, 4.0)
    assert combine(Stats.of(4.0), Stats.of(-1.0)) == Stats(2, 3.0, -1.0, 4.0, -1.0)
    assert Stats(2, 3.0, -1.0, 4.0, -1.0).avg == 1.5


def test_empty_is_no_stats_not_zeros():
    assert fold([]) is None
    assert fold([None, None]) is None
    assert combine(None, None) is None
    s = Stats.of(2.0)
    assert combine(None, s) is s and combine(s, None) is s


def test_real_zero_counts():
    assert fold([0.0]) == Stats(1, 0.0, 0.0, 0.0, 0.0)
    assert fold([0.0, None, 0.0]) == Stats(2, 0.0, 0.0, 0.0, 0.0)


def test_last_is_chronological_not_extreme():
    assert fold([5.0, 1.0, 3.0]).last == 3.0
    a, b = fold([5.0]), fold([1.0])
    assert combine(a, b).last == 1.0 and combine(b, a).last == 5.0  # not commutative


def test_trailing_null_keeps_last_known_value():
    s = fold([2.0, 7.0, None, None])
    assert s == Stats(2, 9.0, 2.0, 7.0, 7.0)


def test_associativity_exhaustive_small_integers():
    # Integer-valued floats make every sum exact, so equality must be exact.
    values = [Stats(1, 3.0, 3.0, 3.0, 3.0), Stats(2, -1.0, -4.0, 3.0, 3.0), Stats(3, 9.0, 0.0, 5.0, 0.0), None]
    for a in values:
        for b in values:
            for c in values:
                assert combine(combine(a, b), c) == combine(a, combine(b, c))


def test_pre_aggregated_equals_direct_fold():
    rng = random.Random(7)
    vals = [None if rng.random() < 0.2 else float(rng.randint(-50, 5000)) for _ in range(600)]
    direct = fold(vals)
    for size in (1, 5, 60, 137):
        chunks = [fold(vals[i:i + size]) for i in range(0, len(vals), size)]
        acc = None
        for c in chunks:
            acc = combine(acc, c)
        assert acc == direct


# ------------------------------------------------------------------ derived series


def test_derive_recorded_and_all_catalog_keys():
    d = derive(minute(main_outlet_temp=35.0))
    assert set(d) == set(SERIES)
    assert d["recorded"] == 1.0 and d["main_outlet_temp"] == 35.0 and d["outside_temp"] is None
    assert all(d[k] is None for k in DERIVED_KEYS if k != "recorded")


def test_pairs_require_both_channels_in_the_same_minute():
    d = derive(power(co_in=900.0, co_out=None, dhw_in=0.0, dhw_out=0.0))
    assert (d["pair_co_in"], d["pair_co_out"]) == (None, None)
    assert (d["pair_dhw_in"], d["pair_dhw_out"]) == (0.0, 0.0)  # real zeros pair
    assert (d["pair_total_in"], d["pair_total_out"]) == (None, None)


def test_total_pair_requires_all_four_channels():
    d = derive(power(900.0, 3600.0, 18.0, 0.0))
    assert (d["pair_co_in"], d["pair_co_out"]) == (900.0, 3600.0)
    assert (d["pair_dhw_in"], d["pair_dhw_out"]) == (18.0, 0.0)
    assert (d["pair_total_in"], d["pair_total_out"]) == (918.0, 3600.0)


def test_fold_minutes_requires_ascending_order():
    rows = [(T0 + 60, minute(outside_temp=1.0)), (T0, minute(outside_temp=2.0))]
    with pytest.raises(ValueError):
        fold_minutes(rows)
    with pytest.raises(ValueError):
        fold_minutes([(T0, minute()), (T0, minute())])


def test_fold_minutes_only_known_series():
    rows = [(T0, minute(outside_temp=1.0)), (T0 + 60, minute(outside_temp=None)),
            (T0 + 120, power(10.0, 30.0))]
    acc = fold_minutes(rows)
    assert acc["recorded"] == Stats(3, 3.0, 1.0, 1.0, 1.0)
    assert acc["outside_temp"] == Stats(1, 1.0, 1.0, 1.0, 1.0)
    assert acc["pair_co_in"] == Stats.of(10.0)
    assert "pair_dhw_in" not in acc and "main_outlet_temp" not in acc


def test_combine_maps_matches_fold_of_concatenation():
    rows = [(T0 + 60 * i, power(float(i), float(3 * i), 1.0 if i % 3 else None, 0.0)) for i in range(120)]
    split = combine_maps(fold_minutes(rows[:37]), fold_minutes(rows[37:]))
    assert split == fold_minutes(rows)


# ------------------------------------------------------------------ energy and COP


def test_energy_from_incomplete_minutes_is_not_extrapolated():
    # 3 known minutes of 1200 W inside a 60-minute window: 3 * 1200 / 60000 kWh, not 1.2 kWh.
    s = fold([1200.0, None, 1200.0, 1200.0])
    assert energy_kwh(s) == 0.06 and s.n == 3
    assert energy_kwh(None) is None
    assert energy_kwh(fold([0.0, 0.0])) == 0.0


def cop_of(minutes):
    acc = fold_minutes([(T0 + 60 * i, m) for i, m in enumerate(minutes)])
    return {name: cop(acc.get(f"pair_{name}_in"), acc.get(f"pair_{name}_out")) for name in ("co", "dhw", "total")}


def test_paired_cop_hand_calculated():
    got = cop_of([
        power(1000.0, 4000.0, 0.0, 0.0),
        power(500.0, 1000.0, 18.0, 0.0),  # 18 W in / 0 W out: valid, lowers DHW and total COP
        power(1000.0, None, 0.0, 0.0),  # CO unpaired: excluded from CO and total
        power(None, None, 600.0, 1800.0),  # DHW only
    ])
    assert got["co"] == {"cop": 5000.0 / 1500.0, "paired_minutes": 2,
                         "input_kwh": 1500.0 / 60000, "output_kwh": 5000.0 / 60000}
    assert got["dhw"] == {"cop": 1800.0 / 618.0, "paired_minutes": 4,
                          "input_kwh": 618.0 / 60000, "output_kwh": 1800.0 / 60000}
    # Total: minutes 0 and 1 only: in 1000+518, out 4000+1000.
    assert got["total"] == {"cop": 5000.0 / 1518.0, "paired_minutes": 2,
                            "input_kwh": 1518.0 / 60000, "output_kwh": 5000.0 / 60000}


def test_period_cop_is_not_average_of_minute_cops():
    got = cop_of([power(1000.0, 5000.0), power(100.0, 100.0)])["co"]
    assert got["cop"] == 5100.0 / 1100.0  # 4.636…, whereas mean(5, 1) would be 3
    assert got["cop"] != (5.0 + 1.0) / 2


def test_cop_zero_denominator_and_no_pairs():
    zero = cop_of([power(0.0, 0.0), power(0.0, 0.0)])["co"]
    assert zero == {"cop": None, "paired_minutes": 2, "input_kwh": 0.0, "output_kwh": 0.0}
    none = cop_of([power(10.0, None)])["co"]
    assert none == {"cop": None, "paired_minutes": 0, "input_kwh": None, "output_kwh": None}


def test_zero_zero_minute_is_paired_but_adds_no_denominator():
    got = cop_of([power(0.0, 0.0), power(1000.0, 3000.0)])["co"]
    assert got["paired_minutes"] == 2 and got["cop"] == 3.0


def test_cop_rejects_unpaired_stats():
    with pytest.raises(ValueError):
        cop(Stats.of(1.0), None)
    with pytest.raises(ValueError):
        cop(fold([1.0, 2.0]), Stats.of(1.0))


def test_coverage_percent():
    assert coverage_percent(0, 0) is None
    assert coverage_percent(0, 60) == 0.0
    assert coverage_percent(59, 60) == 98.3
    assert coverage_percent(1380, 1380) == 100.0
