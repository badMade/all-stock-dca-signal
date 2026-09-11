"""Tests for kalman.py: the optional local-level / local-trend state-space model.

Written before the implementation. Three kinds of assertion appear here, in
descending order of how much they actually prove:

1. **Closed-form identities.** A scalar Kalman filter is not a black box.
   With zero process noise it collapses to a precision-weighted running
   mean; with constant (Q, R) its prediction covariance converges to the
   positive root of a scalar Riccati equation, which in turn makes the
   filter an EWMA with a *known* smoothing factor. Those roots are written
   out here as arithmetic, derived independently of the module, so a
   recursion that looks plausible but folds the noise in at the wrong step
   fails instead of quietly producing well-shaped garbage. Same discipline
   as validate_formulas.py.
2. **Distributional properties.** Bands drawn at +-Phi^-1(0.9) around the
   predictive mean must cover ~80% of fresh draws from the distribution the
   filter believes in.
3. **Contract properties.** predict() is pure, no forecast depends on data
   revealed after its origin day, checkpoints round-trip bit-identically
   (float64, not float32 -- the assertions below are exact), and a recovery
   refit REPLACES state instead of absorbing every training day a second
   time (the bug PersistenceModel.fit() carries a comment about).

The no-lookahead test follows tests/test_features.py's shape: replay twice,
perturb everything after a cutoff, assert nothing at or before the cutoff
moves -- and assert something AFTER it does, so the test cannot pass by
comparing two identical runs.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
from statistics import NormalDist

import model_interface
import numpy as np
import pandas as pd
import pytest
from data import COMPONENTS
from features import build_panel

kalman = pytest.importorskip("kalman", reason="optional kalman component is not present in this checkout")

#: Phi^-1(0.9), derived from the standard library rather than copied from
#: the module under test -- the band half-width constant is exactly the kind
#: of number a typo hides in.
Z90 = NormalDist().inv_cdf(0.9)

WINDOW_LEN = 32


@pytest.fixture
def panel(synthetic_history_dir):
    # Freeze normalization/fit window ~60% through the archive so both an
    # "initial fit" and a stretch of later reveals exist, exactly as
    # tests/test_baseline_model.py does.
    probe = build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[int(len(probe.calendar) * 0.6)])
    return build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


def _requests(panel, ticker, rows, window_len=WINDOW_LEN):
    series = panel.series[ticker]
    return [model_interface.make_request(series, row, window_len) for row in rows]


def _trainable_rows(series) -> list[int]:
    usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
    return [int(row) for row in np.flatnonzero(usable)]


# --------------------------------------------------------------------------
# 1. Closed-form identities
# --------------------------------------------------------------------------


class TestScalarFilterClosedForms:
    def test_zero_process_noise_is_a_precision_weighted_running_mean(self):
        # With Q = 0 the filter accumulates precision and nothing else, so
        # after n observations the level is the exact posterior mean of a
        # Gaussian with a conjugate prior:
        #     level_n = (kappa * level_0 + sum(y)) / (kappa + n),  kappa = R / P_0.
        # Choosing P_0 = R makes kappa exactly 1, which pins the arithmetic
        # to something checkable by hand rather than "approximately a mean".
        r = 0.04
        filt = kalman.ScalarFilter(r=r, q_ratio=0.0, prior_variance=r, prior_level=0.0)
        observations = [0.10, -0.20, 0.30, 0.05, -0.15]
        for value in observations:
            filt.absorb(value)
        expected = sum(observations) / (1.0 + len(observations))
        assert filt.level == pytest.approx(expected, rel=1e-12)

    def test_zero_process_noise_predictive_variance_matches_closed_form(self):
        # Same setting: P_n = R / (kappa + n), and the predictive variance
        # for the next observation is P_n + Q + R = R / (1 + n) + R.
        r = 0.04
        filt = kalman.ScalarFilter(r=r, q_ratio=0.0, prior_variance=r, prior_level=0.0)
        observations = [0.10, -0.20, 0.30, 0.05, -0.15]
        for value in observations:
            filt.absorb(value)
        mean, variance = filt.project()
        assert mean == pytest.approx(sum(observations) / (1.0 + len(observations)), rel=1e-12)
        assert variance == pytest.approx(r / (1.0 + len(observations)) + r, rel=1e-12)

    def test_riccati_root_solves_its_own_fixed_point_equation(self):
        # steady_state_variance(q, r) is the positive root of
        #     p = p * r / (p + r) + q     <=>     p^2 - q*p - q*r = 0.
        # Assert BOTH: the closed form, and that the returned value actually
        # satisfies the recursion it claims to be the fixed point of.
        for q, r in ((0.002, 0.04), (1e-6, 1e-4), (0.5, 0.5)):
            root = kalman.steady_state_variance(q, r)
            closed_form = (q + math.sqrt(q * q + 4.0 * q * r)) / 2.0
            assert root == pytest.approx(closed_form, rel=1e-12)
            assert root * root - q * root - q * r == pytest.approx(0.0, abs=1e-15 + 1e-12 * root * root)
            assert kalman.steady_state_gain(q, r) == pytest.approx(root / (root + r), rel=1e-12)

    def test_prediction_covariance_converges_to_the_riccati_root(self):
        r = 0.04
        q_ratio = 0.05
        expected = kalman.steady_state_variance(q_ratio * r, r)
        filt = kalman.ScalarFilter(r=r, q_ratio=q_ratio, prior_variance=10.0 * r, prior_level=0.0)
        rng = np.random.default_rng(0)
        for value in rng.normal(0.0, math.sqrt(r), size=3000):
            filt.absorb(float(value))
        _, variance = filt.project()
        # project()'s variance is P_pred + R, so subtracting R recovers the
        # Riccati root itself. Convergence is geometric; 3000 steps is far
        # past it, so this is a tight tolerance on purpose.
        assert variance - r == pytest.approx(expected, rel=1e-9)

    def test_steady_state_filter_is_an_ewma_with_the_riccati_gain(self):
        # Started exactly AT the steady state -- P = (1 - K_inf) * p_inf, so
        # the predict step lands back on p_inf and the gain never moves --
        # the update is level <- (1 - K) * level + K * y, i.e. a plain EWMA.
        # This is the single strongest statement about the recursion: it is
        # not "close to" an EWMA, it IS one, with a gain computed from the
        # Riccati root by arithmetic this test does independently.
        r = 0.04
        q_ratio = 0.05
        p_inf = kalman.steady_state_variance(q_ratio * r, r)
        gain = p_inf / (p_inf + r)
        filt = kalman.ScalarFilter(r=r, q_ratio=q_ratio, prior_variance=(1.0 - gain) * p_inf, prior_level=0.0)
        ewma = 0.0
        rng = np.random.default_rng(7)
        for value in rng.normal(0.0, 0.2, size=500):
            ewma = (1.0 - gain) * ewma + gain * float(value)
            filt.absorb(float(value))
            assert filt.level == pytest.approx(ewma, rel=1e-9, abs=1e-15)

    def test_local_trend_recovers_an_exact_line(self):
        # A local-TREND filter on y_k = a + b*k should drive its innovation
        # to zero and forecast the next point. The local-LEVEL contrast in
        # the same test is what makes it meaningful: on a ramp a level-only
        # filter lags by a growing amount, so its innovation does not
        # collapse. Without that contrast a broken trend state could pass by
        # accident on a flat series.
        a, b = 0.5, 0.02
        trend = kalman.ScalarFilter(r=1e-4, q_ratio=0.0, trend=True, prior_variance=1.0)
        trend_innovations = [trend.absorb(a + b * step) for step in range(200)]
        assert max(abs(value) for value in trend_innovations[-20:]) < 1e-5
        mean, _ = trend.project()
        assert mean == pytest.approx(a + b * 200, abs=1e-5)

        level = kalman.ScalarFilter(r=1e-4, q_ratio=0.0, trend=False, prior_variance=1.0)
        level_innovations = [level.absorb(a + b * step) for step in range(200)]
        assert abs(level_innovations[-1]) > 1e-3

    def test_project_does_not_mutate_the_filter(self):
        filt = kalman.ScalarFilter(r=0.04, q_ratio=0.01, prior_variance=0.04, prior_level=0.0)
        for value in (0.1, -0.05, 0.02):
            filt.absorb(value)
        before = copy.deepcopy(filt)
        first = filt.project()
        second = filt.project()
        assert first == second
        assert filt.level == before.level
        assert filt.slope == before.slope
        assert filt.observations == before.observations
        np.testing.assert_array_equal(filt.covariance, before.covariance)

    def test_absorb_returns_the_innovation_it_actually_used(self):
        filt = kalman.ScalarFilter(r=0.04, q_ratio=0.01, prior_variance=0.04, prior_level=0.0)
        mean, _ = filt.project()
        innovation = filt.absorb(0.25)
        assert innovation == pytest.approx(0.25 - mean, rel=1e-12)


# --------------------------------------------------------------------------
# 2. Distributional properties
# --------------------------------------------------------------------------


class TestBandCoverage:
    def test_bands_cover_eighty_percent_of_fresh_draws(self):
        sigma = 0.02
        filt = kalman.ScalarFilter(r=sigma**2, q_ratio=0.0, prior_variance=sigma**2, prior_level=0.0)
        rng = np.random.default_rng(11)
        draws = rng.normal(0.0, sigma, size=20000)
        covered = 0
        for value in draws:
            mean, variance = filt.project()
            half = Z90 * math.sqrt(variance)
            covered += int(mean - half <= value <= mean + half)
            filt.absorb(float(value))
        assert covered / len(draws) == pytest.approx(0.8, abs=0.02)

    def test_default_process_noise_is_slightly_conservative_by_construction(self):
        # No simulation: this is the design choice stated as arithmetic. The
        # default signal-to-noise ratio inflates the predictive standard
        # deviation by ~5%, which turns a nominal 80% band into ~82% -- wide
        # rather than narrow, which is the side to err on for a band the
        # conformal calibrator will subsequently tighten.
        r = 0.0004
        p_inf = kalman.steady_state_variance(kalman.DEFAULT_Q_RATIO * r, r)
        inflation = math.sqrt((p_inf + r) / r)
        assert 1.0 < inflation < 1.10
        realized = 2.0 * NormalDist().cdf(Z90 * inflation) - 1.0
        assert 0.80 < realized < 0.85


# --------------------------------------------------------------------------
# 3. The forecaster on a real panel
# --------------------------------------------------------------------------


class TestKalmanForecasterOnPanel:
    def test_fit_seeds_one_filter_per_ticker_and_component(self, panel):
        model = kalman.KalmanForecaster()
        stats = model.fit(panel, seed=0)
        assert stats["absorbed"] > 0
        assert set(model._filters) <= set(panel.series)
        assert "AAA" in model._filters
        assert set(model._filters["AAA"]) == set(COMPONENTS)
        for component in COMPONENTS:
            assert model._filters["AAA"][component].observations > 0

    def test_fit_ignores_rows_whose_target_had_not_matured(self, panel):
        # The panel's normalization window_end is the fit cutoff: a row whose
        # label_end_date falls after it was not knowable at fit time and must
        # not have been absorbed. Compare the absorbed count against the rows
        # this test selects itself from the panel.
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        cutoff = np.datetime64(str(panel.normalization["window_end"]))
        series = panel.series["AAA"]
        usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
        expected = int(np.count_nonzero(usable & (series.label_end_dates <= cutoff)))
        assert expected > 0
        assert model._filters["AAA"]["close"].observations == expected

    def test_point_and_band_come_from_the_filter_projection(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        [forecast] = model.predict(_requests(panel, "AAA", [200]))
        # open/close only: finalize_forecast's bar repair can raise `high`
        # and lower `low`, but it never touches open or close, so these two
        # components carry the filter's raw numbers through unchanged.
        for component in ("open", "close"):
            mean, variance = model._filters["AAA"][component].project()
            half = Z90 * math.sqrt(variance)
            band = forecast.quantiles[component]
            assert forecast.point[component] == pytest.approx(mean, abs=1e-12)
            assert band["p50"] == pytest.approx(mean, abs=1e-12)
            assert band["p90"] - band["p50"] == pytest.approx(half, abs=1e-12)
            assert band["p50"] - band["p10"] == pytest.approx(half, abs=1e-12)

    def test_emitted_forecasts_are_ordered_and_geometrically_possible(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        for forecast in model.predict(_requests(panel, "AAA", [150, 175, 200])):
            assert forecast.point["high"] >= max(forecast.point["open"], forecast.point["close"])
            assert forecast.point["low"] <= min(forecast.point["open"], forecast.point["close"])
            for component in COMPONENTS:
                band = forecast.quantiles[component]
                assert band["p10"] <= band["p50"] <= band["p90"]
            for key in ("p10", "p50", "p90"):
                level = {component: forecast.quantiles[component][key] for component in COMPONENTS}
                assert level["high"] >= max(level["open"], level["close"])
                assert level["low"] <= min(level["open"], level["close"])

    def test_repairs_are_counted_never_silent(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        model.predict(_requests(panel, "AAA", [150, 175, 200]))
        assert set(model.last_repairs.as_dict()) == {"quantile_crossings", "bar_repairs"}

    def test_unseen_ticker_uses_the_pooled_prior_without_creating_state(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        request = dataclasses.replace(_requests(panel, "AAA", [200])[0], ticker="NEWCO")
        [forecast] = model.predict([request])
        # predict() is read-only: a ticker the fit never saw must not gain a
        # filter just by being forecast.
        assert "NEWCO" not in model._filters
        for component in COMPONENTS:
            band = forecast.quantiles[component]
            assert math.isfinite(forecast.point[component])
            assert band["p90"] > band["p10"]

    def test_predict_is_pure(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        requests = _requests(panel, "AAA", [180, 200])
        first = model.predict(requests)
        second = model.predict(requests)
        # Exact equality, not approx: predict() reads state and computes; if
        # calling it advances the filter, these diverge.
        assert [f.point for f in first] == [f.point for f in second]
        assert [f.quantiles for f in first] == [f.quantiles for f in second]

    def test_update_advances_exactly_one_filter_step_per_revealed_day(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        series = panel.series["AAA"]
        row = _trainable_rows(series)[-5]
        before = copy.deepcopy(model._filters["AAA"]["close"])
        revealed = model_interface.make_revealed(series, row, WINDOW_LEN)
        stats = model.update([revealed], step_index=1)
        assert stats["absorbed"] == 1
        before.absorb(revealed.actual["close"])
        assert model._filters["AAA"]["close"].observations == before.observations
        assert model._filters["AAA"]["close"].level == pytest.approx(before.level, abs=1e-15)

    def test_update_creates_state_for_a_ticker_the_fit_never_saw(self, panel):
        # A late IPO joins mid-replay: its first revealed day must open a
        # filter seeded from the pooled prior rather than being dropped.
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        series = panel.series["AAA"]
        row = _trainable_rows(series)[-5]
        revealed = dataclasses.replace(model_interface.make_revealed(series, row, WINDOW_LEN), ticker="NEWCO")
        model.update([revealed], step_index=1)
        assert model._filters["NEWCO"]["close"].observations == 1

    def test_refit_replaces_state_instead_of_absorbing_every_day_twice(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        first = {component: model._filters["AAA"][component].observations for component in COMPONENTS}
        levels = {component: model._filters["AAA"][component].level for component in COMPONENTS}
        model.fit(panel, seed=0)  # the recovery-retrain path calls fit() again
        for component in COMPONENTS:
            assert model._filters["AAA"][component].observations == first[component]
            assert model._filters["AAA"][component].level == pytest.approx(levels[component], abs=1e-15)

    def test_reconstruction_diagnostic_is_none(self, panel):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        assert model.reconstruction_diagnostic(_requests(panel, "AAA", [200])) is None


class TestCheckpointing:
    def test_save_load_round_trip_reproduces_predictions_exactly(self, panel, tmp_path):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        requests = _requests(panel, "AAA", [180, 200])
        before = model.predict(requests)
        model.save(tmp_path / "kalman")
        restored = kalman.KalmanForecaster.load(tmp_path / "kalman")
        after = restored.predict(requests)
        # Exact, not approx: the state is float64 on both sides of the disk
        # boundary or this fails, which is the point -- a float32 .npz would
        # silently change every forecast after a resume.
        assert [f.point for f in before] == [f.point for f in after]
        assert [f.quantiles for f in before] == [f.quantiles for f in after]

    def test_round_trip_preserves_the_trend_variants_slope_state(self, panel, tmp_path):
        model = kalman.KalmanForecaster(trend=True)
        model.fit(panel, seed=0)
        model.save(tmp_path / "kalman_trend")
        restored = kalman.KalmanForecaster.load(tmp_path / "kalman_trend")
        assert restored.trend is True
        for component in COMPONENTS:
            original = model._filters["AAA"][component]
            reloaded = restored._filters["AAA"][component]
            assert reloaded.slope == original.slope
            assert reloaded.observations == original.observations
            np.testing.assert_array_equal(reloaded.covariance, original.covariance)

    def test_artifact_is_json_plus_npz_and_nothing_executable(self, panel, tmp_path):
        model = kalman.KalmanForecaster()
        model.fit(panel, seed=0)
        directory = tmp_path / "kalman"
        model.save(directory)
        written = sorted(path.name for path in directory.iterdir())
        assert written == ["metadata.json", "weights.npz"]
        metadata = json.loads((directory / "metadata.json").read_bytes())
        assert metadata["name"] == "kalman"
        # load_arrays enforces allow_pickle=False; assert the archive really
        # opens under it rather than trusting the helper.
        with np.load(directory / "weights.npz", allow_pickle=False) as archive:
            assert archive.files


class TestNoLookahead:
    def test_forecasts_at_or_before_a_cutoff_ignore_later_reveals(self, panel):
        series = panel.series["AAA"]
        rows = _trainable_rows(series)[-80:]
        cutoff = 40

        def replay(override: float | None) -> list:
            model = kalman.KalmanForecaster()
            model.fit(panel, seed=0)
            emitted = []
            for position, row in enumerate(rows):
                [forecast] = model.predict(_requests(panel, "AAA", [row]))
                emitted.append(forecast)
                revealed = model_interface.make_revealed(series, row, WINDOW_LEN)
                if override is not None and position > cutoff:
                    revealed = dataclasses.replace(
                        revealed, actual={component: override for component in COMPONENTS}
                    )
                model.update([revealed], step_index=position)
            return emitted

        base = replay(None)
        perturbed = replay(0.5)
        # Forecast at position cutoff+1 is emitted BEFORE that position's
        # (perturbed) reveal is absorbed, so it is still clean.
        for position in range(cutoff + 2):
            assert base[position].point == perturbed[position].point
            assert base[position].quantiles == perturbed[position].quantiles
        # ... and the very next one must differ, or this test proves nothing.
        assert base[cutoff + 2].point != perturbed[cutoff + 2].point


class TestProtocolConformance:
    def test_exposes_the_forecaster_surface_the_harness_requires(self):
        model = kalman.KalmanForecaster()
        assert model.name == "kalman"
        for method in ("fit", "predict", "update", "reconstruction_diagnostic", "save", "load"):
            assert callable(getattr(model, method))
