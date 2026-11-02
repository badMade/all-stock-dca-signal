"""Signal 4, cheaper-leg tilt -- SPECULATIVE, SHADOW ONLY.

docs/adr-comovement-signals.md, open question 7 (proposed default): within a close
substitute pair (daily-return correlation of at least 0.75), record how the pair's
combined DCA dollars WOULD be split if they leaned toward the leg that is cheap on the
spread z-score:

    weight_A = clip(0.5 - 0.15 * z, 0.2, 0.8),   weight_B = 1 - weight_A

Why "speculative": the 2026-11-02 research found no pair in the universe that passed a
multiple-testing-corrected cointegration test, STX/WDC sat below its formation mean for
six months, and no peer-reviewed long-only evidence was found (report: 30% confidence it
adds value). So every record is labelled ``speculative_shadow_only``, nothing reads the
file, and this module is not a SizingSuggester.

Method, and how lookahead is kept out:

  * Leg A is the alphabetically first ticker; the spread is log(A) - c - beta * log(B).
  * The evaluation session is the pair's newest shared close (on or before ``as_of``;
    rows after it are dropped by returns.py before anything is read). The FORMATION
    window is the ``formation_sessions`` shared closes strictly before it. The hedge
    ratio beta, intercept c, residual mean and residual sd come from OLS over the
    formation window only, then are frozen and applied to the evaluation close, the
    research's "freeze hedge ratio and mean from formation" rule. History older than the
    formation window is never read.
  * The correlation filter uses the daily log returns inside the formation window.
  * Fail closed per pair (``ShadowResult.skipped``): too little formation history, a
    stale leg, a degenerate fit (no variance, or a perfect fit with no residual spread to
    z-score) and a non-positive hedge ratio (legs that move apart are not substitutes for
    a buy-only tilt) all give no record. Malformed or stale input fails the whole run.

Pure: stdlib only; reuses returns_matrix (via shadow.fresh_matrix) and pearson.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from comovement_signals.clustering import pearson
from comovement_signals.returns import ComovementInputError, _clean_rows
from comovement_signals.shadow import (
    DEFAULT_MAX_PRICE_STALENESS_DAYS,
    SIGNAL_CHEAPER_LEG_TILT,
    ShadowRecord,
    ShadowResult,
    ShadowUnavailable,
    finite,
    fresh_matrix,
    pair_key,
)

LABEL = "speculative_shadow_only"
# returns_matrix keeps the newest N sessions; the tilt picks its own formation dates per
# pair, so it asks for every session and slices afterwards.
_ALL_SESSIONS = 1_000_000
# A residual sd this small relative to the price scale is a perfect fit: there is no
# spread to z-score, and dividing by round-off would invent a huge z.
_DEGENERATE_SD = 1e-12


@dataclass(frozen=True)
class TiltConfig:
    """Thresholds, defaulting to the ADR's proposed values. ``formation_sessions`` 252 is
    the ADR's one-trading-year correlation window."""

    formation_sessions: int = 252
    min_pair_correlation: float = 0.75
    min_overlap_sessions: int = 120
    center: float = 0.5
    slope: float = 0.15
    lower: float = 0.2
    upper: float = 0.8
    max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS


@dataclass(frozen=True)
class HedgeFit:
    beta: float
    intercept: float
    residual_mean: float
    residual_sd: float  # sample sd (n - 1)


def tilt_weight(z: float, center: float = 0.5, slope: float = 0.15, lower: float = 0.2, upper: float = 0.8) -> float:
    """Leg A's share of the pair's dollars: clip(center - slope * z, lower, upper).

    Raises:
        ValueError: on a non-finite z.
    """
    if not finite(z):
        raise ValueError(f"z must be finite, got {z!r}")
    return min(upper, max(lower, center - slope * z))


def ols_hedge(xs: Sequence[float], ys: Sequence[float]) -> HedgeFit | None:
    """OLS of y on x with an intercept. None when x has no variance, there are fewer
    than 3 points, or the fit is perfect (no residual spread)."""
    n = len(xs)
    if n != len(ys):
        raise ValueError("ols_hedge() needs equal-length sequences")
    if n < 3:
        return None
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    if sxx == 0.0:
        return None
    beta = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - beta * mx
    resid = [y - intercept - beta * x for x, y in zip(xs, ys, strict=True)]
    rmean = math.fsum(resid) / n
    sd = math.sqrt(math.fsum((e - rmean) ** 2 for e in resid) / (n - 1))
    scale = max(1.0, max(abs(y) for y in ys))
    if sd <= _DEGENERATE_SD * scale:
        return None
    return HedgeFit(beta=beta, intercept=intercept, residual_mean=rmean, residual_sd=sd)


def cheaper_leg_tilt_shadow(
    close_rows: Iterable[Mapping[str, object]],
    as_of: date,
    config: TiltConfig | None = None,
    pairs: Iterable[tuple[str, str]] | None = None,
    pair_dollars: Mapping[tuple[str, str], float] | None = None,
) -> ShadowResult:
    """Shadow tilt weights for every qualifying pair as of ``as_of``.

    Args:
        close_rows: daily close rows (shape as returns.returns_matrix).
        as_of: the as-of date; nothing dated later is read.
        config: thresholds.
        pairs: optionally the candidate pairs (either order); default every pair of
            tickers with prices. An explicitly named pair that misses the correlation
            filter is reported in ``skipped``; an implicit one is simply not a pair.
        pair_dollars: optionally the pair's combined DCA dollars, keyed by the pair in
            either order, to also record the shadow dollar split. None records weights
            only.
    """
    config = config if config is not None else TiltConfig()
    rows = list(close_rows)
    try:
        matrix = fresh_matrix(rows, as_of, _ALL_SESSIONS, 1, config.max_price_staleness_days)
        closes = _clean_rows(rows, as_of)
        dollars = _normalise_dollars(pair_dollars or {})
    except ShadowUnavailable as failed:
        return ShadowResult(SIGNAL_CHEAPER_LEG_TILT, as_of, False, failed.reason)
    except ComovementInputError:
        return ShadowResult(SIGNAL_CHEAPER_LEG_TILT, as_of, False, "invalid_input")

    explicit = pairs is not None
    if explicit:
        candidates = sorted({tuple(sorted(p)) for p in pairs})
    else:
        names = sorted(closes)
        candidates = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]

    records: list[ShadowRecord] = []
    skipped: dict[str, str] = {}
    for a, b in candidates:
        outcome = _evaluate_pair(a, b, closes, matrix.returns, matrix.latest_price_date, config)
        if isinstance(outcome, str):
            if outcome != "below_min_correlation" or explicit:
                skipped[pair_key(a, b)] = outcome
            continue
        payload = outcome
        usd = dollars.get((a, b))
        if usd is not None:
            payload["combined_usd"] = usd
            payload["shadow_usd_a"] = usd * payload["weight_a"]
            payload["shadow_usd_b"] = usd * payload["weight_b"]
        records.append(ShadowRecord(SIGNAL_CHEAPER_LEG_TILT, as_of,
                                    date.fromisoformat(payload["evaluation_session"]), payload))
    return ShadowResult(SIGNAL_CHEAPER_LEG_TILT, as_of, True, "ok", tuple(records), skipped)


def _normalise_dollars(pair_dollars: Mapping[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for pair, usd in pair_dollars.items():
        if not finite(usd) or usd < 0:
            raise ComovementInputError(f"pair dollars must be finite and non-negative: {pair} -> {usd!r}")
        out[tuple(sorted(pair))] = float(usd)
    return out


def _evaluate_pair(a, b, closes, returns, latest, config: TiltConfig) -> dict | str:
    """The payload for one pair, or the fail-closed reason string."""
    if a not in closes or b not in closes:
        return "missing_ticker"
    ca, cb = closes[a], closes[b]
    shared = sorted(set(ca) & set(cb))
    if not shared or shared[-1] != latest:
        return "stale_pair"
    evaluation = shared[-1]
    formation = shared[:-1][-config.formation_sessions:]
    if len(formation) < config.formation_sessions:
        return "insufficient_history"

    ra, rb = returns.get(a, {}), returns.get(b, {})
    corr_dates = [d for d in formation[1:] if d in ra and d in rb]
    if len(corr_dates) < config.min_overlap_sessions:
        return "insufficient_history"
    rho = pearson([ra[d] for d in corr_dates], [rb[d] for d in corr_dates])
    if rho is None:
        return "degenerate_spread"
    if rho < config.min_pair_correlation:
        return "below_min_correlation"

    fit = ols_hedge([math.log(cb[d]) for d in formation], [math.log(ca[d]) for d in formation])
    if fit is None:
        return "degenerate_spread"
    if fit.beta <= 0.0:
        return "non_positive_hedge_ratio"
    spread = math.log(ca[evaluation]) - fit.intercept - fit.beta * math.log(cb[evaluation])
    z = (spread - fit.residual_mean) / fit.residual_sd
    weight_a = tilt_weight(z, config.center, config.slope, config.lower, config.upper)
    return {
        "pair": [a, b],
        "leg_a": a,
        "leg_b": b,
        "correlation": rho,
        "formation_start": formation[0].isoformat(),
        "formation_end": formation[-1].isoformat(),
        "formation_sessions": len(formation),
        "evaluation_session": evaluation.isoformat(),
        "hedge_ratio": fit.beta,
        "intercept": fit.intercept,
        "residual_sd": fit.residual_sd,
        "spread": spread,
        "z": z,
        "weight_a": weight_a,
        "weight_b": 1.0 - weight_a,
        "label": LABEL,
        "live_effect": "none",
    }
