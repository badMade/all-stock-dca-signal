"""Combine per-signal raw scores into one ranked composite per candidate.

No third-party dependency at all -- pure stdlib (``statistics``/``math``) so
this module (and its tests) never need any of the heavy signal-specific
packages installed.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping, TypeGuard

# Exactly config/tickers.json's `active`/`excluded` schema pattern
# (schemas/tickers.schema.json). Shared between advise_tickers.py (filters
# candidates BEFORE they reach combine_scores(), so a schema-incompatible
# symbol like "BRK-B" can't skew other candidates' cross-candidate z-scores)
# and report.py (a second, defense-in-depth check right before a proposed
# diff is emitted) -- living here since both already import from this
# module (Codex review on PR #403: "Validate symbols before cross-candidate
# normalization").
TICKER_SYMBOL_PATTERN = re.compile(r"^[A-Z.]{1,6}$")

DEFAULT_WEIGHTS: dict[str, float] = {"health": 1.0, "backtest": 1.0, "sentiment": 1.0}

# Composite score is a cross-candidate z-score average; these bands are in
# standard-deviation units, not raw metric units. Falls through to REJECT
# below the WATCH threshold.
CLASSIFICATION_BANDS: tuple[tuple[float, str], ...] = ((0.5, "ADD"), (-0.5, "WATCH"))

_HEALTH_COMPONENTS = ("sharpe", "calmar")
_BACKTEST_STRATEGIES = ("ma_crossover", "rsi_reversion")


@dataclass
class TickerScore:
    """One candidate's scoring result from combine_scores().

    Attributes:
        ticker: The candidate symbol.
        raw: The unmodified per-signal input this score was built from --
            score_candidate()'s ``{"health": ..., "backtest": ...,
            "sentiment": ..., "errors": [...]}`` dict, in the original
            metric units (Sharpe, Calmar, a -1..1 sentiment score, etc.).
        sub_scores: Per-signal ``{"health"/"backtest"/"sentiment": z}``,
            each a cross-candidate z-score in standard-deviation units (NOT
            the same units as ``raw``) -- ``None`` when that signal was
            unavailable or too sparse across the batch to normalize.
        composite: The weighted average of the available sub_scores, or
            ``None`` if none were available (see ``rationale``).
        classification: ``"ADD"``, ``"WATCH"``, or ``"REJECT"`` from
            ``classify(composite)``.
        rationale: Human-readable notes explaining any excluded signal or
            propagated error -- what a reader of scores.md needs to
            understand why a signal is missing, not just that it is.
    """

    ticker: str
    raw: Mapping[str, Any]
    sub_scores: dict[str, float | None]
    composite: float | None
    classification: str
    rationale: list[str] = field(default_factory=list)


def _is_usable(value: object) -> TypeGuard[float | int]:
    # math.isfinite rejects NaN AND +/-inf -- a zero-variance/zero-drawdown
    # edge case can produce an infinite Sharpe/Calmar, and an infinite value
    # reaching statistics.pstdev() would corrupt or crash normalization for
    # every OTHER candidate in the batch too (Codex review on PR #403:
    # "Reject infinite signal components").
    return isinstance(value, (int, float)) and math.isfinite(value)


def _extract_health_component(health: object, component: str) -> float | None:
    if not isinstance(health, dict):
        return None
    value = health.get(component)
    if _is_usable(value):
        return float(value)
    return None


def _extract_backtest_component(backtest: object, strategy: str) -> float | None:
    if not isinstance(backtest, dict):
        return None
    metrics = backtest.get(strategy)
    if not isinstance(metrics, dict):
        return None
    value = metrics.get("sharpe")
    if _is_usable(value):
        return float(value)
    return None


def _extract_sentiment_component(sentiment: object) -> float | None:
    if not isinstance(sentiment, dict):
        return None
    value = sentiment.get("score")
    if _is_usable(value):
        return float(value)
    return None


def _zscore(values_by_ticker: dict[str, float | None]) -> dict[str, float | None]:
    """Compute standard-deviation units for every candidate with a usable value.

    Returns ``None`` for any candidate whose value was missing/NaN. Returns
    ``None`` for EVERY candidate when fewer than 2 candidates have a usable
    value: a single observation has zero sample variance and cannot be
    meaningfully compared against peers (Antigravity review on PR #403:
    "Cross-candidate normalization on single observation").
    """
    usable = [value for value in values_by_ticker.values() if value is not None]
    if len(usable) < 2:
        return {ticker: None for ticker in values_by_ticker}
    mean = statistics.fmean(usable)
    stdev = statistics.pstdev(usable)
    # A degenerate batch where every candidate has the EXACT same metric
    # value has stdev == 0.0 -- returning None for every candidate here
    # would incorrectly report the signal as "unavailable" when it is
    # actually identical; returning 0.0 gives each candidate a neutral z-score.
    # Checked with a small epsilon: floating-point cancellation in pstdev()
    # can leave a tiny positive residue (e.g. 1e-17) for identical inputs, and
    # dividing by that micro-epsilon produces artificially huge (>1000)
    # z-scores instead of the intended "no real spread" 0.0 (Antigravity
    # review on PR #403: "Apply Standard Deviation Epsilon Threshold").
    if stdev < 1e-12:
        return {ticker: (0.0 if value is not None else None) for ticker, value in values_by_ticker.items()}
    return {
        ticker: ((value - mean) / stdev if value is not None else None) for ticker, value in values_by_ticker.items()
    }


def _blend(values: list[float | None]) -> float | None:
    usable = [value for value in values if value is not None]
    return statistics.fmean(usable) if usable else None


def classify(composite: float | None) -> str:
    """WATCH (never a silent ADD/REJECT) when there's no composite to classify at all."""
    if composite is None:
        return "WATCH"
    for threshold, label in CLASSIFICATION_BANDS:
        if composite >= threshold:
            return label
    return "REJECT"


def combine_scores(
    per_ticker_raw_scores: Mapping[str, Mapping[str, Any]],
    *,
    weights: Mapping[str, float] | None = None,
) -> dict[str, TickerScore]:
    """Combine each candidate's raw ``health``/``backtest``/``sentiment`` signals.

    Every individual raw metric (health's Sharpe and Calmar; backtest's
    MA-crossover and RSI-reversion Sharpe) is z-scored independently across
    the batch *before* being blended into that signal's sub-score --
    blending first and z-scoring the blend would let whichever raw metric
    happens to have the larger natural scale (Calmar routinely runs
    5.0-20.0+, Sharpe usually 0.0-2.0) dominate the signal regardless of
    which one actually says more about the candidate (Antigravity review on
    PR #403: "Heterogeneous metric blend dominated by scale differences").

    A candidate missing a signal (skipped sentiment, a NaN/inf metric, or a
    metric too sparse across the batch to normalize) is scored on whichever
    signals it does have -- never defaulted to a neutral 0, which would
    silently reward "no data" the same as "average data." That gap is
    recorded in ``TickerScore.rationale`` instead.
    """
    active_weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    tickers = list(per_ticker_raw_scores)

    health_components = {
        component: {t: _extract_health_component(per_ticker_raw_scores[t].get("health"), component) for t in tickers}
        for component in _HEALTH_COMPONENTS
    }
    backtest_components = {
        strategy: {
            t: _extract_backtest_component(per_ticker_raw_scores[t].get("backtest"), strategy) for t in tickers
        }
        for strategy in _BACKTEST_STRATEGIES
    }
    sentiment_raw = {t: _extract_sentiment_component(per_ticker_raw_scores[t].get("sentiment")) for t in tickers}

    health_z = {component: _zscore(values) for component, values in health_components.items()}
    backtest_z = {strategy: _zscore(values) for strategy, values in backtest_components.items()}
    sentiment_z = _zscore(sentiment_raw)

    results: dict[str, TickerScore] = {}
    for ticker in tickers:
        raw = per_ticker_raw_scores[ticker]
        had_raw = {
            "health": any(health_components[c][ticker] is not None for c in _HEALTH_COMPONENTS),
            "backtest": any(backtest_components[s][ticker] is not None for s in _BACKTEST_STRATEGIES),
            "sentiment": sentiment_raw[ticker] is not None,
        }
        sub_scores: dict[str, float | None] = {
            "health": _blend([health_z[c][ticker] for c in _HEALTH_COMPONENTS]),
            "backtest": _blend([backtest_z[s][ticker] for s in _BACKTEST_STRATEGIES]),
            "sentiment": sentiment_z[ticker],
        }

        rationale: list[str] = []
        # _blend() silently averages whichever components ARE usable --
        # when e.g. Calmar is NaN (zero drawdown) for this candidate or
        # too sparse across the batch to normalize while Sharpe is fine,
        # sub_scores["health"] comes back non-None from Sharpe alone, so
        # the `value is None` branch below never fires and the omission
        # goes unrecorded. scores.md documents health/backtest as a blend
        # of both named components, so a reader (and a resulting ADD
        # recommendation) can't tell this candidate's sub-score reflects
        # only half of it (Codex review on PR #403: "Record partially
        # excluded signal components").
        for signal_name, components, raw_by_component, component_z in (
            ("health", _HEALTH_COMPONENTS, health_components, health_z),
            ("backtest", _BACKTEST_STRATEGIES, backtest_components, backtest_z),
        ):
            used = [c for c in components if component_z[c][ticker] is not None]
            missing = [c for c in components if c not in used]
            if used and missing:
                # A component's z-score is None for one of two DIFFERENT
                # reasons -- conflating them made the saved explanation
                # wrong: (a) batch-wide, fewer than 2 candidates anywhere
                # have a usable raw value, so _zscore() excludes it for
                # EVERY candidate; or (b) candidate-specific, this
                # candidate's own raw value is missing/NaN (e.g. zero
                # drawdown makes Calmar NaN) while enough OTHER candidates
                # have one for it to normalize fine among them (Codex
                # review on PR #403: "Report the actual reason a component
                # was excluded").
                missing_reasons = []
                for component in missing:
                    usable_count = sum(1 for t in tickers if raw_by_component[component][t] is not None)
                    if usable_count < 2:
                        missing_reasons.append(
                            f"{component} (fewer than 2 candidates in this batch have a usable value)"
                        )
                    else:
                        missing_reasons.append(f"{component} (no usable value for this candidate)")
                rationale.append(
                    f"{signal_name} sub-score computed from {', '.join(used)} only -- "
                    f"{'; '.join(missing_reasons)} could not be normalized"
                )
        weighted_sum = 0.0
        weight_total = 0.0
        for signal_name, value in sub_scores.items():
            if value is None:
                if had_raw[signal_name]:
                    rationale.append(
                        f"{signal_name} signal present but excluded -- fewer than 2 candidates in this batch "
                        f"have a usable {signal_name} value, so it can't be normalized against peers"
                    )
                else:
                    rationale.append(
                        f"{signal_name} signal unavailable -- excluded from composite, not scored as neutral"
                    )
                continue
            weighted_sum += value * active_weights.get(signal_name, 0.0)
            weight_total += active_weights.get(signal_name, 0.0)
        composite = weighted_sum / weight_total if weight_total > 0 else None
        if composite is None:
            rationale.append("no usable signals at all -- cannot classify; defaulting to WATCH for manual follow-up")
        # score_candidate() (advise_tickers.py) records why a signal failed
        # (a network error, a computation exception) in raw["errors"], but
        # that detail previously only reached scores.json -- a reader of
        # scores.md saw "backtest signal unavailable" with no way to tell
        # whether that meant --skip-sentiment, a missing dependency, or an
        # actual exception without separately opening the raw JSON
        # (Antigravity review on PR #403: "Exception Failure Reasons Hidden
        # in Markdown Report").
        errors = raw.get("errors")
        if isinstance(errors, list):
            rationale.extend(f"error: {message}" for message in errors)
        results[ticker] = TickerScore(
            ticker=ticker,
            raw=dict(raw),
            sub_scores=sub_scores,
            composite=composite,
            classification=classify(composite),
            rationale=rationale,
        )
    return results


def rank(scores: dict[str, TickerScore]) -> list[TickerScore]:
    """Highest composite first; candidates with no composite sort last."""
    return sorted(scores.values(), key=lambda s: (s.composite is None, -(s.composite or 0.0)))


__all__ = [
    "DEFAULT_WEIGHTS",
    "CLASSIFICATION_BANDS",
    "TickerScore",
    "classify",
    "combine_scores",
    "rank",
]
