"""A suggester that peeks at tomorrow's return must be rejected or flagged, never scored
as a clean win. Three ways to peek, three outcomes:

* through the view (ask for the fill session's close)          -> run rejected;
* through the view, swallowing the LookaheadError and carrying on -> still rejected;
* around the view (a closure over the full dataset)               -> flagged by the IC canary,
  and the promotion test fails.

And the honest residual_reversal binding trips none of them."""

from __future__ import annotations

import pytest

from cost_basis_backtest import BacktestConfig, CandidateDecision, LookaheadError, run_backtest

from ._binding import residual_reversal_candidate
from ._data import ETF, closes_by, graded_baseline, universe

TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")


@pytest.fixture(scope="module")
def data():
    rows, dates = universe()
    return rows, dates, graded_baseline(rows, dates, TICKERS)


class ViewPeeker:
    """Tilts toward names that close up on the fill session -- by asking the view for it."""

    name = "view_peeker"

    def __init__(self, swallow: bool = False):
        self.swallow = swallow

    def decide(self, view, buys):
        prev = view.sessions()[-1]
        mult = {}
        for t in buys:
            try:
                today = view.close(t, view.asof)
            except LookaheadError:
                if not self.swallow:
                    raise
                continue
            mult[t] = 1.25 if today > view.close(t, prev) else 0.75
        return CandidateDecision(mult)


class ClosurePeeker:
    """Closes over the whole dataset and leans into tomorrow's (t -> t+1) winners. The view
    cannot stop this, so the statistics must."""

    name = "closure_peeker"

    def __init__(self, rows, dates):
        self.closes = closes_by(rows)
        self.next = dict(zip(dates, dates[1:], strict=False))

    def decide(self, view, buys):
        t = view.asof
        nxt = self.next.get(t)
        if nxt is None:
            return CandidateDecision()
        mult = {}
        for ticker in buys:
            r = self.closes[(ticker, nxt)] / self.closes[(ticker, t)] - 1.0
            mult[ticker] = 1.25 if r > 0 else 0.75
        return CandidateDecision(mult)


def test_peeking_through_the_view_is_rejected(data):
    rows, dates, buys = data
    result = run_backtest(ViewPeeker(), rows, buys)
    assert result.status == "rejected"
    assert result.reason.startswith("lookahead_access:")
    assert result.metrics == {} and result.fills == ()


def test_swallowing_the_lookahead_error_is_still_rejected(data):
    rows, dates, buys = data
    result = run_backtest(ViewPeeker(swallow=True), rows, buys)
    assert result.status == "rejected"
    assert "lookahead_access" in result.reason


def test_peeking_around_the_view_is_flagged_and_cannot_promote(data):
    rows, dates, buys = data
    result = run_backtest(ClosurePeeker(rows, dates), rows, buys, config=BacktestConfig(oos_start=dates[190]))
    assert result.status == "ok"
    assert any(f.startswith("suspected_lookahead:after_fill") for f in result.lookahead_flags)
    assert result.metrics["lookahead_ic"]["after_fill"]["mean_ic"] > 0.5
    assert result.promotion["passed"] is False
    assert "lookahead canary tripped" in result.promotion["reasons"]
    # ...and the oracle's "edge" is exactly the kind of number that would otherwise look real:
    # every one of its tilts wins relative to the day's mean on the next session.
    assert result.metrics["periods"]["all"]["tilt_forward_return"]["5"]["return_bp"] > 0.0


def test_honest_residual_reversal_passes_the_lookahead_checks(data):
    rows, dates, buys = data
    cand = residual_reversal_candidate({t: ETF for t in TICKERS})
    result = run_backtest(cand, rows, buys, config=BacktestConfig(oos_start=dates[190]))
    assert result.status == "ok", result.reason
    assert result.lookahead_flags == ()
    for stats in result.metrics["lookahead_ic"].values():
        assert stats["mean_ic"] is None or abs(stats["mean_ic"]) < 0.3


def test_future_rows_cannot_change_past_decisions(data):
    """Rewrite every close after session k: decisions up to k must be bit-identical."""
    rows, dates, buys = data
    k = dates[170]
    scrambled = [dict(r, close=r["close"] * (1.5 if r["date"] > k.isoformat() else 1.0)) for r in rows]
    cand = residual_reversal_candidate({t: ETF for t in TICKERS})
    a = run_backtest(cand, rows, buys)
    b = run_backtest(cand, scrambled, buys)
    # A decision for session t reads closes before t, so decisions through k+1 are untouched.
    cutoff = dates[171]
    mult_a = {(f.session, f.ticker): (f.multiplier, f.score) for f in a.fills if f.session <= cutoff}
    mult_b = {(f.session, f.ticker): (f.multiplier, f.score) for f in b.fills if f.session <= cutoff}
    assert mult_a == mult_b
    assert any(m != 1.0 for m, _ in mult_a.values()), "the binding should have tilted something by then"
