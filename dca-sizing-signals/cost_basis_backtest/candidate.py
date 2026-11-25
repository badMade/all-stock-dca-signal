"""Candidates: what the harness replays beside the graded baseline.

A candidate turns one session's point-in-time view plus the baseline's proposed dollars into
per-ticker multipliers on those dollars. A ticker it leaves out keeps 1.0 -- the same
fallback SizingAdjustmentReader applies when a suggester publishes nothing for a ticker
(residual_reversal's guarded tickers, a failed run).

This package imports no suggester. ``SuggesterCandidate`` adapts anything shaped like a
dca_shared_interfaces SizingSuggester by duck typing, and the caller supplies the factory
that builds one from the view (see tests/cost_basis_backtest/_binding.py for the
residual_reversal binding). That keeps the two-way boundary in
tests/test_architecture_boundaries.py intact: no suggester learns the harness exists and the
harness learns no suggester exists.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cost_basis_backtest.view import PointInTimeView


@dataclass(frozen=True)
class CandidateDecision:
    """One session's decision.

    Attributes:
        multipliers: ticker -> multiplier on the baseline's proposed dollars. Absent = 1.0.
        scores: ticker -> the signal value behind the tilt (residual_reversal's s-score), for
            the bucket analysis. Absent = the ticker is not in any bucket that day.
        hedges: ticker -> (benchmark ticker, beta), for forward *residual* returns. Absent =
            the forward return is demeaned against that day's other scored tickers instead.
        available: False when the candidate produced nothing (failed closed). The session is
            then replayed at 1.0 for every ticker and counted, never silently dropped.
        reason: why it was unavailable.
    """

    multipliers: Mapping[str, float] = field(default_factory=dict)
    scores: Mapping[str, float] = field(default_factory=dict)
    hedges: Mapping[str, tuple[str, float]] = field(default_factory=dict)
    available: bool = True
    reason: str = "ok"


@runtime_checkable
class Candidate(Protocol):
    name: str

    def decide(self, view: PointInTimeView, baseline_buys: Mapping[str, float]) -> CandidateDecision: ...


class ZeroSignalCandidate:
    """Tilts nothing. Its replay must reproduce the baseline's cost basis exactly -- the
    harness's own correctness check (tests/cost_basis_backtest/test_zero_signal.py)."""

    name = "zero_signal"

    def decide(self, view: PointInTimeView, baseline_buys: Mapping[str, float]) -> CandidateDecision:
        return CandidateDecision()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


class SuggesterCandidate:
    """Adapt a SizingSuggester-shaped object, rebuilt for every session.

    ``build(view, baseline_buys)`` returns the suggester for that session; the view is the
    only data it should close over. Then:

    * if it has ``evaluate(asof)`` returning an outcome with ``ok``/``reason``/``suggestions``
      (comovement_signals and residual_reversal do), one call covers the whole day; an
      ``ok=False`` outcome makes the day unavailable;
    * otherwise ``suggest(ticker, asof)`` is called per proposed ticker, and an exception
      means no suggestion for that ticker (1.0), exactly as a guarded ticker reads live.

    A suggestion's multiplier is ``1 + multiplier_adjustment_pct / 100``; a 0.0 adjustment
    maps to exactly 1.0 so a neutral suggestion cannot perturb the replay by a rounding ulp.
    Scores are read from a suggestion's ``s_score`` or ``score`` attribute, and hedges from
    ``outcome.detail["tickers"][ticker]`` ``benchmark``/``beta`` when present.
    """

    def __init__(self, name: str, build: Callable[[PointInTimeView, Mapping[str, float]], object]) -> None:
        self.name = name
        self._build = build

    def decide(self, view: PointInTimeView, baseline_buys: Mapping[str, float]) -> CandidateDecision:
        suggester = self._build(view, baseline_buys)
        evaluate = getattr(suggester, "evaluate", None)
        if callable(evaluate):
            outcome = evaluate(view.asof)
            if not getattr(outcome, "ok", False):
                return CandidateDecision(available=False, reason=str(getattr(outcome, "reason", "not_ok")))
            suggestions = tuple(getattr(outcome, "suggestions", ()))
            detail = getattr(outcome, "detail", {}) or {}
        else:
            suggestions = []
            for ticker in sorted(baseline_buys):
                try:
                    suggestions.append(suggester.suggest(ticker, view.asof))
                except Exception:  # noqa: BLE001 -- a refused ticker reads as "no suggestion", as live
                    continue
            detail = {}
        multipliers: dict[str, float] = {}
        scores: dict[str, float] = {}
        hedges: dict[str, tuple[str, float]] = {}
        per_ticker = detail.get("tickers", {}) if isinstance(detail, Mapping) else {}
        for s in suggestions:
            ticker = s.ticker
            pct = _number(s.multiplier_adjustment_pct)
            if pct is None:
                raise ValueError(f"non-finite multiplier_adjustment_pct for {ticker}")
            multipliers[ticker] = 1.0 if pct == 0.0 else 1.0 + pct / 100.0
            score = _number(getattr(s, "s_score", getattr(s, "score", None)))
            if score is not None:
                scores[ticker] = score
            info = per_ticker.get(ticker) if isinstance(per_ticker, Mapping) else None
            if isinstance(info, Mapping):
                bench, beta = info.get("benchmark"), _number(info.get("beta"))
                if isinstance(bench, str) and bench and beta is not None:
                    hedges[ticker] = (bench, beta)
        return CandidateDecision(multipliers, scores, hedges)
