"""End to end: residual_reversal replayed against a graded-style baseline on synthetic
data with real OU residual reversal. These check the harness's plumbing on the first
target, not the strategy's merit -- the data are synthetic."""

from __future__ import annotations

import json
from datetime import date

import pytest

from cost_basis_backtest import BacktestConfig, run_backtest
from residual_reversal import CorporateEvent

from ._binding import residual_reversal_candidate
from ._data import ETF, graded_baseline, universe

TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")


@pytest.fixture(scope="module")
def replay():
    rows, dates = universe(n_sessions=300)
    buys = graded_baseline(rows, dates, TICKERS)
    cfg = BacktestConfig(oos_start=dates[210], buy_cost_bps=3.0, min_bucket_count=5)
    return dates, run_backtest(residual_reversal_candidate({t: ETF for t in TICKERS}), rows, buys, config=cfg)


def test_replay_is_ok_and_budget_neutral(replay):
    _, r = replay
    assert r.status == "ok", r.reason
    assert r.metrics["budget_neutral"] is True
    assert r.metrics["max_abs_net_tilt_usd"] < 1e-6
    assert any(f.multiplier != 1.0 for f in r.fills)
    assert all(0.75 - 1e-9 <= f.multiplier <= 1.25 + 1e-9 for f in r.fills)


def test_every_requested_metric_is_reported(replay):
    _, r = replay
    for name in ("all", "in_sample", "out_of_sample"):
        p = r.metrics["periods"][name]
        assert p["cost_basis"]["aggregate"]["n_tickers"] == len(TICKERS)
        assert p["terminal_wealth_per_dollar"]["difference_bp"] is not None
        assert set(p["tilt_forward_return"]) == {"5", "10", "20", "terminal"}
        assert set(p["hit_rate"]) == {"5", "10", "20"}
        assert p["turnover"]["gross_tilt_fraction"] > 0.0
        for b in p["score_buckets"].values():
            assert len(b["buckets"]) == 5 and b["n"] > 0
    assert r.metrics["periods"]["all"]["worst_rolling_underperformance_bp"] is not None
    assert set(r.promotion) == {"passed", "monotone_by_horizon", "reasons"}
    assert set(r.promotion["monotone_by_horizon"]) == {"5", "10", "20"}
    json.dumps(r.summary(), allow_nan=False)


def test_scores_and_hedges_reach_the_fills(replay):
    _, r = replay
    scored = [f for f in r.fills if f.score is not None]
    assert scored and all(f.hedge is not None and f.hedge[0] == ETF for f in scored)


def test_8k_enters_on_its_filed_date():
    """An 8-K filed on session t guards the ticker from t+1, not on t (data up to t-1)."""
    rows, dates = universe(n_sessions=200)
    buys = graded_baseline(rows, dates, TICKERS, first=150)
    filed = dates[170]
    events = {"AAA": [CorporateEvent("8k_1.01", event_date=filed, known_at=filed)]}
    cand = residual_reversal_candidate({t: ETF for t in TICKERS})
    with_event = run_backtest(cand, rows, buys, events_by_ticker=events)
    without = run_backtest(cand, rows, buys)

    def aaa(result, d: date):
        return next(f for f in result.fills if f.ticker == "AAA" and f.session == d)

    # Through the filing session itself AAA is decided exactly as without the event...
    for d in dates[150:171]:
        assert aaa(with_event, d).score == aaa(without, d).score
    # ...and from the next session it is guarded: no score, multiplier 1.0.
    for d in dates[171:180]:
        assert aaa(with_event, d).score is None and aaa(with_event, d).multiplier == 1.0
