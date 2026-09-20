"""Tests for model_interface.py: repairs, calibration, artifact helpers."""

from __future__ import annotations

import math
from typing import Any

import features as _features
import model_interface
import numpy as np
import pytest
from data import COMPONENTS

features: Any = _features


def _flat_quantiles(value: float, spread: float = 0.01) -> dict[str, dict[str, float]]:
    return {
        component: {"p10": value - spread, "p50": value, "p90": value + spread} for component in COMPONENTS
    }


def _finalize(point, quantiles, repairs=None):
    repairs = repairs if repairs is not None else model_interface.RepairCounts()
    forecast = model_interface.finalize_forecast(
        ticker="AAA",
        origin_date="2021-06-01",
        target_date="2021-06-02",
        point=point,
        quantiles=quantiles,
        ref_close=100.0,
        adj_ratio=1.0,
        repairs=repairs,
    )
    return forecast, repairs


class TestFinalizeForecast:
    def test_crossed_quantiles_are_sorted_and_counted(self):
        quantiles = _flat_quantiles(0.0)
        quantiles["close"] = {"p10": 0.02, "p50": 0.0, "p90": -0.02}  # fully inverted
        point = {c: 0.0 for c in COMPONENTS}
        forecast, repairs = _finalize(point, quantiles)
        band = forecast.quantiles["close"]
        assert band["p10"] <= band["p50"] <= band["p90"]
        assert repairs.quantile_crossings == 1

    def test_impossible_point_bar_is_repaired_and_counted(self):
        point = {"open": 0.01, "high": -0.05, "low": 0.03, "close": 0.0}
        forecast, repairs = _finalize(point, _flat_quantiles(0.0))
        assert forecast.point["high"] >= max(forecast.point["open"], forecast.point["close"])
        assert forecast.point["low"] <= min(forecast.point["open"], forecast.point["close"])
        assert repairs.bar_repairs >= 1

    def test_consistent_forecast_passes_untouched(self):
        point = {"open": 0.001, "high": 0.02, "low": -0.02, "close": 0.005}
        quantiles = {
            component: {"p10": point[component] - 0.01, "p50": point[component], "p90": point[component] + 0.01}
            for component in COMPONENTS
        }
        # Make each quantile LEVEL a valid bar too.
        for key in model_interface.QUANTILE_KEYS:
            quantiles["high"][key] += 0.05
            quantiles["low"][key] -= 0.05
        forecast, repairs = _finalize(point, quantiles)
        assert repairs.quantile_crossings == 0
        assert repairs.bar_repairs == 0
        assert forecast.point == point


def test_repair_band_geometry_fixes_calibration_breakage():
    # Independent per-component scaling can invert quantiles / break the bar.
    bands = {
        "open": {"p10": 0.02, "p50": 0.0, "p90": -0.02},  # inverted
        "high": {"p10": -0.5, "p50": -0.5, "p90": -0.5},  # far below open/close
        "low": {"p10": 0.5, "p50": 0.5, "p90": 0.5},  # far above open/close
        "close": {"p10": -0.01, "p50": 0.0, "p90": 0.01},
    }
    repaired = model_interface.repair_band_geometry(bands)
    for component in COMPONENTS:
        band = repaired[component]
        assert band["p10"] <= band["p50"] <= band["p90"]
    for key in model_interface.QUANTILE_KEYS:
        level = {component: repaired[component][key] for component in COMPONENTS}
        assert level["high"] >= max(level["open"], level["close"])
        assert level["low"] <= min(level["open"], level["close"])


class TestConformalCalibrator:
    def test_sustained_misses_widen_the_band(self):
        calibrator = model_interface.ConformalCalibrator()
        forecast, _ = _finalize({c: 0.0 for c in COMPONENTS}, _flat_quantiles(0.0, spread=0.001))
        before = calibrator.scales["close"]
        for _ in range(50):
            calibrated = calibrator.calibrate(forecast)
            calibrator.observe(calibrated, {c: 0.5 for c in COMPONENTS})  # always far outside
        assert calibrator.scales["close"] > before

    def test_sustained_cover_narrows_the_band(self):
        calibrator = model_interface.ConformalCalibrator()
        forecast, _ = _finalize({c: 0.0 for c in COMPONENTS}, _flat_quantiles(0.0, spread=0.05))
        for _ in range(50):
            calibrated = calibrator.calibrate(forecast)
            calibrator.observe(calibrated, {c: 0.0 for c in COMPONENTS})  # always inside
        assert calibrator.scales["close"] < 1.0

    def test_scales_are_clipped(self):
        calibrator = model_interface.ConformalCalibrator(lr=1.0)
        forecast, _ = _finalize({c: 0.0 for c in COMPONENTS}, _flat_quantiles(0.0, spread=0.001))
        for _ in range(200):
            calibrated = calibrator.calibrate(forecast)
            calibrator.observe(calibrated, {c: 5.0 for c in COMPONENTS})
        assert calibrator.scales["close"] == calibrator.max_scale

    def test_state_roundtrip(self):
        calibrator = model_interface.ConformalCalibrator(min_scale=0.5, max_scale=2.5)
        calibrator.scales["close"] = 1.7
        restored = model_interface.ConformalCalibrator.from_state(calibrator.to_state())
        assert restored.scales == calibrator.scales
        assert restored.target == calibrator.target
        # Non-default clip bounds survive resume (Copilot review finding).
        assert restored.min_scale == 0.5
        assert restored.max_scale == 2.5

    def test_state_from_pre_bounds_checkpoint_uses_defaults(self):
        legacy = {"target": 0.8, "lr": 0.02, "scales": {c: 1.0 for c in COMPONENTS}}
        restored = model_interface.ConformalCalibrator.from_state(legacy)
        assert restored.min_scale == 0.25
        assert restored.max_scale == 4.0

    def test_observe_batch_applies_one_aggregate_nudge_not_one_per_entry(self):
        # observe() is documented as one nudge per REVEALED DAY; calling it
        # once per ticker on a full-universe day (thousands of tickers)
        # would collapse the scale to its clip bound almost immediately and
        # make the final value sensitive to ticker ordering (Codex review
        # regression: "Aggregate calibration updates once per market day").
        # observe_batch() must apply ONE update from the aggregate miss
        # rate across the whole batch, not one per observation.
        forecast, _ = _finalize({c: 0.0 for c in COMPONENTS}, _flat_quantiles(0.0, spread=0.001))
        calibrated = model_interface.ConformalCalibrator().calibrate(forecast)
        # 6000 "tickers" all covered (miss=0) maturing on the same simulated day.
        observations = [(calibrated, {c: 0.0 for c in COMPONENTS}) for _ in range(6000)]

        looped = model_interface.ConformalCalibrator()
        for calibrated_obs, actual_obs in observations:
            looped.observe(calibrated_obs, actual_obs)

        batched = model_interface.ConformalCalibrator()
        batched.observe_batch(observations, target_date="2021-06-01")

        # The old per-observation loop collapses straight to the floor;
        # one aggregate update moves by a single day's worth of drift.
        assert looped.scales["close"] == looped.min_scale
        assert batched.scales["close"] > batched.min_scale
        expected = math.exp(0.02 * (0.0 - 0.2))  # miss_rate=0 (all covered), lr=0.02, target=0.8
        assert batched.scales["close"] == pytest.approx(expected, rel=1e-9)

    def test_observe_batch_matches_a_single_observe_call_for_one_entry(self):
        forecast, _ = _finalize({c: 0.0 for c in COMPONENTS}, _flat_quantiles(0.0, spread=0.001))
        calibrated = model_interface.ConformalCalibrator().calibrate(forecast)
        actual = {c: 0.5 for c in COMPONENTS}
        looped_once = model_interface.ConformalCalibrator()
        looped_once.observe(calibrated, actual)
        batched_once = model_interface.ConformalCalibrator()
        batched_once.observe_batch([(calibrated, actual)], target_date="2021-06-01")
        assert looped_once.scales == batched_once.scales

    def test_observe_batch_empty_is_a_noop(self):
        calibrator = model_interface.ConformalCalibrator()
        before = dict(calibrator.scales)
        calibrator.observe_batch([], target_date="2021-06-01")
        assert calibrator.scales == before

    def test_observe_batch_skips_a_second_call_for_an_already_calibrated_target_date(self):
        # A lagging ticker's forecast can mature independently (via
        # rebind_unresolved()) after the REST of the market's forecasts
        # for that SAME target date already went through the normal
        # per-day aggregate batch -- re-nudging with only that smaller
        # late cohort would double-weight the day and make future bands
        # depend on archive-arrival order rather than one aggregate
        # update per market day (Codex review: "Avoid recalibrating an
        # already-processed target day").
        calibrator = model_interface.ConformalCalibrator()
        forecast, _ = _finalize({c: 0.0 for c in COMPONENTS}, _flat_quantiles(0.0, spread=0.001))
        calibrated = calibrator.calibrate(forecast)
        missed = {c: 0.5 for c in COMPONENTS}  # far outside the band -- a miss
        first_batch = [(calibrated, missed) for _ in range(10)]
        calibrator.observe_batch(first_batch, target_date="2021-06-02")
        after_first = dict(calibrator.scales)
        assert after_first["close"] != 1.0  # sanity: the first batch actually moved the scale

        # A late-arriving second batch for the SAME target date must not
        # apply another nudge.
        late_batch = [(calibrated, missed)]
        calibrator.observe_batch(late_batch, target_date="2021-06-02")
        assert calibrator.scales == after_first

        # An earlier target date (an out-of-order late arrival) is also refused.
        calibrator.observe_batch(late_batch, target_date="2021-06-01")
        assert calibrator.scales == after_first

        # A genuinely LATER target date still applies normally.
        calibrator.observe_batch(late_batch, target_date="2021-06-03")
        assert calibrator.scales != after_first

    def test_state_roundtrip_carries_the_calibration_watermark(self):
        calibrator = model_interface.ConformalCalibrator()
        calibrator.last_calibrated_target = "2021-06-02"
        restored = model_interface.ConformalCalibrator.from_state(calibrator.to_state())
        assert restored.last_calibrated_target == "2021-06-02"

    def test_state_from_pre_watermark_checkpoint_has_no_watermark(self):
        legacy = {"target": 0.8, "lr": 0.02, "scales": {c: 1.0 for c in COMPONENTS}}
        restored = model_interface.ConformalCalibrator.from_state(legacy)
        assert restored.last_calibrated_target is None


class TestArtifactHelpers:
    def test_array_roundtrip_without_pickle(self, tmp_path):
        arrays = {"weight": np.arange(6, dtype=np.float32).reshape(2, 3), "bias": np.zeros(3)}
        model_interface.save_arrays(tmp_path, arrays)
        loaded = model_interface.load_arrays(tmp_path)
        np.testing.assert_array_equal(loaded["weight"], arrays["weight"])
        np.testing.assert_array_equal(loaded["bias"], arrays["bias"])

    def test_object_arrays_are_refused(self, tmp_path):
        with pytest.raises(TypeError, match="object dtype"):
            model_interface.save_arrays(tmp_path, {"bad": np.array([{"a": 1}], dtype=object)})

    def test_metadata_roundtrip(self, tmp_path):
        metadata = {"name": "test", "hyper": {"alpha": 1.0}}
        model_interface.write_metadata(tmp_path, metadata)
        assert model_interface.read_metadata(tmp_path) == metadata


def test_last_real_row_respects_padding():
    window = np.arange(12, dtype=np.float32).reshape(4, 3)
    mask = np.array([False, True, True, False])
    request = model_interface.ForecastRequest(
        ticker="AAA",
        ticker_id=1,
        origin_date="2021-06-01",
        features_window=window,
        window_mask=mask,
        day_of_week=np.zeros(4, dtype=np.int8),
        cluster_id=np.zeros(4, dtype=np.int8),
        window_dates=np.full(4, np.datetime64("NaT"), dtype="datetime64[ns]"),
        ref_close=100.0,
        adj_ratio=1.0,
    )
    np.testing.assert_array_equal(model_interface.last_real_row(request), window[2])


def test_make_request_pads_with_nat_dates_using_the_series_own_unit(synthetic_history_dir):
    # A request near a series' start needs more history than exists yet
    # (common at the archive start / a ticker's warm-up against the
    # default window length); the padded front of window_dates must be
    # NaT constructed in the SAME unit as series.dates, not a generic-unit
    # NaT that numpy silently casts -- deprecated, and numpy states it
    # will become an error (Codex review regression: "Construct padded
    # NaT values with an explicit unit").
    panel = features.build_panel(synthetic_history_dir, min_history=60)
    series = panel.series["AAA"]
    row_index = 2
    window_len = 16
    request = model_interface.make_request(series, row_index, window_len)
    assert request.window_dates.dtype == series.dates.dtype
    real_count = row_index + 1
    pad = window_len - real_count
    assert np.isnat(request.window_dates[:pad]).all()
    assert not np.isnat(request.window_dates[pad:]).any()
    np.testing.assert_array_equal(request.window_dates[pad:], series.dates[: row_index + 1])


# ---------------------------------------------------------------------------
# cluster_id must travel with the window, not just sit on the panel.
#
# Storing the style-cluster label on TickerSeries proves nothing on its own:
# requests and revealed rows carry day_of_week today and nothing else
# categorical, so an implementation could compute clusters perfectly and
# still never show them to a model (Copilot review: "This verifies only that
# cluster_id is stored on TickerSeries, not that either model consumes it").
# These tests make the transport a contract.
# ---------------------------------------------------------------------------


def _cluster_panel(synthetic_history_dir):
    return features.build_panel(synthetic_history_dir, min_history=60, n_clusters=2, cluster_window=120)


def test_make_request_carries_the_cluster_window_alongside_day_of_week(synthetic_history_dir):
    panel = _cluster_panel(synthetic_history_dir)
    series = panel.series["AAA"]
    window_len = 16
    row_index = 200
    request = model_interface.make_request(series, row_index, window_len)
    assert request.cluster_id.shape == request.day_of_week.shape
    assert request.cluster_id.dtype == np.int8
    np.testing.assert_array_equal(
        request.cluster_id, series.cluster_id[row_index + 1 - window_len : row_index + 1]
    )


def test_make_revealed_carries_the_same_cluster_window(synthetic_history_dir):
    panel = _cluster_panel(synthetic_history_dir)
    series = panel.series["AAA"]
    window_len = 16
    row_index = 200
    revealed = model_interface.make_revealed(series, row_index, window_len)
    np.testing.assert_array_equal(
        revealed.cluster_id, series.cluster_id[row_index + 1 - window_len : row_index + 1]
    )


def test_cluster_window_is_front_padded_with_the_unassigned_label(synthetic_history_dir):
    # Same padding rule day_of_week already follows, and 0 is already the
    # "not assigned yet" label, so a padded row and a pre-refit row read
    # alike to the model rather than pointing at a real cluster.
    panel = _cluster_panel(synthetic_history_dir)
    series = panel.series["AAA"]
    row_index, window_len = 2, 16
    request = model_interface.make_request(series, row_index, window_len)
    pad = window_len - (row_index + 1)
    assert (request.cluster_id[:pad] == 0).all()
    np.testing.assert_array_equal(request.cluster_id[pad:], series.cluster_id[: row_index + 1])
