"""Guards against picking a lucky variant: the deflated Sharpe ratio and PBO.

The expected edge here is a few basis points per tilt dollar, so the best of a handful of
variants will look good by chance alone. Two standard guards, both stdlib:

* Deflated Sharpe ratio (Bailey and Lopez de Prado 2014, "The Deflated Sharpe Ratio"): the
  probability that the true Sharpe exceeds the best Sharpe expected from ``n_trials``
  independent tries with zero skill, given the observed skew, kurtosis and sample length.
  The Phase 0 ledger records the promotion bar as DSR >= 0.95
  (market_data_ingest/ledger/defaults.py, PROMOTION_DSR_THRESHOLD); this module computes the
  number and leaves the bar to the reader. ``n_trials`` should be the ledger's
  ``trial_count(experiment=...)`` -- every configuration ever tried, not just the ones
  passed in -- so the deflation cannot be dodged by forgetting old trials.
* Probability of backtest overfitting via combinatorially symmetric cross-validation
  (Bailey, Borwein, Lopez de Prado and Zhu 2014, "The Probability of Backtest Overfitting"):
  split the aligned daily series into S blocks, and for every half/half split pick the
  in-sample best variant and see where it ranks out of sample. PBO is the share of splits in
  which it lands in the bottom half.

Sharpe ratios here are per period (daily, not annualised), as both papers' formulas expect.
I have not re-derived either paper's results; the formulas follow their published
statements and are unit-tested only for their limiting behaviour.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

EULER_GAMMA = 0.5772156649015329
_NORMAL = statistics.NormalDist()


def sharpe(returns: Sequence[float]) -> float | None:
    """Per-period Sharpe (mean / sample stdev). None for fewer than 2 points or zero spread."""
    if len(returns) < 2:
        return None
    sd = statistics.stdev(returns)
    return statistics.fmean(returns) / sd if sd > 0.0 else None


def skew_kurtosis(returns: Sequence[float]) -> tuple[float, float]:
    """(skewness, non-excess kurtosis) by population moments; a normal series gives (0, 3)."""
    n = len(returns)
    mean = statistics.fmean(returns)
    m2 = sum((x - mean) ** 2 for x in returns) / n
    if m2 <= 0.0:
        return 0.0, 3.0
    m3 = sum((x - mean) ** 3 for x in returns) / n
    m4 = sum((x - mean) ** 4 for x in returns) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def probabilistic_sharpe(sr: float, benchmark_sr: float, n_obs: int, skew: float, kurtosis: float) -> float:
    """P(true SR > benchmark_sr) given an observed per-period ``sr`` over ``n_obs`` periods."""
    if n_obs < 2:
        raise ValueError("need at least 2 observations")
    denom_sq = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if denom_sq <= 0.0:
        raise ValueError("non-positive Sharpe variance estimate (extreme skew/kurtosis)")
    return _NORMAL.cdf((sr - benchmark_sr) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq))


def expected_max_sharpe(n_trials: int, trial_sharpe_variance: float) -> float:
    """The best per-period Sharpe expected from ``n_trials`` zero-skill trials whose Sharpes
    have variance ``trial_sharpe_variance``. One trial: 0 (no selection, no deflation)."""
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1:
        return 0.0
    z1 = _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(max(trial_sharpe_variance, 0.0)) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(returns: Sequence[float], n_trials: int, trial_sharpe_variance: float) -> float:
    """DSR of the selected variant's per-period ``returns``."""
    sr = sharpe(returns)
    if sr is None:
        raise ValueError("Sharpe undefined (fewer than 2 returns or zero variance)")
    skew, kurt = skew_kurtosis(returns)
    return probabilistic_sharpe(sr, expected_max_sharpe(n_trials, trial_sharpe_variance), len(returns), skew, kurt)


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    n_splits: int
    n_blocks: int
    logits: tuple[float, ...]


def probability_of_backtest_overfitting(series: Mapping[str, Sequence[float]], n_blocks: int = 10) -> PBOResult:
    """CSCV PBO over aligned per-period returns of two or more variants.

    The series are cut into ``n_blocks`` equal contiguous blocks (the oldest remainder
    periods are dropped so every block is the same length). Performance is per-period Sharpe;
    a variant with zero spread in a half scores -inf there. Ties go to the alphabetically
    first variant, deterministically.
    """
    names = sorted(series)
    if len(names) < 2:
        raise ValueError("PBO needs at least two variants")
    lengths = {len(series[n]) for n in names}
    if len(lengths) != 1:
        raise ValueError("variant series must be aligned (equal length)")
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("n_blocks must be an even number >= 2")
    t = lengths.pop()
    block = t // n_blocks
    if block < 2:
        raise ValueError(f"{t} periods is too short for {n_blocks} blocks")
    start = t - block * n_blocks
    # Per variant, per block: (n, sum, sum of squares). A half's Sharpe is then O(blocks).
    stats = {}
    for n in names:
        xs = series[n][start:]
        stats[n] = [(block, math.fsum(xs[b * block:(b + 1) * block]),
                     math.fsum(x * x for x in xs[b * block:(b + 1) * block])) for b in range(n_blocks)]

    def half_sharpe(name: str, blocks: Sequence[int]) -> float:
        cnt = sum(stats[name][b][0] for b in blocks)
        s = sum(stats[name][b][1] for b in blocks)
        ss = sum(stats[name][b][2] for b in blocks)
        mean = s / cnt
        var = (ss - cnt * mean * mean) / (cnt - 1)
        return mean / math.sqrt(var) if var > 1e-30 else -math.inf

    logits = []
    all_blocks = range(n_blocks)
    for chosen in itertools.combinations(all_blocks, n_blocks // 2):
        oos = [b for b in all_blocks if b not in chosen]
        is_perf = {n: half_sharpe(n, chosen) for n in names}
        best = max(names, key=lambda n: (is_perf[n], -names.index(n)))
        oos_perf = {n: half_sharpe(n, oos) for n in names}
        # Relative rank in (0, 1): 1 = worst ... M = best, then / (M + 1).
        rank = 1 + sum(1 for n in names if oos_perf[n] < oos_perf[best])
        omega = rank / (len(names) + 1)
        logits.append(math.log(omega / (1.0 - omega)))
    pbo = sum(1 for lg in logits if lg <= 0.0) / len(logits)
    return PBOResult(pbo, len(logits), n_blocks, tuple(logits))
