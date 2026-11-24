"""Strategy runners as pure functions of a price path, plus the CRN comparison report.

Scoring rule (docs/mc_variance_reduction.md, "Cost-basis and IRR bias"): every strategy receives
the *same* cash, ``daily_budget`` per step, and is scored on terminal wealth = shares x final price
+ unspent cash (which earns ``cash_rate`` per year). Average cost basis and IRR are not scores:
strategies that buy more after declines look better on both by construction (Hayley, Cass).

The graded ladder here is a research re-statement of the stepped tier match in
``dca_ladder_core.ladder_multiplier`` -- deliberately not an import of it. This package may never
import the live engine (tests/test_architecture_boundaries.py), and a study must be able to try
tier sets the live config does not hold. Tiers are passed in; nothing reads ``config/``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from mc_research.estimators import Estimate, independent_difference_se, paired_control_variate, paired_difference
from mc_research.paths import PathSet

Path = Sequence[float]
StrategyFn = Callable[[Path], float]


def stepped_multiplier(drop: float, tiers: Sequence[tuple[float, float]]) -> float:
    """Largest threshold <= drop wins; below the first threshold, or a non-finite drop, is 0.0 (no buy)."""
    if not math.isfinite(drop):
        return 0.0
    chosen = 0.0
    for threshold, multiplier in sorted((float(t), float(m)) for t, m in tiers):
        if drop >= threshold:
            chosen = multiplier
        else:
            break
    return chosen


def _validate_path(path: Path) -> None:
    if len(path) < 2:
        raise ValueError(f"a path needs at least 2 prices, got {len(path)}")
    if any(not math.isfinite(p) or p <= 0.0 for p in path):
        raise ValueError("path prices must be positive and finite")


def flat_dca(path: Path, *, daily_budget: float, cash_rate: float = 0.0, dt: float = 1.0 / 252) -> float:
    """Buy ``daily_budget`` at every step's price. Never holds cash, so ``cash_rate`` cannot matter."""
    del cash_rate, dt  # same signature as graded_dca so the two are interchangeable in a study
    _validate_path(path)
    shares = math.fsum(daily_budget / price for price in path[1:])
    return shares * path[-1]


def flat_dca_gbm_mean(*, s0: float, mu: float, n_steps: int, dt: float, daily_budget: float) -> float:
    """Exact E[flat DCA terminal wealth] under GBM: sum_t budget * E[S_n / S_t] = budget * sum e^{mu dt (n-t)}.

    The flat-DCA control variate's known mean. Only valid for ``paths.gbm_paths`` with the same
    ``mu``/``dt``/``n_steps``; under a bootstrap generator there is no such closed form, so use the
    CRN paired difference alone (it needs no known mean).
    """
    del s0  # terminal wealth / budget is a ratio of prices; the starting level cancels
    return daily_budget * math.fsum(math.exp(mu * dt * (n_steps - t)) for t in range(1, n_steps + 1))


def graded_dca(
    path: Path,
    *,
    daily_budget: float,
    tiers: Sequence[tuple[float, float]],
    cash_rate: float = 0.0,
    dt: float = 1.0 / 252,
) -> float:
    """Accrue ``daily_budget`` into cash each step; spend ``daily_budget x tier multiplier`` (capped at cash).

    ``drop`` is the step's fractional decline from the prior price (0.02 == 2% down), matching the
    live ladder's convention. Unspent cash compounds at ``cash_rate`` (annual, continuous) -- the
    cash-drag term that dominates DCA-vs-lump-sum results, so never leave it implicit.
    """
    _validate_path(path)
    growth = math.exp(cash_rate * dt)
    cash = 0.0
    shares = 0.0
    for prev, price in zip(path, path[1:], strict=False):
        cash = cash * growth + daily_budget
        spend = min(cash, daily_budget * stepped_multiplier((prev - price) / prev, tiers))
        if spend > 0.0:
            shares += spend / price
            cash -= spend
    return shares * path[-1] + cash


@dataclass(frozen=True)
class LadderVariant:
    name: str
    tiers: tuple[tuple[float, float], ...]

    def runner(self, *, daily_budget: float, cash_rate: float, dt: float) -> StrategyFn:
        def run(path: Path) -> float:
            return graded_dca(path, daily_budget=daily_budget, tiers=self.tiers, cash_rate=cash_rate, dt=dt)

        return run


def run_strategies(paths: PathSet, strategies: Mapping[str, StrategyFn]) -> dict[str, tuple[float, ...]]:
    """Every strategy over the identical ``PathSet``: outcome ``i`` of each strategy is path ``i``."""
    if not strategies:
        raise ValueError("no strategies to run")
    return {name: tuple(fn(path) for path in paths.prices) for name, fn in strategies.items()}


@dataclass(frozen=True)
class Comparison:
    """``variant - baseline`` three ways, so the measured variance reduction is always on the page."""

    variant: str
    baseline: str
    independent: Estimate  # what independent paths would have cost
    crn: Estimate  # same paths, paired difference
    crn_cv: Estimate | None  # paired difference with the baseline as a known-mean control

    def crn_variance_reduction(self) -> float:
        return self.crn.variance_reduction_vs(self.independent)

    def crn_cv_variance_reduction(self) -> float | None:
        return None if self.crn_cv is None else self.crn_cv.variance_reduction_vs(self.independent)


def compare(
    outcomes: Mapping[str, Sequence[float]],
    *,
    variant: str,
    baseline: str,
    baseline_mean: float | None = None,
) -> Comparison:
    """Paired report for ``variant`` vs ``baseline``. Pass ``baseline_mean`` only when it is exact."""
    a, b = outcomes[variant], outcomes[baseline]
    return Comparison(
        variant=variant,
        baseline=baseline,
        independent=independent_difference_se(a, b),
        crn=paired_difference(a, b),
        crn_cv=None if baseline_mean is None else paired_control_variate(a, b, b, baseline_mean),
    )
