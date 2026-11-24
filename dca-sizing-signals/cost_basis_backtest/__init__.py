"""cost_basis_backtest: offline, walk-forward cost-basis comparison of a candidate sizing
rule against the graded DCA baseline, on the same cash flows.

Spec: docs/adr-cost-basis-backtest-harness.md. Research only: stdlib only, no network, no
broker, no repo config, no suggester imports, and nothing in the live path imports it. It
writes nothing; results are returned to the caller (and, if they choose, recorded in the
Phase 0 ledger's ``trials`` table through ``trial_row``).
"""

from cost_basis_backtest.candidate import Candidate, CandidateDecision, SuggesterCandidate, ZeroSignalCandidate
from cost_basis_backtest.compare import VariantComparison, compare_variants
from cost_basis_backtest.engine import BacktestConfig, BacktestResult, DayRecord, run_backtest
from cost_basis_backtest.ledger_io import baseline_from_ledger_fills, rejected_trial_row, trial_row
from cost_basis_backtest.metrics import FillRecord
from cost_basis_backtest.overfitting import (
    PBOResult,
    deflated_sharpe,
    expected_max_sharpe,
    probabilistic_sharpe,
    probability_of_backtest_overfitting,
    sharpe,
)
from cost_basis_backtest.view import BacktestInputError, LookaheadError, PointInTimeView, PriceIndex

__all__ = [
    "BacktestConfig",
    "BacktestInputError",
    "BacktestResult",
    "Candidate",
    "CandidateDecision",
    "DayRecord",
    "FillRecord",
    "LookaheadError",
    "PBOResult",
    "PointInTimeView",
    "PriceIndex",
    "SuggesterCandidate",
    "VariantComparison",
    "ZeroSignalCandidate",
    "baseline_from_ledger_fills",
    "compare_variants",
    "deflated_sharpe",
    "expected_max_sharpe",
    "probabilistic_sharpe",
    "probability_of_backtest_overfitting",
    "rejected_trial_row",
    "run_backtest",
    "sharpe",
    "trial_row",
]
