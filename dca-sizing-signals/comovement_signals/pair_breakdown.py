"""Signal 3, pair breakdown warning -- SHADOW ONLY, report only.

docs/adr-comovement-signals.md, open question 6 (proposed default): for a pair whose
2-year daily-return correlation is at least 0.70, flag a breakdown when its rolling
60-session correlation sits below that pair's own 2-year 10th percentile for 5
consecutive sessions. A flagged record suggests both names hold at 1x (no dip
escalation). The suggestion has NO live effect: nothing reads the shadow file, and this
module is not a SizingSuggester. Scoring it in the Phase 0 ledger comes first.

Design choices worth knowing (each is a default, all are in PairBreakdownConfig):

  * Point in time. Every input comes through returns_matrix(), which drops any row dated
    after ``as_of`` before anything else sees it. The 2-year correlation, the rolling
    correlations and the percentile all use only those returns.
  * The 10th percentile is computed over the pair's rolling correlations in the 2-year
    window EXCLUDING the sessions being tested, so the breakdown under test does not
    vote on (and drag down) its own threshold. One threshold, as of today, is applied to
    each of the last 5 sessions.
  * "Below" is strict, with a 1e-12 float slack: a correlation exactly on the percentile
    does not count. Without the slack two numerically identical windows could differ by
    one ulp and flip the comparison.
  * Percentiles use linear interpolation between order statistics (numpy's default).
  * Fail closed. Malformed or stale input: the whole run fails with a reason and no
    record. Per pair: too little shared history or reference, or a leg whose newest
    return is not the newest session, gives no record for that pair and a reason in
    ``ShadowResult.skipped``.

Pure: stdlib only, reuses pearson / pairwise_correlations from clustering.py.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from comovement_signals.clustering import pairwise_correlations, pearson
from comovement_signals.shadow import (
    DEFAULT_MAX_PRICE_STALENESS_DAYS,
    SIGNAL_PAIR_BREAKDOWN,
    ShadowRecord,
    ShadowResult,
    ShadowUnavailable,
    finite,
    fresh_matrix,
    pair_key,
)

HOLD_1X = "hold_1x"
# Float slack for the strict "below the percentile" comparison only (see module doc).
_CMP_EPS = 1e-12


@dataclass(frozen=True)
class PairBreakdownConfig:
    """Thresholds, defaulting to the ADR's proposed values.

    ``lookback_sessions`` 504 is about two trading years. ``min_history_sessions`` is the
    shared-return minimum for a pair's 2-year correlation (one trading year), and
    ``min_reference_values`` the minimum number of rolling correlations behind the
    percentile; with the defaults a pair with full history has about 440.
    """

    lookback_sessions: int = 504
    min_pair_correlation: float = 0.70
    rolling_sessions: int = 60
    percentile: float = 0.10
    consecutive_sessions: int = 5
    min_history_sessions: int = 252
    min_reference_values: int = 100
    max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS


@dataclass(frozen=True)
class BreakdownCheck:
    threshold: float
    run_length: int
    flagged: bool
    reference_count: int


def percentile(values: Iterable[float], q: float) -> float:
    """Linear-interpolation percentile: position q * (n - 1) in the sorted values.

    Raises:
        ValueError: on an empty or non-finite input, or q outside [0, 1].
    """
    data = sorted(values)
    if not data:
        raise ValueError("percentile() of no values")
    if not (0.0 <= q <= 1.0):
        raise ValueError(f"q must be in [0, 1], got {q}")
    if not all(finite(v) for v in data):
        raise ValueError("percentile() needs finite values")
    pos = q * (len(data) - 1)
    lo = math.floor(pos)
    frac = pos - lo
    if frac == 0.0:
        return data[lo]
    return data[lo] + (data[lo + 1] - data[lo]) * frac


def breakdown_run(
    rolling_values: Sequence[float],
    consecutive: int = 5,
    q: float = 0.10,
    min_reference: int = 1,
) -> BreakdownCheck | None:
    """Test the last ``consecutive`` rolling correlations against the percentile of the
    ones before them.

    Returns:
        A BreakdownCheck, or None when fewer than ``min_reference`` (or zero) values
        precede the tested run. ``run_length`` counts the newest values strictly below
        the threshold, up to ``consecutive``; ``flagged`` means all of them are.
    """
    if consecutive < 1:
        raise ValueError("consecutive must be at least 1")
    reference = list(rolling_values[:-consecutive])
    tested = list(rolling_values[-consecutive:])
    if len(tested) < consecutive or not reference or len(reference) < min_reference:
        return None
    threshold = percentile(reference, q)
    run = 0
    for value in reversed(tested):
        if value < threshold - _CMP_EPS:
            run += 1
        else:
            break
    return BreakdownCheck(threshold=threshold, run_length=run, flagged=run >= consecutive,
                          reference_count=len(reference))


def rolling_correlations(
    returns_a: Mapping[date, float],
    returns_b: Mapping[date, float],
    window: int,
) -> list[tuple[date, float]]:
    """Pearson correlation over each run of ``window`` consecutive shared sessions,
    dated by the window's last session. A window with an undefined correlation (a flat
    series) is left out rather than given a made-up value."""
    shared = sorted(set(returns_a) & set(returns_b))
    out: list[tuple[date, float]] = []
    for end in range(window, len(shared) + 1):
        dates = shared[end - window:end]
        r = pearson([returns_a[d] for d in dates], [returns_b[d] for d in dates])
        if r is not None:
            out.append((dates[-1], r))
    return out


def pair_breakdown_shadow(
    close_rows: Iterable[Mapping[str, object]],
    as_of: date,
    config: PairBreakdownConfig | None = None,
    tickers: Iterable[str] | None = None,
) -> ShadowResult:
    """Evaluate every eligible pair as of ``as_of`` and return shadow records.

    Args:
        close_rows: daily close rows (shape as returns.returns_matrix).
        as_of: the as-of date; nothing dated later is read, and a bar dated on it only
            when the row is marked ``final`` (returns.py).
        config: thresholds; None means the ADR defaults.
        tickers: optionally restrict the universe; default every ticker with enough
            history.

    Returns:
        A ShadowResult. Each evaluated pair (2-year correlation >= the minimum) gets one
        record, flagged or not, so the ledger sees the negatives too.
    """
    config = config if config is not None else PairBreakdownConfig()
    try:
        matrix = fresh_matrix(close_rows, as_of, config.lookback_sessions, config.min_history_sessions,
                              config.max_price_staleness_days)
    except ShadowUnavailable as failed:
        return ShadowResult(SIGNAL_PAIR_BREAKDOWN, as_of, False, failed.reason)

    wanted = None if tickers is None else set(tickers)
    universe = matrix.eligible if wanted is None else tuple(t for t in matrix.eligible if t in wanted)
    newest = matrix.sessions[-1]
    corr = pairwise_correlations(matrix.returns, universe, min_overlap=config.min_history_sessions)
    records: list[ShadowRecord] = []
    skipped: dict[str, str] = {}
    for (a, b), rho in sorted(corr.items()):
        if rho is None or rho < config.min_pair_correlation:
            continue  # not a "normally tight" pair: nothing to break down
        ra, rb = matrix.returns[a], matrix.returns[b]
        shared = set(ra) & set(rb)
        if max(shared) != newest:
            skipped[pair_key(a, b)] = "stale_pair"
            continue
        rolling = rolling_correlations(ra, rb, config.rolling_sessions)
        # Copilot review on PR #2106: rolling_correlations skips sessions with no shared
        # return or an undefined correlation, so its last N values can span a gap. The
        # "N consecutive sessions" trigger needs a value for each of the last N matrix
        # sessions; anything else is skipped with a reason, never flagged across the gap.
        recent_dates = [d for d, _ in rolling[-config.consecutive_sessions:]]
        if len(rolling) >= config.consecutive_sessions and recent_dates != list(
                matrix.sessions[-config.consecutive_sessions:]):
            skipped[pair_key(a, b)] = "gap_in_recent_sessions"
            continue
        check = breakdown_run([r for _, r in rolling], config.consecutive_sessions, config.percentile,
                              config.min_reference_values)
        if check is None or rolling[-1][0] != newest:
            skipped[pair_key(a, b)] = "insufficient_history"
            continue
        recent = rolling[-config.consecutive_sessions:]
        records.append(ShadowRecord(
            signal=SIGNAL_PAIR_BREAKDOWN,
            as_of=as_of,
            inputs_known_at=newest,
            payload={
                "pair": [a, b],
                "correlation_2y": rho,
                "shared_returns_2y": len(shared),
                "rolling_sessions": config.rolling_sessions,
                "rolling_correlation_recent": [r for _, r in recent],
                "rolling_dates_recent": [d.isoformat() for d, _ in recent],
                "percentile": config.percentile,
                "threshold": check.threshold,
                "reference_count": check.reference_count,
                "consecutive_sessions": config.consecutive_sessions,
                "run_length": check.run_length,
                "breakdown": check.flagged,
                "suggestion": {a: HOLD_1X, b: HOLD_1X} if check.flagged else None,
                "live_effect": "none",
            },
        ))
    return ShadowResult(SIGNAL_PAIR_BREAKDOWN, as_of, True, "ok", tuple(records), skipped)
