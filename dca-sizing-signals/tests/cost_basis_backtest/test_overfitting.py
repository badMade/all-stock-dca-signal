"""Limiting-case checks of the deflated Sharpe ratio and CSCV PBO, and compare_variants."""

from __future__ import annotations

import random

import pytest

from cost_basis_backtest import (
    BacktestConfig,
    CandidateDecision,
    compare_variants,
    deflated_sharpe,
    expected_max_sharpe,
    probabilistic_sharpe,
    probability_of_backtest_overfitting,
    run_backtest,
    sharpe,
)
from cost_basis_backtest.overfitting import skew_kurtosis

from ._binding import residual_reversal_candidate
from ._data import ETF, graded_baseline, universe


def _noise(seed, n=400, mu=0.0):
    rng = random.Random(seed)
    return [rng.gauss(mu, 0.01) for _ in range(n)]


def test_expected_max_sharpe_grows_with_trials():
    assert expected_max_sharpe(1, 0.01) == 0.0
    values = [expected_max_sharpe(n, 0.01) for n in (2, 10, 100, 1000)]
    assert values == sorted(values) and values[0] > 0


def test_one_trial_dsr_is_the_psr_against_zero():
    xs = _noise(1, mu=0.002)
    skew, kurt = skew_kurtosis(xs)
    assert deflated_sharpe(xs, 1, 0.0) == pytest.approx(probabilistic_sharpe(sharpe(xs), 0.0, len(xs), skew, kurt))


def test_more_trials_deflate_more():
    xs = _noise(2, mu=0.001)
    assert deflated_sharpe(xs, 100, 0.004) < deflated_sharpe(xs, 5, 0.004) < deflated_sharpe(xs, 1, 0.0)


def test_skew_kurtosis_of_a_normal_sample():
    skew, kurt = skew_kurtosis(_noise(3, n=20000))
    assert abs(skew) < 0.1 and abs(kurt - 3.0) < 0.15


def test_pbo_low_for_a_genuinely_better_variant():
    series = {f"noise{i}": _noise(10 + i) for i in range(5)}
    series["real"] = [x + 0.004 for x in _noise(99)]
    assert probability_of_backtest_overfitting(series).pbo < 0.1


def test_pbo_near_half_for_pure_noise():
    series = {f"v{i}": _noise(100 + i) for i in range(8)}
    result = probability_of_backtest_overfitting(series)
    assert result.n_splits == 252 and 0.2 < result.pbo < 0.8


def test_pbo_input_checks():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting({"a": _noise(1)})
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting({"a": _noise(1), "b": _noise(2, n=10)})
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting({"a": _noise(1), "b": _noise(2)}, n_blocks=3)


TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")


def test_compare_variants_counts_every_trial():
    rows, dates = universe()
    buys = graded_baseline(rows, dates, TICKERS)
    bench = {t: ETF for t in TICKERS}
    from residual_reversal import ResidualReversalConfig

    results = {
        f"slope_{s}": run_backtest(residual_reversal_candidate(bench, ResidualReversalConfig(slope=s)), rows, buys,
                                   config=BacktestConfig(oos_start=dates[190]))
        for s in (0.05, 0.1, 0.2)
    }
    cmp_few = compare_variants(results, start=dates[190])
    cmp_many = compare_variants(results, n_trials=50, start=dates[190])
    assert cmp_few.n_trials == 3 and cmp_many.n_trials == 50
    assert cmp_few.best in results
    assert cmp_many.deflated_sharpe <= cmp_few.deflated_sharpe
    assert cmp_few.pbo is not None and 0.0 <= cmp_few.pbo.pbo <= 1.0


def test_compare_variants_refuses_rejected_runs():
    rows, dates = universe(n_sessions=140)
    buys = graded_baseline(rows, dates, TICKERS)

    class Peek:
        name = "peek"

        def decide(self, view, b):
            view.close("AAA", view.asof)
            return CandidateDecision()

    rejected = run_backtest(Peek(), rows, buys)
    with pytest.raises(ValueError, match="rejected"):
        compare_variants({"peek": rejected})
