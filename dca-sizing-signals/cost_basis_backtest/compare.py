"""Compare several variants of a candidate without fooling yourself.

``compare_variants`` takes finished BacktestResults over the same baseline and computes,
on their daily active-return series (aligned on the sessions every variant has): the best
variant by out-of-sample Sharpe, its deflated Sharpe ratio counting every trial, and the
CSCV probability of backtest overfitting across all of them.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from cost_basis_backtest.engine import BacktestResult
from cost_basis_backtest.overfitting import PBOResult, deflated_sharpe, probability_of_backtest_overfitting, sharpe


@dataclass(frozen=True)
class VariantComparison:
    best: str | None
    sharpes: Mapping[str, float | None]
    n_trials: int
    trial_sharpe_variance: float | None
    deflated_sharpe: float | None
    pbo: PBOResult | None
    n_sessions: int
    notes: tuple[str, ...]


def compare_variants(results: Mapping[str, BacktestResult], *, n_trials: int | None = None,
                     start: date | None = None, n_blocks: int = 10) -> VariantComparison:
    """Args:
        results: variant name -> BacktestResult. Rejected results are refused: a variant that
            broke the point-in-time rule must not quietly drop out of the trial count.
        n_trials: every configuration ever tried for this experiment -- pass the ledger's
            ``LedgerStore.trial_count(experiment=...)``. Never fewer than ``len(results)``.
        start: score only sessions from here (normally the out-of-sample start).
        n_blocks: CSCV blocks (even). 10 gives C(10, 5) = 252 splits.
    """
    notes: list[str] = []
    if not results:
        raise ValueError("no variants to compare")
    rejected = sorted(n for n, r in results.items() if r.status != "ok")
    if rejected:
        raise ValueError(f"rejected variants cannot be compared: {rejected}")
    trials = max(len(results), n_trials or 0)
    if n_trials is not None and n_trials < len(results):
        notes.append(f"n_trials {n_trials} < {len(results)} variants passed; using {len(results)}")

    series: dict[str, dict[date, float]] = {
        n: {d: rc - rb for d, rb, rc in r.active_returns if start is None or d >= start} for n, r in results.items()
    }
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    aligned = {n: [s[d] for d in common] for n, s in series.items()}
    sharpes = {n: sharpe(xs) for n, xs in aligned.items()}
    scored = {n: s for n, s in sharpes.items() if s is not None}
    best = max(sorted(scored), key=lambda n: scored[n]) if scored else None

    var = statistics.variance(scored.values()) if len(scored) >= 2 else None
    dsr = None
    if best is not None:
        if trials == 1:
            dsr = deflated_sharpe(aligned[best], 1, 0.0)
        elif var is not None:
            dsr = deflated_sharpe(aligned[best], trials, var)
            if trials > len(scored):
                notes.append("trial Sharpe variance estimated from the variants passed in only")
        else:
            notes.append("deflated Sharpe needs the Sharpes of at least two trials to estimate their variance")

    pbo = None
    if len(aligned) >= 2:
        try:
            pbo = probability_of_backtest_overfitting(aligned, n_blocks=n_blocks)
        except ValueError as exc:
            notes.append(f"PBO not computed: {exc}")
    return VariantComparison(best, sharpes, trials, var, dsr, pbo, len(common), tuple(notes))
