"""Tests for volatility.py: the optional GARCH(1,1) band-width component.

This is deliberately NOT an OHLCForecaster. It has no view on direction; it
produces one number per ticker per day -- how wide tomorrow's band should be
relative to that ticker's own long-run width -- and any model's bands can be
scaled by it. That is what "volatility clustering, formalized instead of
approximated by realized_vol_5 / realized_vol_20" buys.

Written before the implementation. The same three tiers as
tests/test_kalman.py:

1. **Closed-form identities.** Variance targeting is an exact algebraic
   constraint (omega = sample_var * (1 - alpha - beta), so the model's own
   unconditional variance IS the sample variance), and the recursion is
   three lines of arithmetic this file writes out by hand. Both are checked
   against numbers derived here, not against the module.
2. **Does the estimator actually estimate?** Simulate a GARCH(1,1) with
   parameters chosen in this file and assert the fit recovers them; then
   simulate i.i.d. returns -- no clustering at all -- and assert the fitted
   conditional path stays flat. The comparative form (clustered data moves
   the band, unclustered data does not) is the assertion that would catch a
   fit that "works" by fitting noise.
3. **Contract properties.** The recursion is causal, the scale is clipped,
   an unknown or too-short ticker gets a neutral 1.0 rather than a
   confident wrong number, applying a scale leaves p50 alone and re-imposes
   bar geometry, and state round-trips as plain JSON-able floats.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from data import COMPONENTS
from features import build_panel

volatility = pytest.importorskip("volatility", reason="optional volatility component is not present in this checkout")


def _simulate_garch(*, omega: float, alpha: float, beta: float, n: int, seed: int) -> np.ndarray:
    """A GARCH(1,1) return path, generated here so the truth is this file's.

    Started at the unconditional variance, which is also where
    ``conditional_variances`` starts -- so a fit on this path is estimating
    exactly the process that produced it, with no burn-in mismatch to excuse
    a bad recovery.
    """
    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal(n)
    sigma2 = omega / (1.0 - alpha - beta)
    returns = np.empty(n, dtype=np.float64)
    for step in range(n):
        returns[step] = math.sqrt(sigma2) * shocks[step]
        sigma2 = omega + alpha * returns[step] ** 2 + beta * sigma2
    return returns


@pytest.fixture
def panel(synthetic_history_dir):
    probe = build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[int(len(probe.calendar) * 0.6)])
    return build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


# --------------------------------------------------------------------------
# 1. Closed-form identities
# --------------------------------------------------------------------------


class TestGarchParams:
    def test_unconditional_variance_is_the_stationary_identity(self):
        params = volatility.GarchParams(omega=2e-6, alpha=0.08, beta=0.90)
        assert params.persistence == pytest.approx(0.98, rel=1e-12)
        assert params.unconditional == pytest.approx(2e-6 / (1.0 - 0.98), rel=1e-12)

    @pytest.mark.parametrize(
        "omega, alpha, beta",
        [
            (0.0, 0.08, 0.90),  # omega must be strictly positive
            (-1e-6, 0.08, 0.90),
            (1e-6, -0.01, 0.90),  # negative ARCH term -> negative variances
            (1e-6, 0.08, -0.01),
            (1e-6, 0.50, 0.60),  # persistence >= 1 -> no unconditional variance
            (1e-6, 0.10, 0.90),
        ],
    )
    def test_refuses_parameters_with_no_stationary_variance(self, omega, alpha, beta):
        # Fail closed: a non-stationary or negative parameterization has no
        # unconditional variance to target, so it must not be constructible
        # at all rather than produce a NaN or negative band width later.
        with pytest.raises(ValueError):
            volatility.GarchParams(omega=omega, alpha=alpha, beta=beta)


class TestVarianceTargeting:
    def test_targeted_omega_reproduces_the_sample_variance_exactly(self):
        returns = _simulate_garch(omega=2e-6, alpha=0.08, beta=0.90, n=3000, seed=3)
        params = volatility.fit_garch(returns)
        sample_variance = float(np.mean(returns**2))
        # Variance targeting is not "roughly matched", it is an algebraic
        # constraint imposed before the search starts: whatever (alpha, beta)
        # the search lands on, omega is pinned so the model's own long-run
        # variance equals the sample's.
        assert params.unconditional == pytest.approx(sample_variance, rel=1e-10)
        assert params.omega == pytest.approx(sample_variance * (1.0 - params.persistence), rel=1e-10)

    def test_fit_always_lands_inside_the_stationary_region(self):
        for seed in range(5):
            returns = _simulate_garch(omega=2e-6, alpha=0.08, beta=0.90, n=1500, seed=seed)
            params = volatility.fit_garch(returns)
            assert params.omega > 0.0
            assert params.alpha >= 0.0
            assert params.beta >= 0.0
            assert params.persistence <= volatility.MAX_PERSISTENCE < 1.0


class TestRecursion:
    def test_conditional_variances_match_hand_arithmetic(self):
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        returns = np.array([0.01, -0.02, 0.005], dtype=np.float64)
        path = volatility.conditional_variances(returns, params)
        # sigma2[k] is the conditional variance OF returns[k], so it may only
        # use returns[:k]. sigma2[0] therefore uses no data at all.
        first = params.unconditional
        second = params.omega + params.alpha * 0.01**2 + params.beta * first
        third = params.omega + params.alpha * (-0.02) ** 2 + params.beta * second
        assert path.shape == returns.shape
        assert path[0] == pytest.approx(first, rel=1e-12)
        assert path[1] == pytest.approx(second, rel=1e-12)
        assert path[2] == pytest.approx(third, rel=1e-12)

    def test_one_step_forecast_extends_the_path_by_one(self):
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        returns = np.array([0.01, -0.02, 0.005], dtype=np.float64)
        path = volatility.conditional_variances(returns, params)
        expected = params.omega + params.alpha * returns[-1] ** 2 + params.beta * path[-1]
        assert volatility.forecast_variance(returns, params) == pytest.approx(expected, rel=1e-12)

    def test_the_recursion_is_causal(self):
        # The whole no-lookahead argument for this module in one assertion:
        # rewriting the tail of the return series may not move any earlier
        # conditional variance. The final entry must move, or the test would
        # pass on a function that ignores its input.
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        rng = np.random.default_rng(5)
        returns = rng.normal(0.0, 0.015, size=200)
        cutoff = 120
        perturbed = returns.copy()
        perturbed[cutoff:] *= 6.0
        base_path = volatility.conditional_variances(returns, params)
        perturbed_path = volatility.conditional_variances(perturbed, params)
        np.testing.assert_array_equal(base_path[: cutoff + 1], perturbed_path[: cutoff + 1])
        assert perturbed_path[-1] != base_path[-1]

    def test_negative_log_likelihood_is_minimized_at_the_fitted_parameters(self):
        # Proves the search actually searched. A fit that returned its
        # starting point, or a grid that never evaluated the objective,
        # fails here even though every other test in this file would pass.
        returns = _simulate_garch(omega=2e-6, alpha=0.08, beta=0.90, n=3000, seed=11)
        params = volatility.fit_garch(returns)
        best = volatility.negative_log_likelihood(returns, params)
        sample_variance = float(np.mean(returns**2))
        for d_alpha, d_beta in ((0.02, 0.0), (-0.02, 0.0), (0.0, 0.02), (0.0, -0.02), (0.01, 0.01)):
            alpha = params.alpha + d_alpha
            beta = params.beta + d_beta
            if alpha < 0.0 or beta < 0.0 or alpha + beta >= volatility.MAX_PERSISTENCE:
                continue
            neighbour = volatility.GarchParams(
                omega=sample_variance * (1.0 - alpha - beta), alpha=alpha, beta=beta
            )
            assert best <= volatility.negative_log_likelihood(returns, neighbour) + 1e-9


# --------------------------------------------------------------------------
# 2. Does the estimator actually estimate?
# --------------------------------------------------------------------------


class TestParameterRecovery:
    def test_recovers_parameters_from_a_simulated_garch_path(self):
        returns = _simulate_garch(omega=2e-6, alpha=0.08, beta=0.90, n=8000, seed=17)
        params = volatility.fit_garch(returns)
        # Persistence (alpha + beta) is the well-identified quantity -- it is
        # what sets how long a shock stays in the band -- so it gets the
        # tight tolerance; the split between alpha and beta is noisier and
        # gets a looser one. These are quasi-MLE tolerances at n=8000, not
        # aspirations.
        assert params.persistence == pytest.approx(0.98, abs=0.02)
        assert params.alpha == pytest.approx(0.08, abs=0.05)
        assert params.beta == pytest.approx(0.90, abs=0.06)

    def test_clustered_returns_move_the_band_and_iid_returns_do_not(self):
        clustered = _simulate_garch(omega=2e-6, alpha=0.10, beta=0.88, n=6000, seed=23)
        rng = np.random.default_rng(29)
        flat = rng.normal(0.0, float(np.std(clustered)), size=6000)

        def band_variation(returns: np.ndarray) -> float:
            params = volatility.fit_garch(returns)
            sigma = np.sqrt(volatility.conditional_variances(returns, params))
            return float(np.std(sigma) / np.mean(sigma))

        clustered_variation = band_variation(clustered)
        flat_variation = band_variation(flat)
        # The comparative claim is the real one: the component must respond
        # to clustering that is there and stay quiet when it is not. An
        # estimator that simply fits noise fails the second half.
        assert clustered_variation > flat_variation
        assert clustered_variation > 0.15
        assert flat_variation < 0.10

    def test_a_volatility_shock_widens_the_next_days_scale(self):
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        quiet = np.full(200, 0.001, dtype=np.float64)
        shocked = quiet.copy()
        shocked[-1] = 0.15  # one very large move on the last day
        calm = volatility.forecast_variance(quiet, params)
        after = volatility.forecast_variance(shocked, params)
        assert after > calm
        # alpha is the whole mechanism: the jump is exactly alpha * r^2.
        assert after - calm == pytest.approx(params.alpha * (0.15**2 - 0.001**2), rel=1e-12)


# --------------------------------------------------------------------------
# 3. Contract properties
# --------------------------------------------------------------------------


class TestScale:
    def test_scale_is_one_when_conditional_equals_unconditional(self):
        component = volatility.GarchVolatility()
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        component._params["AAA"] = params
        component._sigma2["AAA"] = params.unconditional
        assert component.scale("AAA") == pytest.approx(1.0, rel=1e-12)

    def test_scale_is_the_ratio_of_conditional_to_unconditional_standard_deviation(self):
        component = volatility.GarchVolatility()
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        component._params["AAA"] = params
        component._sigma2["AAA"] = 2.25 * params.unconditional
        assert component.scale("AAA") == pytest.approx(1.5, rel=1e-12)

    def test_scale_is_clipped_at_both_ends(self):
        component = volatility.GarchVolatility(min_scale=0.5, max_scale=2.5)
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        component._params["AAA"] = params
        component._sigma2["AAA"] = 10_000.0 * params.unconditional
        assert component.scale("AAA") == pytest.approx(2.5, rel=1e-12)
        component._sigma2["AAA"] = 1e-9 * params.unconditional
        assert component.scale("AAA") == pytest.approx(0.5, rel=1e-12)

    def test_unknown_ticker_scores_a_neutral_one(self):
        # "No opinion" is 1.0, not a guess borrowed from another ticker and
        # not a refusal: this component only ever widens or narrows a band
        # some other model already produced, so the safe default is to leave
        # that band exactly as it was.
        component = volatility.GarchVolatility()
        assert component.scale("NEVER-SEEN") == 1.0

    def test_a_ticker_with_too_little_history_is_left_neutral(self, panel):
        component = volatility.GarchVolatility(min_observations=10_000)
        stats = component.fit(panel, seed=0)
        assert stats["fitted"] == 0
        assert stats["skipped_short_history"] > 0
        assert component.scale("AAA") == 1.0


class TestOnlineUpdate:
    def test_observe_matches_extending_the_offline_path(self):
        # GARCH is recursive by construction, so its "online update" should
        # be the same arithmetic as the batch path -- not an approximation of
        # it. Fit on the first n returns, observe the n+1'th, and assert the
        # state equals a batch fit that had seen all of them.
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        rng = np.random.default_rng(31)
        returns = rng.normal(0.0, 0.015, size=300)
        component = volatility.GarchVolatility()
        component._params["AAA"] = params
        component._sigma2["AAA"] = volatility.forecast_variance(returns[:-1], params)
        component.observe("AAA", float(returns[-1]))
        assert component._sigma2["AAA"] == pytest.approx(volatility.forecast_variance(returns, params), rel=1e-12)

    def test_observe_is_order_dependent(self):
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)

        def after(sequence):
            component = volatility.GarchVolatility()
            component._params["AAA"] = params
            component._sigma2["AAA"] = params.unconditional
            for value in sequence:
                component.observe("AAA", value)
            return component._sigma2["AAA"]

        assert after([0.001, 0.08]) != pytest.approx(after([0.08, 0.001]), rel=1e-9)

    def test_observing_an_unknown_ticker_is_a_no_op(self):
        component = volatility.GarchVolatility()
        component.observe("NEVER-SEEN", 0.05)
        assert component.scale("NEVER-SEEN") == 1.0


class TestApply:
    @staticmethod
    def _band(**offsets) -> dict[str, dict[str, float]]:
        return {
            component: {"p10": value - 0.02, "p50": value, "p90": value + 0.02}
            for component, value in offsets.items()
        }

    def test_median_is_untouched_and_the_band_scales_around_it(self):
        component = volatility.GarchVolatility()
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        component._params["AAA"] = params
        component._sigma2["AAA"] = 2.25 * params.unconditional  # scale 1.5
        band = self._band(open=0.0, high=0.03, low=-0.03, close=0.0)
        scaled = component.apply(band, ticker="AAA")
        for name in COMPONENTS:
            assert scaled[name]["p50"] == pytest.approx(band[name]["p50"], abs=1e-15)
            assert scaled[name]["p90"] - scaled[name]["p50"] == pytest.approx(0.02 * 1.5, rel=1e-12)
            assert scaled[name]["p50"] - scaled[name]["p10"] == pytest.approx(0.02 * 1.5, rel=1e-12)

    def test_a_neutral_ticker_leaves_the_band_identical(self):
        component = volatility.GarchVolatility()
        band = self._band(open=0.0, high=0.03, low=-0.03, close=0.0)
        assert component.apply(band, ticker="NEVER-SEEN") == band

    def test_scaling_re_imposes_bar_geometry(self):
        # The same trap stacking.blend() documents: a per-component width
        # change can push a level's high below its open even when every
        # input level was a possible bar. apply() must repair rather than
        # emit an impossible bar, so the assertion is on the OUTPUT geometry.
        component = volatility.GarchVolatility()
        params = volatility.GarchParams(omega=2e-6, alpha=0.10, beta=0.85)
        component._params["AAA"] = params
        component._sigma2["AAA"] = 4.0 * params.unconditional  # scale 2.0
        band = {
            "open": {"p10": -0.001, "p50": 0.0, "p90": 0.001},
            "high": {"p10": 0.0005, "p50": 0.0006, "p90": 0.0007},
            "low": {"p10": -0.05, "p50": -0.04, "p90": -0.03},
            "close": {"p10": -0.001, "p50": 0.0, "p90": 0.001},
        }
        scaled = component.apply(band, ticker="AAA")
        for name in COMPONENTS:
            assert scaled[name]["p10"] <= scaled[name]["p50"] <= scaled[name]["p90"]
        for key in ("p10", "p50", "p90"):
            level = {name: scaled[name][key] for name in COMPONENTS}
            assert level["high"] >= max(level["open"], level["close"])
            assert level["low"] <= min(level["open"], level["close"])


class TestPanelFit:
    def test_fits_every_eligible_ticker_inside_the_stationary_region(self, panel):
        component = volatility.GarchVolatility(min_observations=60)
        stats = component.fit(panel, seed=0)
        assert stats["fitted"] > 0
        for ticker, params in component._params.items():
            assert params.omega > 0.0, ticker
            assert 0.0 <= params.alpha, ticker
            assert 0.0 <= params.beta, ticker
            assert params.persistence <= volatility.MAX_PERSISTENCE, ticker
            assert component._sigma2[ticker] > 0.0, ticker

    def test_refit_replaces_state_instead_of_advancing_it(self, panel):
        component = volatility.GarchVolatility(min_observations=60)
        component.fit(panel, seed=0)
        first = dict(component._sigma2)
        component.fit(panel, seed=0)
        assert component._sigma2 == first

    def test_fit_ignores_returns_after_the_panels_cutoff(self, panel):
        # Same trainability rule the rest of the subproject uses: a return
        # whose day had not happened by the fit cutoff was not available to
        # fit against, so it may not be in the estimate.
        component = volatility.GarchVolatility(min_observations=60)
        stats = component.fit(panel, seed=0)
        cutoff = np.datetime64(str(panel.normalization["window_end"]))
        series = panel.series["AAA"]
        available = int(np.count_nonzero(series.dates <= cutoff))
        assert stats["observations"]["AAA"] <= available


class TestCheckpointing:
    def test_state_round_trips_through_plain_json_able_floats(self, panel):
        import json

        component = volatility.GarchVolatility(min_observations=60)
        component.fit(panel, seed=0)
        state = component.to_state()
        # Round-tripping through json is the assertion, not a convenience:
        # this state has to survive a checkpoint that never pickles.
        restored = volatility.GarchVolatility.from_state(json.loads(json.dumps(state)))
        assert set(restored._params) == set(component._params)
        for ticker, params in component._params.items():
            assert restored._params[ticker].omega == params.omega
            assert restored._params[ticker].alpha == params.alpha
            assert restored._params[ticker].beta == params.beta
            assert restored._sigma2[ticker] == component._sigma2[ticker]
            assert restored.scale(ticker) == component.scale(ticker)

    def test_round_trip_preserves_the_clip_bounds(self, panel):
        import json

        component = volatility.GarchVolatility(min_observations=60, min_scale=0.8, max_scale=1.2)
        component.fit(panel, seed=0)
        restored = volatility.GarchVolatility.from_state(json.loads(json.dumps(component.to_state())))
        assert restored.min_scale == 0.8
        assert restored.max_scale == 1.2
