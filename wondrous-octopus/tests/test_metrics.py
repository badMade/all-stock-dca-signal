"""Tests for metrics.py: scoring payloads, aggregation, regimes, rendering."""

from __future__ import annotations

import math

import pytest

import metrics
from data import COMPONENTS


def _score_record(
    *,
    model="persistence",
    ticker="AAA",
    origin_date="2021-06-01",
    target_date="2021-06-02",
    point=None,
    actual=None,
    spread=0.02,
):
    point = point if point is not None else {c: 0.0 for c in COMPONENTS}
    actual = actual if actual is not None else {"open": 0.001, "high": 0.01, "low": -0.01, "close": 0.002}
    quantiles = {c: {"p10": point[c] - spread, "p50": point[c], "p90": point[c] + spread} for c in COMPONENTS}
    score = metrics.score_forecast(
        point=point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
    )
    return {
        "model": model,
        "ticker": ticker,
        "origin_date": origin_date,
        "target_date": target_date,
        "score": score,
    }


class TestScoreForecast:
    def test_hand_checked_errors(self):
        record = _score_record()
        score = record["score"]
        assert score["abs_err"]["close"] == pytest.approx(0.002)
        assert score["sq_err"]["close"] == pytest.approx(4e-6)
        # Price error: |e^0 - e^0.002| * 100
        assert score["price_abs_err"]["close"] == pytest.approx(abs(1 - math.exp(0.002)) * 100.0)
        assert score["in_band"]["close"] is True
        assert score["band_width"]["close"] == pytest.approx(0.04)
        # Point close 0.0 sits inside [low=-0.01, high=0.01].
        assert score["close_in_range"] is True

    def test_pinball_hand_check(self):
        # actual above p90: pinball_p90 = 0.9 * (actual - pred)
        record = _score_record(actual={"open": 0.05, "high": 0.06, "low": 0.04, "close": 0.05})
        score = record["score"]
        assert score["pinball"]["close"]["p90"] == pytest.approx(0.9 * (0.05 - 0.02))
        assert score["in_band"]["close"] is False
        assert score["close_in_range"] is False  # 0.0 below the realized low of 0.04

    def test_score_forecast_survives_non_finite_or_extreme_predictions(self):
        # An unbounded model head could in principle emit an extreme
        # log-return (math.exp() raises OverflowError past roughly +/-709,
        # even though math.exp(inf) itself just returns inf) or a NaN one;
        # either used to corrupt or crash score_forecast() right inside the
        # harness's scoring path -- and since the offending model's
        # checkpointed state is deterministic, the same crash would recur on
        # every resume, permanently stalling the run (Codex review
        # regression: "Make score conversion safe for unbounded model
        # outputs").
        actual = {"open": 0.001, "high": 0.01, "low": -0.01, "close": 0.002}
        quantiles = {c: {"p10": -0.02, "p50": 0.0, "p90": 0.02} for c in COMPONENTS}
        extreme = dict({c: 0.0 for c in COMPONENTS}, close=1000.0)
        score = metrics.score_forecast(
            point=extreme, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
        )
        assert math.isfinite(score["price_abs_err"]["close"])

        nan_point = dict({c: 0.0 for c in COMPONENTS}, close=float("nan"))
        score = metrics.score_forecast(
            point=nan_point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
        )
        assert math.isfinite(score["price_abs_err"]["close"])

    def test_score_forecast_sanitizes_every_field_not_just_price_abs_err(self):
        # A first pass guarded only the exponentiated price_abs_err
        # conversion; abs_err/sq_err (from the point) and pinball/in_band
        # (from the quantile bands) still used the raw, unbounded value,
        # so a single non-finite/extreme model output still poisoned
        # them -- and since MetricsAccumulator sums abs_err/sq_err/
        # pred_close into running per-bucket totals, one NaN addend
        # poisons that bucket's aggregate forever (Codex review
        # regression: "Reject non-finite forecasts before scoring").
        actual = {"open": 0.001, "high": 0.01, "low": -0.01, "close": 0.002}
        extreme_point = dict({c: 0.0 for c in COMPONENTS}, close=1000.0)
        nan_quantiles = {
            c: {"p10": float("nan"), "p50": 0.0, "p90": 5000.0} for c in COMPONENTS
        }
        score = metrics.score_forecast(
            point=extreme_point, quantiles=nan_quantiles, quantiles_raw=nan_quantiles,
            actual=actual, ref_close=100.0,
        )
        assert math.isfinite(score["abs_err"]["close"])
        assert math.isfinite(score["sq_err"]["close"])
        assert math.isfinite(score["pred_close"])
        for component in COMPONENTS:
            for key in ("p10", "p50", "p90"):
                assert math.isfinite(score["pinball"][component][key])
        assert isinstance(score["close_in_range"], bool)
        assert isinstance(score["in_band"]["close"], bool)

    def test_a_nan_point_forecast_scores_as_a_miss_not_an_accurate_flat_prediction(self):
        # NaN used to sanitize to 0.0, so a model that emitted NaN could
        # score as an ACCURATE forecast (tiny abs_err) whenever the
        # realized move happened to be small, and even earn in-band
        # credit -- hiding a numerically broken model behind plausible-
        # looking accuracy stats (Codex review: "Reject NaN forecasts
        # instead of scoring them as zero"). A NaN point must register as
        # a large error instead.
        actual = {"open": 0.001, "high": 0.01, "low": -0.01, "close": 0.002}  # a small, ordinary realized move
        nan_point = dict({c: 0.0 for c in COMPONENTS}, close=float("nan"))
        quantiles = {c: {"p10": -0.02, "p50": 0.0, "p90": 0.02} for c in COMPONENTS}
        score = metrics.score_forecast(
            point=nan_point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
        )
        assert math.isfinite(score["abs_err"]["close"])
        assert score["abs_err"]["close"] > 1.0  # nowhere near the tiny realized move -- a clear miss

    def test_a_nan_quantile_band_scores_as_out_of_band_not_in_band(self):
        # Same failure mode for the bands: NaN quantiles sanitizing to 0.0
        # could make a degenerate [0, 0] band look "in range" whenever the
        # realized move landed exactly at zero, and produced a misleadingly
        # small pinball loss for nearby realized values.
        actual = {"open": 0.001, "high": 0.01, "low": -0.01, "close": 0.002}
        point = {c: 0.0 for c in COMPONENTS}
        nan_quantiles = {c: {"p10": float("nan"), "p50": 0.0, "p90": float("nan")} for c in COMPONENTS}
        score = metrics.score_forecast(
            point=point, quantiles=nan_quantiles, quantiles_raw=nan_quantiles, actual=actual, ref_close=100.0
        )
        assert score["in_band"]["close"] is False
        assert math.isfinite(score["pinball"]["close"]["p90"])
        assert score["pinball"]["close"]["p90"] > 1.0  # a large, visible loss -- not a plausible small one

    def test_raw_and_calibrated_bands_scored_separately(self):
        point = {c: 0.0 for c in COMPONENTS}
        wide = {c: {"p10": -0.1, "p50": 0.0, "p90": 0.1} for c in COMPONENTS}
        narrow = {c: {"p10": -0.001, "p50": 0.0, "p90": 0.001} for c in COMPONENTS}
        actual = {"open": 0.05, "high": 0.06, "low": 0.04, "close": 0.05}
        score = metrics.score_forecast(
            point=point, quantiles=wide, quantiles_raw=narrow, actual=actual, ref_close=100.0
        )
        assert score["in_band"]["close"] is True
        assert score["in_band_raw"]["close"] is False


class TestRegimes:
    def test_bucketing(self):
        assert metrics.bucket_regime("2000-06-01") == "dotcom_bust"
        assert metrics.bucket_regime("2008-10-15") == "gfc"
        assert metrics.bucket_regime("2020-03-20") == "covid"
        assert metrics.bucket_regime("2022-06-01") == "rate_shock_2022"
        assert metrics.bucket_regime("2015-01-05") == "other"


class TestBuildReport:
    def test_aggregates_and_skill(self):
        records = {
            "persistence": lambda: [
                _score_record(model="persistence", point={c: 0.02 for c in COMPONENTS}),
                _score_record(model="persistence", origin_date="2021-06-02", target_date="2021-06-03"),
            ],
            "gbm_quantile": lambda: [
                _score_record(model="gbm_quantile", point={c: 0.002 for c in COMPONENTS}),
                _score_record(model="gbm_quantile", origin_date="2021-06-02", target_date="2021-06-03"),
            ],
        }
        report = metrics.build_report(records, universe={"n_tickers": 1, "description": "test"})
        block = report["by_regime"]["all"]
        assert block["persistence"]["close"]["n"] == 2
        # GBM's close MAE is lower than persistence's, so skill is positive.
        assert report["skill_vs_persistence"]["gbm_quantile"]["close"] > 0
        assert report["n_scored"] == {"persistence": 2, "gbm_quantile": 2}

    def test_intersection_restricts_to_shared_keys(self):
        records = {
            "persistence": lambda: [
                _score_record(model="persistence"),
                _score_record(model="persistence", ticker="ONLYP"),
            ],
            "gbm_quantile": lambda: [_score_record(model="gbm_quantile")],
        }
        report = metrics.build_report(records)
        assert report["intersection_applied"] is True
        assert report["n_scored"] == {"persistence": 1, "gbm_quantile": 1}

    def test_intersection_is_correct_across_many_tickers_and_dates(self):
        # Regression test for the compact integer key encoding (ticker_id *
        # DATE_ID_SPACE + date_id) that replaced a set of (ticker,
        # origin_date) string tuples (Codex review: "Avoid materializing
        # every scored key for report intersection"). Enough distinct
        # tickers x dates to catch a wrong space constant either falsely
        # matching unrelated pairs or falsely excluding genuinely shared
        # ones.
        tickers = ["AAA", "BBB", "CCC", "DDD"]
        dates = [("2021-06-01", "2021-06-02"), ("2021-06-02", "2021-06-03"), ("2021-06-03", "2021-06-04")]
        shared = [
            _score_record(ticker=ticker, origin_date=origin, target_date=target)
            for ticker in tickers
            for origin, target in dates
        ]
        only_in_a = _score_record(ticker="ONLY_A", origin_date="2021-06-01", target_date="2021-06-02")
        only_in_b = _score_record(ticker="AAA", origin_date="2021-07-01", target_date="2021-07-02")
        records = {
            "model_a": lambda: [dict(r, model="model_a") for r in shared] + [dict(only_in_a, model="model_a")],
            "model_b": lambda: [dict(r, model="model_b") for r in shared] + [dict(only_in_b, model="model_b")],
            "model_c": lambda: [dict(r, model="model_c") for r in shared],
        }
        report = metrics.build_report(records)
        assert report["intersection_applied"] is True
        assert report["n_scored"] == {"model_a": len(shared), "model_b": len(shared), "model_c": len(shared)}

    def test_intersection_handles_duplicate_keys_within_one_models_stream(self):
        # Regression test for the sorted-numpy-array intersection that
        # replaced a Python set of boxed ints (Codex review: "Stream
        # report intersections without Python sets"). np.unique() must
        # dedupe a model's own key stream exactly like the set
        # comprehension it replaced -- a repeated (ticker, origin_date)
        # pair within one model's stream must not break the sorted-array
        # invariant np.intersect1d(..., assume_unique=True) relies on.
        records = {
            "persistence": lambda: [
                _score_record(model="persistence"),
                _score_record(model="persistence"),  # duplicate key
            ],
            "gbm_quantile": lambda: [_score_record(model="gbm_quantile")],
        }
        report = metrics.build_report(records)
        assert report["intersection_applied"] is True
        assert report["n_scored"] == {"persistence": 2, "gbm_quantile": 1}

    def test_market_fraction_detects_shared_errors(self):
        # Two tickers, same-day errors identical => all error variance is
        # market-wide; the fraction should be ~1.
        shared = []
        for day, err in (("2021-06-02", 0.03), ("2021-06-03", -0.02), ("2021-06-04", 0.01)):
            for ticker in ("AAA", "BBB"):
                shared.append(
                    _score_record(
                        model="persistence",
                        ticker=ticker,
                        origin_date=f"o{day}",
                        target_date=day,
                        point={c: err for c in COMPONENTS},
                        actual={"open": 0.0, "high": 0.01, "low": -0.01, "close": 0.0},
                    )
                )
        report = metrics.build_report({"persistence": lambda: shared})
        fraction = report["market"]["persistence"]["error_variance_market_fraction"]
        assert fraction == pytest.approx(1.0, abs=1e-9)

    def test_market_fraction_is_bounded_with_uneven_day_counts(self):
        # One sparse high-error day plus one dense low-error day: an
        # unweighted day-mean variance would exceed the per-forecast total;
        # the weighted decomposition stays a true fraction (review regression).
        records = []
        records.append(
            _score_record(
                model="persistence", ticker="AAA", origin_date="o1", target_date="2021-06-02",
                point={c: 0.5 for c in COMPONENTS},
                actual={"open": 0.0, "high": 0.01, "low": -0.01, "close": 0.0},
            )
        )
        for ticker in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            records.append(
                _score_record(
                    model="persistence", ticker=ticker, origin_date="o2", target_date="2021-06-03",
                    point={c: 0.001 for c in COMPONENTS},
                    actual={"open": 0.0, "high": 0.01, "low": -0.01, "close": 0.0},
                )
            )
        report = metrics.build_report({"persistence": lambda: records})
        fraction = report["market"]["persistence"]["error_variance_market_fraction"]
        assert 0.0 <= fraction <= 1.0

    def test_coverage_tracks_nominal_on_gaussian_synthetic(self):
        # Gaussian residuals against bands set at the true 10th/90th
        # percentiles must cover ~80% -- the calibration sanity anchor.
        import numpy as np

        rng = np.random.default_rng(0)
        sigma = 0.02
        z90 = 1.2815515655446004  # Phi^-1(0.9)
        records = []
        for index, noise in enumerate(rng.normal(0.0, sigma, size=2000)):
            actual_value = float(noise)
            actual = {
                "open": actual_value, "high": actual_value + 0.01, "low": actual_value - 0.01, "close": actual_value
            }
            records.append(
                _score_record(
                    model="persistence",
                    origin_date=f"2021-{index}",
                    target_date="2021-06-02",
                    point={c: 0.0 for c in COMPONENTS},
                    actual=actual,
                    spread=z90 * sigma,
                )
            )
        report = metrics.build_report({"persistence": lambda: records})
        coverage = report["by_regime"]["all"]["persistence"]["close"]["coverage"]
        assert coverage == pytest.approx(0.8, abs=0.03)


def _diagnostic_record(origin_date, abs_err_close, *, mode="time"):
    return {"mode": mode, "origin_date": origin_date, "abs_err": {"close": abs_err_close}}


class TestDiagnosticHealth:
    def test_insufficient_data_before_two_full_windows(self):
        # With fewer than 2*window_days dates, the first/last comparison
        # windows share dates (or are identical), trivially reporting OK
        # regardless of actual degradation -- must withhold a verdict
        # instead (Codex review regression: "Withhold encoder health until
        # windows are independent").
        records = [_diagnostic_record(f"2021-01-{day:02d}", 0.01) for day in range(1, 15)]
        verdict = metrics.diagnostic_health(records, window_days=10)
        assert verdict["status"] == "INSUFFICIENT_DATA"
        assert verdict["modes"] == {}

    def test_degradation_detected_once_windows_are_independent(self):
        records = [_diagnostic_record(f"2021-01-{day:02d}", 0.01) for day in range(1, 11)]
        records += [_diagnostic_record(f"2021-02-{day:02d}", 0.05) for day in range(1, 11)]
        verdict = metrics.diagnostic_health(records, window_days=10, degrade_factor=2.0)
        assert verdict["status"] == "DEGRADED"
        assert verdict["modes"]["time"]["degraded"] is True

    def test_stable_reconstruction_is_ok_with_independent_windows(self):
        records = [_diagnostic_record(f"2021-01-{day:02d}", 0.01) for day in range(1, 11)]
        records += [_diagnostic_record(f"2021-02-{day:02d}", 0.011) for day in range(1, 11)]
        verdict = metrics.diagnostic_health(records, window_days=10, degrade_factor=2.0)
        assert verdict["status"] == "OK"
        assert verdict["modes"]["time"]["degraded"] is False

    def test_averages_multiple_records_sharing_the_same_date(self):
        # A real replay ledgers one diagnostic record per SAMPLED TICKER
        # per date (diagnostic_sample can be hundreds), not one per date --
        # the per-date mean must reflect all of them, not just whichever
        # arrived (Codex review regression risk: "Bound encoder-health
        # aggregation to the needed windows" changed the accumulator from
        # a per-date list to a running sum/count; a mean is still a mean).
        records = []
        for day in range(1, 11):
            records.append(_diagnostic_record(f"2021-01-{day:02d}", 0.0))
            records.append(_diagnostic_record(f"2021-01-{day:02d}", 0.02))  # mean per date: 0.01
        for day in range(1, 11):
            records.append(_diagnostic_record(f"2021-02-{day:02d}", 0.03))
            records.append(_diagnostic_record(f"2021-02-{day:02d}", 0.03))  # mean per date: 0.03
        verdict = metrics.diagnostic_health(records, window_days=10, degrade_factor=2.0)
        assert verdict["modes"]["time"]["baseline_mae_close"] == pytest.approx(0.01)
        assert verdict["modes"]["time"]["current_mae_close"] == pytest.approx(0.03)
        assert verdict["modes"]["time"]["degraded"] is True

    def test_retrained_after_prevents_false_degraded_against_a_stale_baseline(self):
        # A recovery retrain's freshly-refit encoder settles at a
        # different-but-stable error level than the ORIGINAL run's
        # pre-recovery baseline. Comparing forever against that stale,
        # unrelated baseline would keep reporting DEGRADED (or hide a real
        # regression relative to the encoder's own post-recovery starting
        # point) instead of judging the recovered encoder on its own terms
        # (Codex review regression: "Reset encoder-health baseline after
        # recovery retraining").
        old_good_baseline = [_diagnostic_record(f"2021-01-{day:02d}", 0.01) for day in range(1, 11)]
        post_recovery_baseline = [_diagnostic_record(f"2021-03-{day:02d}", 0.03) for day in range(1, 11)]
        post_recovery_current = [_diagnostic_record(f"2021-04-{day:02d}", 0.031) for day in range(1, 11)]
        records = old_good_baseline + post_recovery_baseline + post_recovery_current

        unbounded = metrics.diagnostic_health(records, window_days=10, degrade_factor=2.0)
        assert unbounded["status"] == "DEGRADED"  # compares Jan's 0.01 against April's 0.031: falsely degraded

        bounded = metrics.diagnostic_health(
            records, window_days=10, degrade_factor=2.0, retrained_after="2021-02-01"
        )
        assert bounded["status"] == "OK"
        assert bounded["modes"]["time"]["baseline_mae_close"] == pytest.approx(0.03)

    def test_latest_retrain_date_returns_the_max_origin_date(self):
        records = [{"origin_date": "2021-01-05"}, {"origin_date": "2021-03-10"}, {"origin_date": "2021-02-01"}]
        assert metrics.latest_retrain_date(records) == "2021-03-10"

    def test_latest_retrain_date_is_none_when_empty(self):
        assert metrics.latest_retrain_date([]) is None


def test_render_markdown_contains_key_sections():
    records = {
        "persistence": lambda: [_score_record(model="persistence")],
        "gbm_quantile": lambda: [_score_record(model="gbm_quantile", point={c: 0.001 for c in COMPONENTS})],
    }
    report = metrics.build_report(records, universe={"n_tickers": 1, "description": "test archive"})
    markdown = metrics.render_markdown(report)
    assert "# Wondrous Octopus" in markdown
    assert "## Regime: all" in markdown
    assert "Skill vs persistence" in markdown
    assert "The market as one entity" in markdown
    assert "test archive" in markdown
