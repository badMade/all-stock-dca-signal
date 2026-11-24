"""A candidate that tilts nothing must reproduce the baseline exactly -- not approximately.

If this fails, any cost-basis "improvement" the harness reports for a real candidate could
be the harness's own arithmetic, and a few-bp edge would be indistinguishable from it."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cost_basis_backtest import BacktestConfig, SuggesterCandidate, ZeroSignalCandidate, run_backtest
from dca_shared_interfaces.forecasting import SizingSuggestion

from ._data import graded_baseline, universe

TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")


@pytest.fixture(scope="module")
def data():
    rows, dates = universe()
    return rows, dates, graded_baseline(rows, dates, TICKERS)


def _assert_identical(result):
    assert result.status == "ok"
    for period in result.metrics["periods"].values():
        per = period["cost_basis"]["per_ticker"]
        assert set(per) == set(TICKERS)
        for v in per.values():
            assert v["baseline_cost_basis"] == v["candidate_cost_basis"]  # bit-identical
            assert v["improvement_bp"] == 0.0
            assert v["baseline_usd"] == v["candidate_usd"]
        assert period["cost_basis"]["aggregate"]["mean_bp"] == 0.0
        tw = period["terminal_wealth_per_dollar"]
        assert tw["baseline"] == tw["candidate"] and tw["difference_bp"] == 0.0
        assert all(v["n_tilts"] == 0 and v["return_bp"] is None for v in period["tilt_forward_return"].values())
        assert period["turnover"]["gross_tilt_fraction"] == 0.0
        assert period["worst_rolling_underperformance_bp"] in (0.0, None)
    for f in result.fills:
        assert f.base_shares == f.cand_shares and f.base_usd == f.cand_usd and f.multiplier == 1.0
    assert result.active_returns and all(rb == rc for _, rb, rc in result.active_returns)
    assert result.metrics["budget_neutral"] is True
    assert result.lookahead_flags == ()


@pytest.mark.parametrize("cost_bps,per_order", [(0.0, 0.0), (5.0, 0.0), (12.5, 0.02)])
def test_zero_signal_reproduces_baseline_exactly(data, cost_bps, per_order):
    rows, dates, buys = data
    cfg = BacktestConfig(buy_cost_bps=cost_bps, per_order_cost_usd=per_order, oos_start=dates[200])
    _assert_identical(run_backtest(ZeroSignalCandidate(), rows, buys, config=cfg))


class _NeutralSuggester:
    """Publishes a 0.0 adjustment for every ticker -- a suggester that exists but says
    nothing. The adapter must map it to exactly 1.0."""

    def __init__(self, buys):
        self._buys = buys

    def suggest(self, ticker, asof):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return SizingSuggestion(ticker, asof, 0.0, float("inf"), "test", "v0", when)


def test_neutral_suggester_through_the_adapter_is_exact(data):
    rows, dates, buys = data
    cand = SuggesterCandidate("neutral", lambda view, b: _NeutralSuggester(b))
    _assert_identical(run_backtest(cand, rows, buys, config=BacktestConfig(buy_cost_bps=5.0)))


def test_always_failing_candidate_is_zero_tilt_and_counted(data):
    rows, dates, buys = data

    class Broken:
        name = "broken"

        def decide(self, view, b):
            raise RuntimeError("boom")

    result = run_backtest(Broken(), rows, buys)
    _assert_identical(result)
    assert result.metrics["unavailable_sessions"] == {"candidate_error:RuntimeError": len(buys)}
