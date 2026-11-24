"""Monte Carlo estimators that always return an ``Estimate`` -- never a bare float.

Every report built on this package shows a mean *with* its standard error, confidence interval,
sample count and effective sample size, and names the method that produced it. A 1-cent error bar
on the wrong model is still wrong (docs/mc_variance_reduction.md, "Precision is not validity"),
but a number with no error bar at all cannot even be judged.

Fail closed, as elsewhere in this repo: fewer than two samples, a non-finite sample, mismatched
lengths, or an importance-sampling run whose effective sample size is below the floor all raise
instead of returning a plausible-looking number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Z_95 = 1.959963984540054  # two-sided 95% normal quantile


class InsufficientEffectiveSampleSize(ValueError):
    """Importance weights collapsed: the estimate rests on too few effective samples to report."""


@dataclass(frozen=True)
class Estimate:
    mean: float
    se: float
    ci_low: float
    ci_high: float
    n: int  # independent units the SE is computed from (pairs, for antithetic)
    n_eff: float  # effective sample size (== n except under importance sampling)
    method: str

    def variance_reduction_vs(self, baseline: Estimate) -> float:
        """Variance ratio baseline/self at equal cost: ``4.7`` means this method needs ~1/4.7 the paths.

        ``inf`` when this estimate's SE is exactly zero (e.g. a strategy compared with itself).
        """
        if self.se == 0.0:
            return math.inf
        return (baseline.se / self.se) ** 2

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "mean": self.mean,
            "se": self.se,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "n": self.n,
            "n_eff": self.n_eff,
            "method": self.method,
        }


def _checked(samples: Sequence[float], name: str = "samples", minimum: int = 2) -> list[float]:
    values = [float(v) for v in samples]
    if len(values) < minimum:
        raise ValueError(f"{name}: need at least {minimum} values, got {len(values)}")
    bad = [i for i, v in enumerate(values) if not math.isfinite(v)]
    if bad:
        raise ValueError(f"{name}: non-finite value at index {bad[0]} ({len(bad)} total)")
    return values


def _same_length(*named: tuple[str, Sequence[float]]) -> None:
    lengths = {name: len(values) for name, values in named}
    if len(set(lengths.values())) != 1:
        raise ValueError(f"length mismatch: {lengths}")


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _sample_variance(values: Sequence[float]) -> float:
    m = _mean(values)
    return math.fsum((v - m) ** 2 for v in values) / (len(values) - 1)


def _sample_covariance(x: Sequence[float], y: Sequence[float]) -> float:
    mx, my = _mean(x), _mean(y)
    return math.fsum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / (len(x) - 1)


def _build(mean: float, se: float, n: int, method: str, n_eff: float | None = None) -> Estimate:
    half = Z_95 * se
    return Estimate(mean, se, mean - half, mean + half, n, float(n if n_eff is None else n_eff), method)


def plain(samples: Sequence[float], method: str = "plain") -> Estimate:
    """Sample mean with SE = s / sqrt(n). Halving the error costs 4x the paths."""
    values = _checked(samples)
    n = len(values)
    return _build(_mean(values), math.sqrt(_sample_variance(values) / n), n, method)


def antithetic(samples: Sequence[float]) -> Estimate:
    """Mirrored-path estimator. ``samples`` alternate (z, -z): indices 2i and 2i+1 are one pair.

    The SE comes from the n *pair means*, not the 2n draws (Boyle, Broadie and Glasserman 1997):
    the two halves of a pair are not independent, so treating them as such understates the error.
    Pilot-check the pair correlation before relying on this: a non-monotone payoff (tier triggers
    that switch on and off) can make pairs positively correlated and *raise* the variance.
    """
    values = _checked(samples, minimum=4)
    if len(values) % 2:
        raise ValueError(f"antithetic samples must come in pairs, got odd length {len(values)}")
    pair_means = [(values[i] + values[i + 1]) / 2.0 for i in range(0, len(values), 2)]
    return plain(pair_means, method="antithetic")


def antithetic_pair_correlation(samples: Sequence[float]) -> float:
    """Correlation between the two halves of each pair. Antithetics help only when this is negative."""
    values = _checked(samples, minimum=4)
    if len(values) % 2:
        raise ValueError(f"antithetic samples must come in pairs, got odd length {len(values)}")
    first, second = values[0::2], values[1::2]
    denom = math.sqrt(_sample_variance(first) * _sample_variance(second))
    return 0.0 if denom == 0.0 else _sample_covariance(first, second) / denom


def control_variate(
    y: Sequence[float],
    x: Sequence[float],
    x_mean: float,
    *,
    pilot_fraction: float = 0.0,
) -> Estimate:
    """Control-variate estimator: mean of ``y - b (x - x_mean)`` with ``b = cov(y, x) / var(x)``.

    ``x_mean`` must be the *exact* mean of the control under the simulation model (e.g. S0*e^{rT}
    for a GBM terminal price, or the Black-Scholes value). Passing a mean estimated from these same
    samples cancels the control and silently reports plain MC.

    Estimating ``b`` on the same samples adds an O(1/n) bias. ``pilot_fraction`` > 0 estimates ``b``
    on that leading share of the samples and applies it only to the rest, removing the bias at the
    cost of the pilot samples.
    """
    ys, xs = _checked(y, "y"), _checked(x, "x")
    _same_length(("y", ys), ("x", xs))
    if not math.isfinite(x_mean):
        raise ValueError(f"x_mean must be finite, got {x_mean!r}")
    if not 0.0 <= pilot_fraction < 1.0:
        raise ValueError(f"pilot_fraction must be in [0, 1), got {pilot_fraction!r}")
    split = int(len(ys) * pilot_fraction)
    if split and (split < 2 or len(ys) - split < 2):
        raise ValueError(f"pilot split {split}/{len(ys)} leaves fewer than 2 samples on one side")
    fit_y, fit_x = (ys[:split], xs[:split]) if split else (ys, xs)
    var_x = _sample_variance(fit_x)
    b = 0.0 if var_x == 0.0 else _sample_covariance(fit_y, fit_x) / var_x
    use_y, use_x = (ys[split:], xs[split:]) if split else (ys, xs)
    adjusted = [a - b * (c - x_mean) for a, c in zip(use_y, use_x, strict=True)]
    return plain(adjusted, method="control_variate_pilot" if split else "control_variate")


def paired_difference(a: Sequence[float], b: Sequence[float]) -> Estimate:
    """CRN estimator of E[A - B] when A and B were run on the *same* paths.

    Var(A - B) = Var(A) + Var(B) - 2 Cov(A, B): for two highly correlated ladder variants the
    covariance term removes almost everything. Only valid when index i of ``a`` and ``b`` is the
    same path -- use ``strategies.run_strategies`` so that holds by construction.
    """
    av, bv = _checked(a, "a"), _checked(b, "b")
    _same_length(("a", av), ("b", bv))
    return plain([x - y for x, y in zip(av, bv, strict=True)], method="crn_paired")


def independent_difference_se(a: Sequence[float], b: Sequence[float]) -> Estimate:
    """What E[A - B] would cost on *independent* paths: SE = sqrt(var(A)/n + var(B)/n).

    Reported beside ``paired_difference`` so every comparison shows its measured CRN gain.
    """
    av, bv = _checked(a, "a"), _checked(b, "b")
    _same_length(("a", av), ("b", bv))
    n = len(av)
    se = math.sqrt(_sample_variance(av) / n + _sample_variance(bv) / n)
    return _build(_mean(av) - _mean(bv), se, n, "independent_difference")


def paired_control_variate(
    a: Sequence[float],
    b: Sequence[float],
    control: Sequence[float],
    control_mean: float,
    *,
    pilot_fraction: float = 0.0,
) -> Estimate:
    """CRN difference A - B, then a control variate on top (variance x a further 1 - rho^2)."""
    av, bv = _checked(a, "a"), _checked(b, "b")
    _same_length(("a", av), ("b", bv))
    d = [x - y for x, y in zip(av, bv, strict=True)]
    estimate = control_variate(d, control, control_mean, pilot_fraction=pilot_fraction)
    return Estimate(
        estimate.mean, estimate.se, estimate.ci_low, estimate.ci_high, estimate.n, estimate.n_eff,
        "crn_paired+" + estimate.method,
    )


def effective_sample_size(weights: Sequence[float]) -> float:
    """Kish ESS = (sum w)^2 / sum w^2. Equal weights give n; one dominant weight gives ~1."""
    ws = _checked(weights, "weights", minimum=1)
    if any(w < 0.0 for w in ws):
        raise ValueError("importance weights must be non-negative")
    total_sq = math.fsum(w * w for w in ws)
    if total_sq == 0.0:
        return 0.0
    return math.fsum(ws) ** 2 / total_sq


def importance_sampling(values: Sequence[float], weights: Sequence[float], *, ess_floor: float) -> Estimate:
    """Unbiased IS estimator mean(w_i f_i), with ESS reported and enforced.

    ``weights`` are likelihood ratios p(x)/q(x) for draws taken under the tilted measure q.
    Raises ``InsufficientEffectiveSampleSize`` when ESS < ``ess_floor``: in high dimensions the
    largest weight tends to dominate (Bengtsson, Bickel and Li 2008), and a degenerate IS run
    looks precise while resting on a handful of paths. Tilt only low-dimensional drivers.
    """
    fs, ws = _checked(values, "values"), _checked(weights, "weights")
    _same_length(("values", fs), ("weights", ws))
    if not math.isfinite(ess_floor) or ess_floor <= 0.0:
        raise ValueError(f"ess_floor must be a positive finite number, got {ess_floor!r}")
    ess = effective_sample_size(ws)
    if ess < ess_floor:
        raise InsufficientEffectiveSampleSize(f"ESS {ess:.1f} below floor {ess_floor:.1f} (n={len(ws)})")
    base = plain([w * f for w, f in zip(ws, fs, strict=True)], method="importance_sampling")
    return _build(base.mean, base.se, base.n, base.method, n_eff=ess)
