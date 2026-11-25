"""The residual_reversal binding: how a caller points the harness at
ResidualReversalSuggester. Kept in tests (and copied into the README) rather than in either
package, so neither package imports the other (tests/test_architecture_boundaries.py)."""

from __future__ import annotations

from datetime import datetime, timezone

from cost_basis_backtest import SuggesterCandidate
from residual_reversal import ResidualReversalConfig, ResidualReversalInputs, ResidualReversalSuggester

FIXED_NOW = datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)


def residual_reversal_candidate(benchmark_by_ticker: dict[str, str],
                                config: ResidualReversalConfig | None = None,
                                name: str = "residual_reversal") -> SuggesterCandidate:
    cfg = config or ResidualReversalConfig()
    # Enough calendar sessions for the beta window, the half-life baseline and a margin for
    # missing days; bounding the window keeps the replay linear in the backtest length.
    lookback = cfg.beta_window + cfg.half_life_lookback + 15

    def build(view, buys):
        tickers = sorted(set(buys) | {benchmark_by_ticker[t] for t in buys if t in benchmark_by_ticker})

        def load_inputs(asof):
            return ResidualReversalInputs(
                close_rows=view.close_rows(tickers, lookback_sessions=lookback),
                proposed_buys_usd_by_ticker=dict(buys),
                benchmark_by_ticker=dict(benchmark_by_ticker),
                # view.events() already dropped anything known on or after the decision
                # session; residual_reversal re-applies its own known_at <= as_of rule.
                events_by_ticker=view.events_by_ticker(sorted(buys)),
            )

        return ResidualReversalSuggester(load_inputs, config=cfg, clock=lambda: FIXED_NOW)

    return SuggesterCandidate(name, build)
