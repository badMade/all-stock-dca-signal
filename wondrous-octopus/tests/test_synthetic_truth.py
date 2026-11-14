"""Rock 4 of the band-calibration cycle: synthetic-truth recovery.

Written BEFORE the implementation (TDD-first; see
``claude/band-calibration/PLAN.md``). ``synthetic_truth`` does not exist yet.

Every other Rock measures the model. This one measures the measuring
instrument. It builds price archives whose answer is known in advance and
asks whether the unmodified harness and scorecard report that answer:

- a random walk, where no forecaster can have skill -- so any skill the
  harness reports is a leak or a scoring bug;
- a volatility-clustering process with a leverage effect, where the SIZE of
  tomorrow's move is predictable but its direction still is not;
- a planted drift (returns that follow yesterday's return with a known
  strength), where a competent forecaster MUST show skill -- so a harness
  that reports none has no power, and its "no skill" on real data would
  mean nothing.

The simulators are not claimed to resemble real markets. They are claimed to
have a known answer.

Skill here is measured against the no-change forecast (tomorrow's close
equals today's): ``1 - sum|predicted - actual| / sum|actual|`` in log-return
space. That needs no second model run, and it is exactly aligned by
construction.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import synthetic_truth as st
from data import discover_available_tickers


def _lag1(values: np.ndarray) -> float:
    return float(np.corrcoef(values[:-1], values[1:])[0, 1])


# --------------------------------------------------------------------------
# The simulators have the properties their names promise
# --------------------------------------------------------------------------


class TestSimulators:
    def test_random_walk_returns_do_not_predict_the_next(self):
        returns = st.simulate_returns("random_walk", n_rows=20_000, seed=1)
        assert abs(_lag1(returns)) < 0.03

    def test_planted_drift_has_the_stated_strength(self):
        returns = st.simulate_returns("planted_drift", n_rows=20_000, seed=1, strength=0.5)
        assert _lag1(returns) == pytest.approx(0.5, abs=0.03)

    def test_volatility_clusters_but_direction_does_not(self):
        returns = st.simulate_returns("garch_leverage", n_rows=20_000, seed=1)
        assert abs(_lag1(returns)) < 0.03
        assert _lag1(returns**2) > 0.1

    def test_leverage_a_fall_is_followed_by_a_larger_move_than_a_rise(self):
        returns = st.simulate_returns("garch_leverage", n_rows=20_000, seed=1)
        next_size = returns[1:] ** 2
        after_fall = next_size[returns[:-1] < 0].mean()
        after_rise = next_size[returns[:-1] > 0].mean()
        assert after_fall > 1.05 * after_rise

    def test_same_seed_same_series_and_different_seed_differs(self):
        first = st.simulate_returns("random_walk", n_rows=500, seed=7)
        again = st.simulate_returns("random_walk", n_rows=500, seed=7)
        other = st.simulate_returns("random_walk", n_rows=500, seed=8)
        assert np.array_equal(first, again)
        assert not np.array_equal(first, other)

    def test_daily_moves_are_of_a_plausible_size(self):
        # Not a realism claim -- a guard against a simulator whose scale
        # would trip the data layer's impossible-bar repair.
        for scenario in st.SCENARIOS:
            returns = st.simulate_returns(scenario, n_rows=5_000, seed=3)
            assert 0.005 < returns.std() < 0.05, scenario

    def test_unknown_scenario_is_rejected(self):
        with pytest.raises(ValueError):
            st.simulate_returns("efficient_market", n_rows=100, seed=0)


class TestKnownAnswers:
    def test_expected_hit_rate_of_a_planted_drift(self):
        # For jointly normal returns with lag-1 correlation rho, the sign of
        # yesterday's return matches today's with probability
        # 1/2 + asin(rho)/pi (Sheppard's formula).
        assert st.expected_hit_rate(0.0) == pytest.approx(0.5)
        assert st.expected_hit_rate(0.5) == pytest.approx(0.5 + math.asin(0.5) / math.pi)
        assert st.expected_hit_rate(0.5) == pytest.approx(2.0 / 3.0)

    def test_simulated_drift_matches_the_expected_hit_rate(self):
        returns = st.simulate_returns("planted_drift", n_rows=50_000, seed=2, strength=0.5)
        agree = np.mean(np.sign(returns[:-1]) == np.sign(returns[1:]))
        assert agree == pytest.approx(st.expected_hit_rate(0.5), abs=0.01)

    def test_scenarios_declare_what_an_honest_harness_must_report(self):
        assert st.SCENARIOS["random_walk"] == "no_skill"
        assert st.SCENARIOS["garch_leverage"] == "no_skill"
        assert st.SCENARIOS["planted_drift"] == "skill"


# --------------------------------------------------------------------------
# The archive is one the real data layer accepts
# --------------------------------------------------------------------------


class TestArchive:
    def test_frame_has_the_archive_layout_and_consistent_bars(self):
        returns = st.simulate_returns("random_walk", n_rows=300, seed=0)
        frame = st.price_frame(returns, seed=0)
        assert list(frame.columns) == [
            "Date",
            "Open",
            "High",
            "Low",
            "Close",
            "Adj Close",
            "Volume",
            "Dividends",
            "Stock Splits",
        ]
        assert (frame["High"] >= frame[["Open", "Close"]].max(axis=1)).all()
        assert (frame["Low"] <= frame[["Open", "Close"]].min(axis=1)).all()
        assert (frame["Close"] == frame["Adj Close"]).all()
        assert (frame["Stock Splits"] == 0).all() and (frame["Dividends"] == 0).all()

    def test_close_to_close_log_returns_are_exactly_the_simulated_ones(self):
        # The harness scores close log-returns. If the frame did not carry
        # the simulated returns through unchanged, the "known answer" would
        # no longer be the answer.
        returns = st.simulate_returns("planted_drift", n_rows=300, seed=0)
        frame = st.price_frame(returns, seed=0)
        observed = np.diff(np.log(frame["Close"].to_numpy()))
        assert observed == pytest.approx(returns[1:])

    def test_written_archive_is_discovered_by_the_real_data_layer(self, tmp_path):
        # Regression guard: the data layer accepts only letter-and-dot
        # symbols, and an archive it silently sees as empty proves nothing.
        tickers = st.write_archive(tmp_path, "random_walk", n_tickers=8, n_rows=120, seed=0)
        assert len(tickers) == 8
        assert discover_available_tickers(tmp_path) == sorted(tickers)

    def test_tickers_in_one_archive_are_different_series(self, tmp_path):
        import pandas as pd

        tickers = st.write_archive(tmp_path, "random_walk", n_tickers=2, n_rows=120, seed=0)
        first = pd.read_csv(tmp_path / f"{tickers[0]}.csv")["Close"].to_numpy()
        second = pd.read_csv(tmp_path / f"{tickers[1]}.csv")["Close"].to_numpy()
        assert not np.allclose(first, second)


# --------------------------------------------------------------------------
# Reading a verdict off score records
# --------------------------------------------------------------------------


def _records(predicted: np.ndarray, actual: np.ndarray, *, model="gbm_quantile", band=0.02):
    """Score-shaped records: ``predicted``/``actual`` are (n_dates, n_tickers) close log-returns."""
    first = np.datetime64("2019-01-01")
    records = []
    for day in range(predicted.shape[0]):
        date = str(first + np.timedelta64(day, "D"))
        for ticker in range(predicted.shape[1]):
            pred = float(predicted[day, ticker])
            real = float(actual[day, ticker])
            records.append(
                {
                    "model": model,
                    "ticker": f"SY{chr(65 + ticker)}",
                    "origin_date": date,
                    "target_date": date,
                    "score": {
                        "pred_close": pred,
                        "actual_close": real,
                        "in_band": {"close": abs(real - pred) <= band},
                    },
                }
            )
    return records


@pytest.fixture
def walk():
    """400 dates x 8 tickers of unpredictable returns."""
    return np.random.default_rng(41).normal(0.0, 0.015, size=(400, 8))


class TestEvaluate:
    def test_a_forecast_of_no_change_has_exactly_zero_skill(self, walk):
        result = st.evaluate(_records(np.zeros_like(walk), walk), model="gbm_quantile")
        assert result["skill"].point == pytest.approx(0.0)
        assert result["skill"].low == pytest.approx(0.0) and result["skill"].high == pytest.approx(0.0)

    def test_a_forecast_of_no_change_makes_no_directional_claim(self, walk):
        # Zero has no sign. Counting it as a miss would drag the hit rate
        # under 50% and make a harmless forecaster look wrong-way skilled.
        result = st.evaluate(_records(np.zeros_like(walk), walk), model="gbm_quantile")
        assert result["hit_rate"].point is None
        assert result["hit_rate"].reason == "no_observations"

    def test_a_perfect_forecast_has_full_skill_and_hit_rate(self, walk):
        result = st.evaluate(_records(walk.copy(), walk), model="gbm_quantile")
        assert result["skill"].point == pytest.approx(1.0)
        assert result["hit_rate"].point == pytest.approx(1.0)

    def test_noise_unrelated_to_the_outcome_is_worse_than_no_change(self, walk):
        noise = np.random.default_rng(42).normal(0.0, 0.005, size=walk.shape)
        result = st.evaluate(_records(noise, walk), model="gbm_quantile")
        assert result["skill"].point < 0.0
        assert result["hit_rate"].low <= 0.5 <= result["hit_rate"].high

    def test_coverage_is_reported_too(self, walk):
        result = st.evaluate(_records(np.zeros_like(walk), walk, band=1.0), model="gbm_quantile")
        assert result["coverage"].point == pytest.approx(1.0)

    def test_only_the_named_model_is_read(self, walk):
        records = _records(walk.copy(), walk, model="cheat") + _records(np.zeros_like(walk), walk)
        result = st.evaluate(records, model="gbm_quantile")
        assert result["skill"].point == pytest.approx(0.0)

    def test_counts_are_reported(self, walk):
        result = st.evaluate(_records(np.zeros_like(walk), walk), model="gbm_quantile")
        assert result["n_dates"] == 400
        assert result["n_forecasts"] == 3200


class TestVerdict:
    def test_no_skill_expected_and_none_found_passes(self, walk):
        noise = np.random.default_rng(43).normal(0.0, 0.005, size=walk.shape)
        verdict = st.verdict(st.evaluate(_records(noise, walk), model="gbm_quantile"), expect="no_skill")
        assert verdict == {"passed": True, "reasons": []}

    def test_being_worse_than_no_change_is_not_a_failure_of_the_harness(self, walk):
        # A fitted model on pure noise fits some of the noise and comes out
        # slightly worse than doing nothing. That is honest. The check is
        # one-sided: only skill that should not exist fails it.
        noise = np.random.default_rng(44).normal(0.0, 0.02, size=walk.shape)
        evaluation = st.evaluate(_records(noise, walk), model="gbm_quantile")
        assert evaluation["skill"].high < 0.0
        assert st.verdict(evaluation, expect="no_skill")["passed"] is True

    def test_a_lookahead_leak_is_caught_as_spurious_skill(self, walk):
        # The forecast has seen half of tomorrow. On a series where nothing
        # is predictable, this is what any leak looks like from outside.
        noise = np.random.default_rng(45).normal(0.0, 0.01, size=walk.shape)
        leaked = 0.5 * walk + noise
        verdict = st.verdict(st.evaluate(_records(leaked, walk), model="gbm_quantile"), expect="no_skill")
        assert verdict["passed"] is False
        assert "spurious_skill" in verdict["reasons"]
        assert "spurious_direction" in verdict["reasons"]

    def test_a_direction_only_leak_is_caught_even_when_error_looks_fine(self, walk):
        # Right sign, absurd size: the error is worse than no-change, so the
        # skill number alone would wave it through.
        leaked = 5.0 * walk
        evaluation = st.evaluate(_records(leaked, walk), model="gbm_quantile")
        assert evaluation["skill"].point < 0.0
        verdict = st.verdict(evaluation, expect="no_skill")
        assert verdict == {"passed": False, "reasons": ["spurious_direction"]}

    def test_skill_expected_and_found_passes(self, walk):
        noise = np.random.default_rng(46).normal(0.0, 0.005, size=walk.shape)
        verdict = st.verdict(st.evaluate(_records(0.5 * walk + noise, walk), model="gbm_quantile"), expect="skill")
        assert verdict == {"passed": True, "reasons": []}

    def test_skill_expected_and_missing_fails(self, walk):
        # The power check: a harness that cannot see a planted signal
        # cannot be trusted when it says there is none.
        noise = np.random.default_rng(47).normal(0.0, 0.005, size=walk.shape)
        verdict = st.verdict(st.evaluate(_records(noise, walk), model="gbm_quantile"), expect="skill")
        assert verdict == {"passed": False, "reasons": ["signal_not_detected"]}

    def test_too_little_data_cannot_certify_anything(self):
        # Fail closed: three dates support no interval, so neither "no
        # skill" nor "skill" can be signed off.
        actual = np.random.default_rng(48).normal(0.0, 0.015, size=(3, 8))
        evaluation = st.evaluate(_records(np.zeros_like(actual), actual), model="gbm_quantile")
        for expect in ("no_skill", "skill"):
            verdict = st.verdict(evaluation, expect=expect)
            assert verdict == {"passed": False, "reasons": ["too_few_blocks"]}

    def test_no_records_cannot_certify_anything(self):
        evaluation = st.evaluate([], model="gbm_quantile")
        assert st.verdict(evaluation, expect="no_skill") == {"passed": False, "reasons": ["no_observations"]}

    def test_unknown_expectation_is_rejected(self, walk):
        evaluation = st.evaluate(_records(np.zeros_like(walk), walk), model="gbm_quantile")
        with pytest.raises(ValueError):
            st.verdict(evaluation, expect="maybe")


# --------------------------------------------------------------------------
# End to end through the real, unmodified harness
# --------------------------------------------------------------------------


class TestThroughTheHarness:
    def test_random_walk_reports_no_skill(self, tmp_path):
        # The smallest run that still supports an interval. The full-size
        # three-scenario check is the command line (see PLAN.md, Rock 4);
        # this keeps the wiring honest in the suite.
        result = st.run_scenario("random_walk", work_dir=tmp_path, n_tickers=4, n_rows=260, seed=0)
        assert result["expect"] == "no_skill"
        assert result["n_forecasts"] > 0
        assert result["verdict"] == {"passed": True, "reasons": []}
        assert result["hit_rate"]["low"] <= 0.5

    def test_command_line_writes_a_result_and_exits_zero_when_all_pass(self, tmp_path, capsys):
        code = st.main(
            ["--scenario", "random_walk", "--work-dir", str(tmp_path), "--tickers", "4", "--rows", "260"]
        )
        assert code == 0
        assert (tmp_path / "synthetic_truth.json").is_file()
        assert "random_walk" in capsys.readouterr().out
