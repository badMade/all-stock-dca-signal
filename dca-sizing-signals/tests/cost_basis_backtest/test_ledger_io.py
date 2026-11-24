"""The two ledger seams: fills in, trials out."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from cost_basis_backtest import (
    BacktestConfig,
    BacktestInputError,
    ZeroSignalCandidate,
    baseline_from_ledger_fills,
    rejected_trial_row,
    run_backtest,
    trial_row,
)

from ._binding import residual_reversal_candidate
from ._data import ETF, graded_baseline, universe

TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")


def test_baseline_from_ledger_fills_sums_scheduled_only():
    rows = [
        {"run_kind": "scheduled", "symbol": "AAA", "session": date(2026, 1, 5), "notional": 4.0},
        {"run_kind": "scheduled", "symbol": "AAA", "session": "2026-01-05", "notional": 6.0},  # two slots
        {"run_kind": "manual", "symbol": "AAA", "session": date(2026, 1, 5), "notional": 100.0},
        {"run_kind": "scheduled", "symbol": "BBB", "session": date(2026, 1, 6), "notional": 2.5},
    ]
    assert baseline_from_ledger_fills(rows) == {date(2026, 1, 5): {"AAA": 10.0}, date(2026, 1, 6): {"BBB": 2.5}}


@pytest.mark.parametrize("row", [
    {"run_kind": "scheduled", "symbol": "AAA", "session": date(2026, 1, 5)},
    {"run_kind": "live", "symbol": "AAA", "session": date(2026, 1, 5), "notional": 1.0},
    {"run_kind": "scheduled", "symbol": "AAA", "session": date(2026, 1, 5), "notional": 0.0},
])
def test_baseline_from_ledger_fills_fails_closed(row):
    with pytest.raises(BacktestInputError):
        baseline_from_ledger_fills([row])


@pytest.fixture(scope="module")
def rr_result():
    rows, dates = universe()
    buys = graded_baseline(rows, dates, TICKERS)
    return run_backtest(residual_reversal_candidate({t: ETF for t in TICKERS}), rows, buys,
                        config=BacktestConfig(oos_start=dates[190], buy_cost_bps=3.0))


def test_trial_row_is_accepted_by_the_ledger(tmp_path, rr_result):
    pytest.importorskip("duckdb")
    from market_data_ingest.ledger.store import LedgerStore

    when = datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)
    row = trial_row(rr_result, trial_id="rr-v1-slope0.1", experiment="residual_reversal_cost_basis",
                    config={"slope": 0.1}, evaluated_at=when)
    assert row["metrics"]["sharpe"] == rr_result.metrics["periods"]["out_of_sample"]["active_daily_sharpe"]
    rejected = rejected_trial_row(run_backtest(ZeroSignalCandidate(), [{"ticker": "A", "date": "2026-01-05",
                                                                        "close": 1.0}],
                                               {date(2026, 1, 5): {"A": 1.0}}),
                                  trial_id="rr-v0-broken", experiment="residual_reversal_cost_basis",
                                  config={}, evaluated_at=when)
    with LedgerStore(tmp_path / "ledger.duckdb") as store:
        store.ingest_trials([row, rejected], source="cost_basis_backtest", run_id="test", as_of=when)
        assert store.trial_count(experiment="residual_reversal_cost_basis") == 2


def test_trial_row_refuses_a_rejected_result():
    from cost_basis_backtest import CandidateDecision

    class Peek:
        name = "peek"

        def decide(self, view, b):
            view.close("A", view.asof)
            return CandidateDecision()

    r = run_backtest(Peek(), [{"ticker": "A", "date": "2026-01-05", "close": 1.0}], {date(2026, 1, 5): {"A": 1.0}})
    assert r.status == "rejected"
    with pytest.raises(ValueError):
        trial_row(r, trial_id="x", experiment="e", config={}, evaluated_at=datetime.now(timezone.utc))
