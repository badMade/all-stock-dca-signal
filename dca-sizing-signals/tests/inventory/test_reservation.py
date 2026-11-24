"""The pure math: one-sided q on cost basis, variance time, shrink-only multiplier."""

from __future__ import annotations

import math

import pytest

from inventory_signals import (
    SLOTS,
    inventory_excess,
    max_buy_price,
    normalized_profile,
    remaining_weight,
    reservation_discount,
    size_multiplier,
)

from ._inputs import PROFILE


@pytest.mark.parametrize("committed,target,expected", [
    (50.0, 100.0, 0.0),     # underweight is neutral, never negative
    (100.0, 100.0, 0.0),
    (200.0, 100.0, 1.0),    # twice target
    (1000.0, 100.0, 2.0),   # capped at q_max
])
def test_inventory_excess(committed, target, expected):
    assert inventory_excess(committed, target) == pytest.approx(expected)


@pytest.mark.parametrize("args", [(-1.0, 100.0), (10.0, 0.0), (math.nan, 1.0), (True, 1.0)])
def test_inventory_excess_rejects_bad_inputs(args):
    with pytest.raises(ValueError):
        inventory_excess(*args)


def test_gamma_calibration_q1_full_day_is_z_sigmas():
    # Median-vol name, q = 1, whole day ahead, no widening: discount = z * sigma.
    delta, v = reservation_discount(1.0, 0.03, 0.03, 1.0, 0.0, z=1.0, cap_sigmas=10.0)
    assert delta == pytest.approx(0.03)
    assert v == pytest.approx(0.0009)


def test_discount_is_capped():
    delta, _ = reservation_discount(2.0, 0.05, 0.01, 1.0, 0.0, z=1.5, cap_sigmas=2.0)
    assert delta == pytest.approx(0.10)


def test_zero_q_leaves_only_volatility_widening():
    delta, _ = reservation_discount(0.0, 0.03, 0.03, 1.0, 0.004, kappa=0.5, b0_bps=10.0)
    assert delta == pytest.approx(0.001 + 0.002)


def test_negative_q_rejected():
    with pytest.raises(ValueError):
        reservation_discount(-0.5, 0.03, 0.03, 1.0, 0.0)


def test_remaining_weight_shrinks_toward_close():
    prof = normalized_profile(PROFILE)
    left = [remaining_weight(prof, s) for s in SLOTS]
    assert left[0] == pytest.approx(1.0)
    assert left == sorted(left, reverse=True)
    assert left[-1] == pytest.approx(prof["15:30"])  # the TWAP limit: only the last interval


@pytest.mark.parametrize("bad", [
    {k: v for k, v in PROFILE.items() if k != "12:30"},
    {**PROFILE, "16:00": 0.1},
    {**PROFILE, "09:35": -0.1},
    {s: 0.0 for s in SLOTS},
    {**PROFILE, "10:30": math.inf},
])
def test_profile_validation(bad):
    with pytest.raises(ValueError):
        normalized_profile(bad)


def test_size_multiplier_shrink_only():
    assert size_multiplier(0.0) == 1.0
    assert size_multiplier(1.0) == pytest.approx(0.5)
    assert size_multiplier(100.0) == pytest.approx(0.25)  # floor
    assert size_multiplier(0.0, gamma_m=0.0) == 1.0


def test_max_buy_price():
    assert max_buy_price(10.0, 0.0) == pytest.approx(10.0)
    assert max_buy_price(10.0, math.log(2)) == pytest.approx(5.0)
    with pytest.raises(ValueError):
        max_buy_price(0.0, 0.1)
