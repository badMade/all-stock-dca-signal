"""Hand-checked metric arithmetic on toy inputs small enough to verify on paper."""

from __future__ import annotations

import math
from datetime import date

import pytest

from cost_basis_backtest import BacktestConfig, BacktestInputError, CandidateDecision, PointInTimeView, run_backtest
from cost_basis_backtest import metrics as m
from cost_basis_backtest.view import PriceIndex

D = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]


class Fixed:
    """Returns pre-set multipliers per session (reads nothing)."""

    name = "fixed"

    def __init__(self, by_day, scores=None):
        self.by_day, self.scores = by_day, scores or {}

    def decide(self, view, buys):
        return CandidateDecision(self.by_day.get(view.asof, {}), self.scores.get(view.asof, {}))


def _rows(prices):
    return [{"ticker": t, "date": d, "close": p} for t, series in prices.items() for d, p in zip(D, series,
                                                                                                strict=True)]


def test_cost_basis_by_hand():
    # One ticker, $10 a day at 10 then 5. Candidate halves day 1 and adds half on day 2.
    rows = _rows({"AAA": [10.0, 5.0, 5.0, 8.0]})
    buys = {D[0]: {"AAA": 10.0}, D[1]: {"AAA": 10.0}}
    r = run_backtest(Fixed({D[0]: {"AAA": 0.5}, D[1]: {"AAA": 1.5}}), rows, buys)
    per = r.metrics["periods"]["all"]["cost_basis"]["per_ticker"]["AAA"]
    assert per["baseline_cost_basis"] == pytest.approx(20.0 / 3.0)        # 20 / (1 + 2)
    assert per["candidate_cost_basis"] == pytest.approx(20.0 / 3.5)       # 20 / (0.5 + 3)
    assert per["improvement_bp"] == pytest.approx((20 / 3 - 20 / 3.5) / (20 / 3) * 1e4)
    tw = r.metrics["periods"]["all"]["terminal_wealth_per_dollar"]
    assert tw["baseline"] == pytest.approx(3 * 8.0 / 20) and tw["candidate"] == pytest.approx(3.5 * 8.0 / 20)
    # Tilt: -5 at 10 then +5 at 5. To the end (8): (-5 * (0.8 - 1) + 5 * (1.6 - 1)) / 10 = 0.4.
    assert r.metrics["periods"]["all"]["tilt_forward_return"]["terminal"]["return_bp"] == pytest.approx(4000.0)
    assert r.metrics["periods"]["all"]["turnover"]["gross_tilt_fraction"] == pytest.approx(0.5)
    assert r.metrics["periods"]["all"]["turnover"]["mean_multiplier_change"] == pytest.approx(1.0)
    assert r.metrics["budget_neutral"] is False  # one ticker cannot be tilted neutrally


def test_one_way_cost_enters_cost_basis_in_both_arms():
    rows = _rows({"AAA": [10.0, 5.0, 5.0, 8.0]})
    buys = {D[0]: {"AAA": 10.0}, D[1]: {"AAA": 10.0}}
    r = run_backtest(Fixed({D[0]: {"AAA": 0.5}, D[1]: {"AAA": 1.5}}), rows, buys,
                     config=BacktestConfig(buy_cost_bps=100.0))
    per = r.metrics["periods"]["all"]["cost_basis"]["per_ticker"]["AAA"]
    assert per["baseline_cost_basis"] == pytest.approx(20.0 / (3 * 0.99))
    assert per["candidate_cost_basis"] == pytest.approx(20.0 / (3.5 * 0.99))
    # A proportional cost scales both arms alike, so the paired improvement is unchanged.
    assert per["improvement_bp"] == pytest.approx((1 - 3 / 3.5) * 1e4)


def test_hit_rate_is_relative_to_the_days_mean():
    # Day 0: AAA +10% to day 1, BBB +0%. Candidate overweights AAA, underweights BBB: 2 hits.
    rows = _rows({"AAA": [10.0, 11.0, 11.0, 11.0], "BBB": [10.0, 10.0, 10.0, 10.0]})
    buys = {D[0]: {"AAA": 10.0, "BBB": 10.0}}
    r = run_backtest(Fixed({D[0]: {"AAA": 1.2, "BBB": 0.8}}), rows, buys, config=BacktestConfig(horizons=(1,)))
    assert r.metrics["periods"]["all"]["hit_rate"]["1"] == {"rate": 1.0, "n": 2}
    assert r.metrics["budget_neutral"] is True
    # Tilt return over 1 session: (+2 * 0.10 + -2 * 0) / 4 = 5%.
    assert r.metrics["periods"]["all"]["tilt_forward_return"]["1"]["return_bp"] == pytest.approx(500.0)


def test_missing_fill_close_means_neither_arm_buys():
    rows = [r for r in _rows({"AAA": [10.0, 5.0, 5.0, 8.0], "BBB": [1.0, 1.0, 1.0, 1.0]})
            if not (r["ticker"] == "AAA" and r["date"] == D[1])]
    buys = {D[1]: {"AAA": 10.0, "BBB": 10.0}}
    r = run_backtest(Fixed({}), rows, buys)
    assert r.metrics["unfilled_orders"] == 1
    assert [f.ticker for f in r.fills] == ["BBB"]


def test_score_buckets_and_monotonicity():
    pts = [(-3.0, 0.02)] * 5 + [(0.0, 0.0)] * 5 + [(3.0, -0.02)] * 5
    out = m.score_buckets(pts, (-1.0, 1.0), min_count=5, score_sign=-1)
    assert [b["n"] for b in out["buckets"]] == [5, 5, 5]
    assert out["monotone"] is True and out["spearman"] == pytest.approx(-1.0)
    assert m.score_buckets(pts, (-1.0, 1.0), min_count=5, score_sign=1)["monotone"] is False
    assert m.score_buckets(pts, (-1.0, 1.0), min_count=6, score_sign=-1)["monotone"] is None  # too thin


def test_forward_residual_uses_the_hedge():
    idx = PriceIndex(_rows({"AAA": [10.0, 11.0, 12.1, 12.1], "XLK": [10.0, 11.0, 12.1, 12.1]}))
    f = m.FillRecord(D[0], "AAA", 10.0, 1.0, 1.0, 0.1, 0.1, 1.0, -2.0, ("XLK", 1.0))
    (score, resid), = m.forward_residuals([f], idx, 2)
    assert score == -2.0 and resid == pytest.approx(0.0, abs=1e-12)  # moved with its ETF exactly


def test_worst_rolling():
    active = [(D[0], 0.0, -0.01), (D[1], 0.0, -0.01), (D[2], 0.0, 0.05)]
    assert m.worst_rolling(active, 2) == pytest.approx((0.99 * 0.99 - 1) * 1e4)
    assert m.worst_rolling(active, 4) is None


def test_view_hides_the_decision_session_and_later():
    idx = PriceIndex(_rows({"AAA": [10.0, 11.0, 12.0, 13.0]}))
    ev_known_today = {"known_at": D[2]}
    ev_known_before = {"known_at": D[1]}
    view = PointInTimeView(idx, {"AAA": [ev_known_today, ev_known_before]}, D[2])
    assert [d for d, _ in view.closes("AAA")] == D[:2]
    assert view.sessions() == tuple(D[:2])
    assert all(r["date"] < D[2] and r["final"] is True for r in view.close_rows())
    assert view.events("AAA") == (ev_known_before,)  # an 8-K filed on t is not visible at t
    assert view.close("AAA", D[1]) == 11.0
    assert view.violations == []


@pytest.mark.parametrize("bad_buys", [
    {},
    {D[0]: {"AAA": -1.0}},
    {D[0]: {"AAA": math.nan}},
    {D[0]: {"": 1.0}},
    {"not-a-date": {"AAA": 1.0}},
])
def test_bad_baseline_fails_closed(bad_buys):
    with pytest.raises(BacktestInputError):
        run_backtest(Fixed({}), _rows({"AAA": [10.0, 11.0, 12.0, 13.0]}), bad_buys)


@pytest.mark.parametrize("bad_rows", [
    [{"ticker": "AAA", "date": D[0], "close": 0.0}],
    [{"ticker": "AAA", "date": D[0], "close": 1.0}, {"ticker": "AAA", "date": D[0], "close": 2.0}],
    [{"ticker": "AAA", "date": "garbage", "close": 1.0}],
    [],
])
def test_bad_closes_fail_closed(bad_rows):
    with pytest.raises(BacktestInputError):
        run_backtest(Fixed({}), bad_rows, {D[0]: {"AAA": 1.0}})


def test_event_without_known_at_fails_closed():
    with pytest.raises(BacktestInputError):
        run_backtest(Fixed({}), _rows({"AAA": [10.0, 11.0, 12.0, 13.0]}), {D[1]: {"AAA": 1.0}},
                     events_by_ticker={"AAA": [{"kind": "8k_1.01"}]})


@pytest.mark.parametrize("decision", [
    {"ZZZ": 1.1},          # a ticker the baseline did not propose
    {"AAA": -0.1},
    {"AAA": math.inf},
    {"AAA": 99.0},
])
def test_malformed_decision_rejects_the_run(decision):
    r = run_backtest(Fixed({D[1]: decision}), _rows({"AAA": [10.0, 11.0, 12.0, 13.0]}), {D[1]: {"AAA": 1.0}})
    assert r.status == "rejected" and r.reason.startswith("invalid_decision")


def test_summary_is_json_safe():
    import json

    rows = _rows({"AAA": [10.0, 5.0, 5.0, 8.0]})
    r = run_backtest(Fixed({D[0]: {"AAA": 0.5}}), rows, {D[0]: {"AAA": 10.0}, D[1]: {"AAA": 10.0}},
                     config=BacktestConfig(oos_start=D[1]))
    json.dumps(r.summary(), allow_nan=False)
    assert r.summary()["metrics"]["survivor_biased"] is True
