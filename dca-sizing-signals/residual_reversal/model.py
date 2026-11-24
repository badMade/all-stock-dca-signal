"""Pure math for the residual-reversal suggester (docs/adr-residual-reversal-suggester.md).

Avellaneda and Lee (2010) style: regress a ticker's daily log returns on its sector ETF,
cumulate the residual, fit an Ornstein-Uhlenbeck process to that cumulated residual
through an AR(1) regression, and express where the residual sits now as an s-score in
units of the OU equilibrium standard deviation. A large negative s-score says the ticker
is residually cheap against its sector; a large positive one says residually rich.

Nothing here reads a clock, a file or the network, and nothing here knows a ticker
universe: every function takes plain floats and returns plain values or raises
ValueError. suggester.py owns the fail-closed wrapping. Stdlib only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True, slots=True)
class RegressionFit:
    """OLS of y on x with an intercept. ``residuals`` are y - alpha - beta * x."""

    alpha: float
    beta: float
    r_squared: float
    residuals: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class OUFit:
    """AR(1) fit X[k+1] = a + b X[k] + zeta of a cumulated residual series.

    kappa = -ln(b) * 252 is the annualised mean-reversion speed, m = a / (1 - b) the OU
    mean, sigma_eq = sqrt(var(zeta) / (1 - b^2)) the equilibrium standard deviation and
    half_life_sessions = ln 2 / -ln(b) the half-life in trading sessions. Only defined for
    0 < b < 1; fit_ou raises otherwise.
    """

    a: float
    b: float
    kappa: float
    m: float
    sigma_eq: float
    half_life_sessions: float
    last_x: float


def ols(x: Sequence[float], y: Sequence[float]) -> RegressionFit:
    """Ordinary least squares of ``y`` on ``x`` with an intercept.

    Raises:
        ValueError: fewer than 3 points, unequal lengths, a non-finite value, or an ``x``
            with no variance (beta undefined).
    """
    n = len(x)
    if n != len(y):
        raise ValueError(f"x has {n} points, y has {len(y)}")
    if n < 3:
        raise ValueError(f"need at least 3 points, got {n}")
    if not all(math.isfinite(v) for v in x) or not all(math.isfinite(v) for v in y):
        raise ValueError("non-finite value in regression input")
    mx = math.fsum(x) / n
    my = math.fsum(y) / n
    sxx = math.fsum((v - mx) ** 2 for v in x)
    if sxx <= 0.0:
        raise ValueError("x has no variance")
    sxy = math.fsum((xi - mx) * (yi - my) for xi, yi in zip(x, y, strict=True))
    beta = sxy / sxx
    alpha = my - beta * mx
    residuals = tuple(yi - alpha - beta * xi for xi, yi in zip(x, y, strict=True))
    syy = math.fsum((v - my) ** 2 for v in y)
    sse = math.fsum(r * r for r in residuals)
    # A y with no variance is fitted perfectly by the intercept alone; R^2 is 0 by
    # convention here so the low-R^2 guard treats it as "the ETF explains nothing".
    r_squared = 0.0 if syy <= 0.0 else max(0.0, 1.0 - sse / syy)
    return RegressionFit(alpha, beta, r_squared, residuals)


def cumulate(values: Sequence[float]) -> tuple[float, ...]:
    """Running sum X[k] = values[0] + ... + values[k]."""
    out: list[float] = []
    total = 0.0
    for v in values:
        total += v
        out.append(total)
    return tuple(out)


def fit_ou(x_series: Sequence[float]) -> OUFit:
    """Fit an OU process to a cumulated residual series through AR(1).

    Raises:
        ValueError: fewer than 10 points, a degenerate series, or b outside (0, 1) -- no
            mean reversion (b >= 1) or an oscillating fit (b <= 0) that the OU mapping
            cannot express. The caller turns either into a per-ticker guard.
    """
    if len(x_series) < 10:
        raise ValueError(f"need at least 10 cumulated residuals, got {len(x_series)}")
    fit = ols(x_series[:-1], x_series[1:])
    a, b = fit.alpha, fit.beta
    if not (0.0 < b < 1.0):
        raise ValueError(f"AR(1) coefficient b={b!r} is outside (0, 1)")
    n = len(fit.residuals)
    # Sample variance of the AR(1) innovations, two fitted parameters.
    var_zeta = math.fsum(r * r for r in fit.residuals) / (n - 2)
    if var_zeta <= 0.0:
        raise ValueError("AR(1) innovations have no variance")
    sigma_eq = math.sqrt(var_zeta / (1.0 - b * b))
    log_b = math.log(b)
    return OUFit(
        a=a,
        b=b,
        kappa=-log_b * TRADING_DAYS_PER_YEAR,
        m=a / (1.0 - b),
        sigma_eq=sigma_eq,
        half_life_sessions=math.log(2.0) / -log_b,
        last_x=float(x_series[-1]),
    )


def s_score(fit: OUFit, mean_shift: float = 0.0) -> float:
    """(X_now - (m - mean_shift)) / sigma_eq.

    ``mean_shift`` is the cross-sectional average OU mean that Avellaneda and Lee subtract
    when centring is on (suggester.py computes it); 0.0 means no centring.
    """
    return (fit.last_x - (fit.m - mean_shift)) / fit.sigma_eq


def raw_multiplier(s: float, entry: float, slope: float, lo: float, hi: float) -> float:
    """Per-ticker tilt before budget neutrality.

    Inside the no-action band (|s| < entry) the answer is exactly 1.0. Outside it,
    clip(1 - slope * s, lo, hi): residually cheap (s <= -entry) leans up, residually rich
    (s >= +entry) leans down. The step at |s| = entry is deliberate -- it is the
    Avellaneda-Lee open threshold, not a tuned parameter.
    """
    if not math.isfinite(s):
        raise ValueError(f"s-score must be finite, got {s!r}")
    if abs(s) < entry:
        return 1.0
    return min(hi, max(lo, 1.0 - slope * s))


def budget_neutral(
    raw: Mapping[str, float],
    weights: Mapping[str, float],
    lo: float,
    hi: float,
    tolerance: float = 1e-9,
) -> dict[str, float]:
    """Shift every multiplier by one common amount so the day's dollars are unchanged.

    Finds c with sum_i w_i * clip(raw_i - c, lo, hi) == sum_i w_i. The left side is
    continuous and non-increasing in c, at least sum w at c = min(raw) - hi and at most it
    at c = max(raw) - lo (because lo <= 1 <= hi), so a bisection always converges. The
    shift touches every ticker in ``raw`` -- names in the no-action band fund (or absorb)
    the net tilt of the names outside it -- and the clip keeps every result in [lo, hi].

    Args:
        raw: ticker -> raw multiplier (each in [lo, hi]).
        weights: ticker -> proposed dollars (> 0). Same keys as ``raw``.
        lo, hi: bounds with lo <= 1 <= hi.
        tolerance: relative error allowed on the dollar total.

    Raises:
        ValueError: mismatched keys, a non-positive weight, bad bounds, or a result that
            misses the total by more than ``tolerance`` (never expected; a self-check).
    """
    if set(raw) != set(weights):
        raise ValueError("raw multipliers and weights must cover the same tickers")
    if not (0.0 < lo <= 1.0 <= hi):
        raise ValueError(f"bounds must satisfy 0 < lo <= 1 <= hi, got lo={lo}, hi={hi}")
    if not raw:
        return {}
    if any(not (math.isfinite(w) and w > 0.0) for w in weights.values()):
        raise ValueError("every weight must be finite and positive")
    total = math.fsum(weights.values())

    def dollars(c: float) -> float:
        return math.fsum(weights[t] * min(hi, max(lo, m - c)) for t, m in raw.items())

    low_c, high_c = min(raw.values()) - hi, max(raw.values()) - lo
    for _ in range(200):
        mid = (low_c + high_c) / 2.0
        if dollars(mid) > total:
            low_c = mid
        else:
            high_c = mid
    c = (low_c + high_c) / 2.0
    result = {t: min(hi, max(lo, m - c)) for t, m in raw.items()}
    # Snap an all-neutral day back to exact 1.0s rather than 1.0 +/- 1e-16.
    if all(m == 1.0 for m in raw.values()):
        result = dict.fromkeys(raw, 1.0)
    achieved = math.fsum(weights[t] * m for t, m in result.items())
    if abs(achieved - total) > tolerance * total:
        raise ValueError(f"budget-neutral projection missed: {achieved} vs {total}")
    return result
