"""Black-Scholes closed forms, used as known-mean control variates and as test oracles.

A Black-Scholes value is a *risk-neutral* price. It is a valid control only in a risk-neutral
pricing run (paths simulated with mu == r). For real-world P&L of the cash-secured put shadow log,
use the terminal price (known mean under the model) as the control instead -- the volatility risk
premium is exactly the gap between the two measures.
"""

from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(spot: float, strike: float, rate: float, sigma: float, tenor: float) -> tuple[float, float]:
    for name, value in (("spot", spot), ("strike", strike), ("sigma", sigma), ("tenor", tenor)):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be positive and finite, got {value!r}")
    if not math.isfinite(rate):
        raise ValueError(f"rate must be finite, got {rate!r}")
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * tenor) / (sigma * math.sqrt(tenor))
    return d1, d1 - sigma * math.sqrt(tenor)


def black_scholes_call(spot: float, strike: float, rate: float, sigma: float, tenor: float) -> float:
    d1, d2 = _d1_d2(spot, strike, rate, sigma, tenor)
    return spot * _norm_cdf(d1) - strike * math.exp(-rate * tenor) * _norm_cdf(d2)


def black_scholes_put(spot: float, strike: float, rate: float, sigma: float, tenor: float) -> float:
    d1, d2 = _d1_d2(spot, strike, rate, sigma, tenor)
    return strike * math.exp(-rate * tenor) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
