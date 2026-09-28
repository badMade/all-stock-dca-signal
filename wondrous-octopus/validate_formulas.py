"""Independent validation of the calculus features and the ensemble blend.

Deliberately re-derives every formula a DIFFERENT way from features.py /
stacking.py, then compares. A bug shared by both would have to be made twice,
independently, in two different algorithms -- which is the point.

Run:  python validate_formulas.py            (from wondrous-octopus/)
Exit code 0 = every check passed.
"""
from __future__ import annotations

import math
import sys
from typing import Any

import data
import features as _features
import numpy as np
import pandas as pd
import stacking

features: Any = _features

FAILURES: list[str] = []


def check(label, got, expected, *, tol, note=""):
    got_a, exp_a = np.asarray(got, dtype=float), np.asarray(expected, dtype=float)
    if got_a.shape != exp_a.shape:
        FAILURES.append(label)
        print(f"  FAIL  {label:52s} shape {got_a.shape} vs {exp_a.shape}")
        return
    both = np.isfinite(got_a) & np.isfinite(exp_a)
    dev = float(np.max(np.abs(got_a[both] - exp_a[both]))) if both.any() else 0.0
    nan_match = np.array_equal(np.isfinite(got_a), np.isfinite(exp_a))
    ok = dev <= tol and nan_match
    if not ok:
        FAILURES.append(label)
    print(f"  {'pass' if ok else 'FAIL':4s}  {label:52s} max|dev| = {dev:.3e}  (tol {tol:.0e})"
          f"{'' if nan_match else '  NaN POSITIONS DIFFER'}{('  ' + note) if note else ''}")


def synthetic_frame(n=400, seed=7):
    rng = np.random.default_rng(seed)
    close = 50.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.018, n)))
    high = close * (1 + rng.uniform(0, 0.01, n))
    low = close * (1 - rng.uniform(0, 0.01, n))
    frame = pd.DataFrame({
        "Date": pd.bdate_range("2020-01-01", periods=n),
        "Open": close, "High": high, "Low": low, "Close": close, "Adj Close": close,
        "Dividends": 0.0, "Stock Splits": 0.0, "Volume": rng.integers(100_000, 5_000_000, n),
    })
    adjusted, _ = data.adjusted_ohlc(frame)
    return adjusted


def main():
    global FAILURES
    FAILURES.clear()
    print("=" * 86)
    print("1. CALCULUS FEATURES vs independent re-derivations")
    print("=" * 86)

    frame = synthetic_frame()
    out = features.compute_ticker_features(frame, extra_features=("calculus",))
    close = frame["Close"].to_numpy(dtype=float)
    logp = np.log(close)
    n = len(close)

    # -- ret_accel: plain Python loop over the definition, no pandas ------------
    manual_accel = np.full(n, np.nan)
    for t in range(2, n):
        manual_accel[t] = (logp[t] - logp[t - 1]) - (logp[t - 1] - logp[t - 2])
    check("ret_accel  vs hand-written second difference", out["ret_accel"], manual_accel, tol=1e-12)

    # -- vol20_roc: manual rolling std (ddof=1, as pandas uses) -----------------
    rets = np.concatenate([[np.nan], np.diff(logp)])
    manual_rv = np.full(n, np.nan)
    for t in range(n):
        window = rets[max(0, t - 19): t + 1]
        if len(window) == 20 and np.isfinite(window).all():
            manual_rv[t] = float(np.std(window, ddof=1))
    lag = features._VOL_ROC_LAG
    manual_roc = np.full(n, np.nan)
    for t in range(n):
        now, before = manual_rv[t], manual_rv[t - lag] if t >= lag else np.nan
        if np.isfinite(now) and np.isfinite(before):
            manual_roc[t] = 0.0 if (now <= 0 or before <= 0) else float(
                np.clip(math.log(now / before), -features.VOL_ROC_CLIP, features.VOL_ROC_CLIP))
    check("vol20_roc  vs hand-written rolling std + log ratio", out["vol20_roc"], manual_roc, tol=1e-12)

    # -- Savitzky-Golay: per-row least-squares polyfit, a different algorithm ---
    W, DEG = features.SG_WINDOW, features.SG_DEGREE
    poly_slope, poly_curv = np.full(n, np.nan), np.full(n, np.nan)
    for t in range(W - 1, n):
        y = logp[t - W + 1: t + 1]
        x = np.arange(-(W - 1), 1, dtype=float)   # oldest .. newest, newest at x=0
        coeffs = np.polyfit(x, y, DEG)            # highest power first
        poly_slope[t] = coeffs[1]                 # d/dx at x=0
        poly_curv[t] = 2.0 * coeffs[0]            # d2/dx2
    check("sg_slope     vs per-row np.polyfit least squares", out["sg_slope"], poly_slope, tol=1e-9)
    check("sg_curvature vs per-row np.polyfit least squares", out["sg_curvature"], poly_curv, tol=1e-9)

    # -- frac-diff weights: textbook generalized binomial, not the recursion ----
    d = features.FRAC_DIFF_D
    textbook = np.array([(-1.0) ** k * math.comb(0, 0) if k == 0 else
                         (-1.0) ** k * math.prod((d - j) for j in range(k)) / math.factorial(k)
                         for k in range(len(features.FRAC_DIFF_WEIGHTS))])
    check("frac-diff weights vs generalized binomial (-1)^k C(d,k)",
          features.FRAC_DIFF_WEIGHTS, textbook, tol=1e-12)

    # -- frac_diff_close: direct dot product, anchored, plain loop --------------
    w = features.FRAC_DIFF_WEIGHTS
    width = len(w)
    manual_ffd = np.full(n, np.nan)
    for t in range(width - 1, n):
        total = 0.0
        anchor = logp[t - width + 1]
        for k in range(width):
            total += w[k] * (logp[t - k] - anchor)
        manual_ffd[t] = total
    check("frac_diff_close vs hand-written anchored dot product", out["frac_diff_close"], manual_ffd, tol=1e-10)

    print()
    print("=" * 86)
    print("2. CLOSED-FORM GROUND TRUTH (the answer is known analytically)")
    print("=" * 86)

    # Savitzky-Golay must reproduce the exact derivatives of an exact quadratic.
    t_idx = np.arange(300.0)
    a, b, c = np.log(50.0), 0.0011, -4.2e-6
    exact_logp = a + b * t_idx + c * t_idx**2
    px = np.exp(exact_logp)
    qframe = pd.DataFrame({
        "Date": pd.bdate_range("2020-01-01", periods=len(px)),
        "Open": px, "High": px, "Low": px, "Close": px, "Adj Close": px,
        "Dividends": 0.0, "Stock Splits": 0.0, "Volume": 1_000_000,
    })
    qadj, _ = data.adjusted_ohlc(qframe)
    qout = features.compute_ticker_features(qadj, extra_features=("calculus",))
    rows = np.arange(W - 1, len(px))
    check("sg_slope     on exact quadratic  = b + 2ct",
          qout["sg_slope"].to_numpy()[rows], b + 2 * c * t_idx[rows], tol=1e-11)
    check("sg_curvature on exact quadratic  = 2c",
          qout["sg_curvature"].to_numpy()[rows], np.full(len(rows), 2 * c), tol=1e-11)

    # A pure random walk differenced at d=1 is exactly ret_close; the frac-diff
    # machinery must reproduce that when d=1 (weights [1, -1]).
    w1 = features._frac_diff_weights(1.0, 1e-6, 60)
    check("frac-diff weights at d=1         = [1, -1]", w1, np.array([1.0, -1.0]), tol=1e-15)

    print()
    print("=" * 86)
    print("3. INVARIANCES (properties that must hold for ANY input)")
    print("=" * 86)

    scaled = frame.copy()
    for col in ("Open", "High", "Low", "Close", "Adj Close"):
        scaled[col] = scaled[col] * 137.0
    sadj, _ = data.adjusted_ohlc(scaled)
    sout = features.compute_ticker_features(sadj, extra_features=("calculus",))
    for col in features.CALCULUS_FEATURES:
        check(f"{col:16s} invariant to a 137x price scaling", sout[col], out[col], tol=1e-9)

    print()
    print("=" * 86)
    print("4. ENSEMBLE BLEND vs hand arithmetic")
    print("=" * 86)

    fc = {
        "a": {"point": {"open": 0.010, "high": 0.030, "low": -0.010, "close": 0.020},
              "quantiles": {k: {"p10": v - 0.01, "p50": v, "p90": v + 0.01}
                            for k, v in (("open", 0.010), ("high", 0.030), ("low", -0.010), ("close", 0.020))}},
        "b": {"point": {"open": -0.020, "high": 0.010, "low": -0.040, "close": 0.000},
              "quantiles": {k: {"p10": v - 0.02, "p50": v, "p90": v + 0.02}
                            for k, v in (("open", -0.020), ("high", 0.010), ("low", -0.040), ("close", 0.000))}},
    }
    weights = {c: {"a": 0.25, "b": 0.75} for c in ("open", "high", "low", "close")}
    blended = stacking.blend(fc, weights)
    hand_close = 0.25 * 0.020 + 0.75 * 0.000
    hand_p90 = 0.25 * (0.020 + 0.01) + 0.75 * (0.000 + 0.02)
    check("blend point.close  = 0.25*a + 0.75*b", [blended["point"]["close"]], [hand_close], tol=1e-15)
    check("blend p90.close    = vincentized average", [blended["quantiles"]["close"]["p90"]], [hand_p90], tol=1e-15)

    # EWMA weights vs pandas' own ewm(halflife=...)
    half_life = 30.0
    tracker = stacking.ComponentWeights(("a", "b"), half_life_days=half_life, floor=0.0)
    rng = np.random.default_rng(3)
    losses_a, losses_b = rng.uniform(0.001, 0.02, 40), rng.uniform(0.001, 0.02, 40)
    for la, lb in zip(losses_a, losses_b, strict=True):
        tracker.observe_day({"a": {c: la for c in ("open", "high", "low", "close")},
                             "b": {c: lb for c in ("open", "high", "low", "close")}})
    pandas_a = pd.Series(losses_a.tolist()).ewm(halflife=half_life, adjust=False).mean().iloc[-1]
    pandas_b = pd.Series(losses_b.tolist()).ewm(halflife=half_life, adjust=False).mean().iloc[-1]
    check("EWMA loss (model a) vs pandas ewm(adjust=False)",
          [tracker.mean_loss("a")["close"]], [pandas_a], tol=1e-12)
    # NOTE: the implementation guards division with + _LOSS_EPS. Omitting it here
    # produced a 1.4e-08 deviation -- the CHECK was wrong, not the code. Left
    # explicit because reproducing the guard is part of reproducing the formula.
    eps = stacking._LOSS_EPS
    strength_a, strength_b = 1.0 / (pandas_a + eps), 1.0 / (pandas_b + eps)
    check("weight (model a)    vs 1/(L+eps) normalised by hand",
          [tracker.weights()["close"]["a"]], [strength_a / (strength_a + strength_b)], tol=1e-12)

    print()
    print("=" * 86)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} CHECK(S) FAILED")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("RESULT: all checks passed")

if __name__ == '__main__':
    main()
