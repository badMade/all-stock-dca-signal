"""Pure math: OLS, OU fit, s-score, the raw multiplier band and budget neutrality."""

from __future__ import annotations

import math
import random

import pytest

from residual_reversal import budget_neutral, cumulate, fit_ou, ols, raw_multiplier, s_score


def test_ols_recovers_exact_line():
    x = [0.0, 1.0, 2.0, 3.0, 4.0]
    fit = ols(x, [2.0 + 3.0 * v for v in x])
    assert fit.alpha == pytest.approx(2.0)
    assert fit.beta == pytest.approx(3.0)
    assert fit.r_squared == pytest.approx(1.0)
    assert all(abs(r) < 1e-12 for r in fit.residuals)


@pytest.mark.parametrize("x,y", [([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]), ([1.0, 2.0], [1.0, 2.0]),
                                 ([1.0, 2.0, math.nan], [1.0, 2.0, 3.0]), ([1.0, 2.0, 3.0], [1.0, 2.0])])
def test_ols_rejects_degenerate_input(x, y):
    with pytest.raises(ValueError):
        ols(x, y)


def test_cumulate():
    assert cumulate([1.0, -2.0, 0.5]) == (1.0, -1.0, -0.5)


def test_fit_ou_recovers_ar1_coefficient():
    rng = random.Random(7)
    x, xs = 0.0, []
    for _ in range(5000):
        x = 0.01 + 0.8 * x + 0.02 * rng.gauss(0.0, 1.0)
        xs.append(x)
    fit = fit_ou(xs)
    assert fit.b == pytest.approx(0.8, abs=0.02)
    assert fit.m == pytest.approx(0.05, abs=0.01)  # a / (1 - b)
    assert fit.sigma_eq == pytest.approx(0.02 / math.sqrt(1 - 0.64), rel=0.05)
    assert fit.half_life_sessions == pytest.approx(math.log(2) / -math.log(fit.b))
    assert fit.kappa == pytest.approx(-math.log(fit.b) * 252)


def test_fit_ou_refuses_a_trending_series():
    with pytest.raises(ValueError, match="outside"):
        fit_ou([float(k) for k in range(30)])  # b == 1 exactly: no mean reversion


def test_s_score_and_centering():
    rng = random.Random(3)
    x, xs = 0.0, []
    for _ in range(200):
        x = 0.7 * x + rng.gauss(0.0, 1.0)
        xs.append(x)
    fit = fit_ou(xs)
    assert s_score(fit) == pytest.approx((fit.last_x - fit.m) / fit.sigma_eq)
    assert s_score(fit, mean_shift=fit.m) == pytest.approx(fit.last_x / fit.sigma_eq)


def test_raw_multiplier_band_direction_and_clip():
    assert raw_multiplier(0.0, 1.25, 0.1, 0.75, 1.25) == 1.0
    assert raw_multiplier(-1.24, 1.25, 0.1, 0.75, 1.25) == 1.0
    assert raw_multiplier(1.24, 1.25, 0.1, 0.75, 1.25) == 1.0
    assert raw_multiplier(-1.5, 1.25, 0.1, 0.75, 1.25) == pytest.approx(1.15)  # cheap leans up
    assert raw_multiplier(1.5, 1.25, 0.1, 0.75, 1.25) == pytest.approx(0.85)   # rich leans down
    assert raw_multiplier(-9.0, 1.25, 0.1, 0.75, 1.25) == 1.25
    assert raw_multiplier(9.0, 1.25, 0.1, 0.75, 1.25) == 0.75
    with pytest.raises(ValueError):
        raw_multiplier(math.inf, 1.25, 0.1, 0.75, 1.25)


def test_budget_neutral_keeps_dollars_and_bounds():
    raw = {"A": 1.25, "B": 1.25, "C": 1.0, "D": 0.9}
    w = {"A": 40.0, "B": 10.0, "C": 25.0, "D": 25.0}
    out = budget_neutral(raw, w, 0.75, 1.25)
    assert math.fsum(w[t] * m for t, m in out.items()) == pytest.approx(100.0, rel=1e-12)
    assert all(0.75 <= m <= 1.25 for m in out.values())
    # Order is preserved: one common shift never swaps two names.
    assert out["A"] >= out["C"] >= out["D"]


def test_budget_neutral_all_neutral_is_exactly_one():
    assert budget_neutral({"A": 1.0, "B": 1.0}, {"A": 5.0, "B": 7.0}, 0.75, 1.25) == {"A": 1.0, "B": 1.0}


def test_budget_neutral_single_name_cannot_tilt():
    out = budget_neutral({"A": 1.25}, {"A": 10.0}, 0.75, 1.25)
    assert out["A"] == pytest.approx(1.0)


def test_budget_neutral_rejects_bad_arguments():
    with pytest.raises(ValueError):
        budget_neutral({"A": 1.0}, {"B": 1.0}, 0.75, 1.25)
    with pytest.raises(ValueError):
        budget_neutral({"A": 1.0}, {"A": 0.0}, 0.75, 1.25)
    with pytest.raises(ValueError):
        budget_neutral({"A": 1.0}, {"A": 1.0}, 1.1, 1.25)
