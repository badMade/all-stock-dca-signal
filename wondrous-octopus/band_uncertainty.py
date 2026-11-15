"""Honest uncertainty for band metrics: date-block bootstrap and coverage tests.

A report that says "coverage 79.85%" over millions of ticker-days invites a
binomial reading in which that is many standard errors away from 80%. The
reading is wrong twice over:

- a horizon-h error from one origin day shares h-1 days of realized returns
  with the next origin day's, so consecutive dates are serially dependent;
- every ticker scored on a date shares that date's market move, so a day of
  3,000 tickers is nowhere near 3,000 independent observations.

The unit of evidence is therefore the DATE, and neighbouring dates are not
independent. Everything here works on per-date aggregates:

- :func:`ratio_interval` / :func:`skill_interval` -- percentile intervals
  from a moving-block bootstrap over dates (Kunsch 1989, "The Jackknife and
  the Bootstrap for General Stationary Observations", Ann. Statist. 17). All
  tickers on a date travel together, and a block should be at least as long
  as the forecast horizon so the overlap between consecutive horizon-h
  targets stays inside a block.
- :func:`kupiec_pof`, :func:`christoffersen_independence`,
  :func:`christoffersen_conditional_coverage` -- the classic coverage tests,
  defined on ONE time-ordered breach sequence (Kupiec 1995, J. Derivatives
  3(2); Christoffersen 1998, Int. Econ. Rev. 39(4)). The independence test
  is only meaningful at horizon 1: at longer horizons breaches are serially
  dependent by construction and it would reject a perfectly good band.
- :func:`daily_band_series` -- folds ledgered score records into the
  per-date arrays the functions above take.

Unknown is reported as ``None`` with a reason, never as a number and never
as an exception: a short run (100 dates at horizon 90 is barely one
independent window) must still be able to write its own report, and a
made-up interval would be worse than none.

Torch-free and stdlib+numpy only, like ``horizons.py``, so the statistics
can be tested and reasoned about on their own. No scipy: the two chi-square
tail probabilities needed have closed forms (df=1: erfc(sqrt(x/2)); df=2:
exp(-x/2)).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
from metrics import INTERVAL_PENALTY

#: Bootstrap resamples. Percentile endpoints at the 95% level are stable to
#: roughly the third decimal at this count, and a full report asks for a few
#: dozen intervals, so the cost is seconds.
DEFAULT_RESAMPLES = 2000

#: A block bootstrap needs more than one block to say anything about
#: block-to-block variation. Below this many whole blocks the interval is
#: reported as unknown rather than computed from a single window.
MIN_BLOCKS = 2


@dataclass(frozen=True)
class Interval:
    """A point estimate with a bootstrap interval, or an honest "unknown".

    ``low``/``high`` are None when the interval cannot be estimated, and
    ``point`` is None when the statistic itself is undefined; ``reason``
    says which case applies.
    """

    point: float | None
    low: float | None
    high: float | None
    n_dates: int
    block_length: int
    reason: str | None = None


@dataclass(frozen=True)
class CoverageTest:
    """A likelihood-ratio statistic and its p-value; both None when the test is undefined."""

    statistic: float | None
    p_value: float | None


@dataclass(frozen=True)
class DailyBandSeries:
    """Per-date sums for one (model, horizon), aligned with ``dates`` (ascending).

    ``n_sided`` counts only the records that carry breach side: ledgers
    written before the band scorecard cannot say which side a miss fell on,
    so a side rate is ``below.sum() / n_sided.sum()``, never over ``n``.
    """

    dates: list[str]
    n: np.ndarray[Any, Any]
    n_sided: np.ndarray[Any, Any]
    in_band: np.ndarray[Any, Any]
    below: np.ndarray[Any, Any]
    above: np.ndarray[Any, Any]
    interval_score: np.ndarray[Any, Any]


def moving_block_resample(n_dates: int, block_length: int, rng: np.random.Generator) -> np.ndarray[Any, Any]:
    """Indices of one moving-block resample: ``n_dates`` positions built from
    contiguous blocks of ``block_length`` drawn with replacement.

    Blocks are not wrapped around the end of the series (a block that joined
    the last dates to the first would manufacture an adjacency that never
    happened), and the final block is truncated so the resample has exactly
    the original length.
    """
    if block_length < 1 or block_length > n_dates:
        raise ValueError(f"block_length must be between 1 and n_dates={n_dates}, got {block_length}")
    n_blocks = -(-n_dates // block_length)
    starts = rng.integers(0, n_dates - block_length + 1, size=n_blocks)
    indices = (starts[:, None] + np.arange(block_length)[None, :]).reshape(-1)
    return indices[:n_dates]


def _percentile_interval(
    statistic: Callable[[np.ndarray[Any, Any]], float],
    *,
    n_dates: int,
    block_length: int,
    n_resamples: int,
    level: float,
    seed: int,
) -> tuple[float, float]:
    # Seeded per call, not from global state: the same ledger must produce
    # the same interval on every re-report, or a number that moved between
    # two runs could not be told apart from a number that changed.
    rng = np.random.default_rng(seed)
    draws = np.array(
        [statistic(moving_block_resample(n_dates, block_length, rng)) for _ in range(n_resamples)], dtype=np.float64
    )
    # A resample can land on dates with no observations at all (ragged
    # histories); its statistic is undefined and is left out rather than
    # counted as zero.
    draws = draws[np.isfinite(draws)]
    tail = (1.0 - level) / 2.0
    return float(np.quantile(draws, tail)), float(np.quantile(draws, 1.0 - tail))


def _paired_arrays(
    first: Sequence[float], second: Sequence[float]
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    left = np.asarray(first, dtype=np.float64)
    right = np.asarray(second, dtype=np.float64)
    if left.shape != right.shape or left.ndim != 1:
        raise ValueError(f"expected two 1-D per-date arrays of equal length, got {left.shape} and {right.shape}")
    return left, right


def ratio_interval(
    numerator: Sequence[float],
    denominator: Sequence[float],
    *,
    block_length: int,
    n_resamples: int = DEFAULT_RESAMPLES,
    level: float = 0.95,
    seed: int = 0,
) -> Interval:
    """Interval for ``sum(numerator) / sum(denominator)`` over resampled dates.

    Coverage is hits over forecasts; mean interval score is summed score over
    forecasts. Both are pooled ratios, so a 10-ticker day weighs less than a
    3,000-ticker day -- which the mean of daily rates would get wrong.
    """
    num, den = _paired_arrays(numerator, denominator)
    n_dates = len(num)
    total = float(den.sum())
    if total <= 0.0:
        return Interval(None, None, None, n_dates, block_length, "no_observations")
    point = float(num.sum()) / total
    if n_dates < MIN_BLOCKS * block_length:
        return Interval(point, None, None, n_dates, block_length, "too_few_blocks")

    def statistic(indices: np.ndarray[Any, Any]) -> float:
        resampled_total = float(den[indices].sum())
        return float(num[indices].sum()) / resampled_total if resampled_total > 0.0 else math.nan

    low, high = _percentile_interval(
        statistic, n_dates=n_dates, block_length=block_length, n_resamples=n_resamples, level=level, seed=seed
    )
    return Interval(point, low, high, n_dates, block_length)


def skill_interval(
    model_loss: Sequence[float],
    baseline_loss: Sequence[float],
    *,
    block_length: int,
    n_resamples: int = DEFAULT_RESAMPLES,
    level: float = 0.95,
    seed: int = 0,
) -> Interval:
    """Interval for skill ``1 - sum(model_loss) / sum(baseline_loss)``.

    Model and baseline are resampled on the SAME dates. A hard day is hard
    for both, and that shared difficulty cancels in the ratio; resampling
    them separately would add it back as noise and bury a real advantage.
    """
    model, baseline = _paired_arrays(model_loss, baseline_loss)
    n_dates = len(model)
    total = float(baseline.sum())
    if total <= 0.0:
        return Interval(None, None, None, n_dates, block_length, "no_baseline_loss")
    point = 1.0 - float(model.sum()) / total
    if n_dates < MIN_BLOCKS * block_length:
        return Interval(point, None, None, n_dates, block_length, "too_few_blocks")

    def statistic(indices: np.ndarray[Any, Any]) -> float:
        resampled_total = float(baseline[indices].sum())
        return 1.0 - float(model[indices].sum()) / resampled_total if resampled_total > 0.0 else math.nan

    low, high = _percentile_interval(
        statistic, n_dates=n_dates, block_length=block_length, n_resamples=n_resamples, level=level, seed=seed
    )
    return Interval(point, low, high, n_dates, block_length)


def _count_times_log(count: int, probability: float) -> float:
    # The 0 * log(0) limit is 0. Without this, a sequence with no breaches
    # (or nothing but breaches) would take log(0) on a term that contributes
    # nothing to the likelihood.
    return count * math.log(probability) if count else 0.0


def _chi2_tail_df1(statistic: float) -> float:
    return math.erfc(math.sqrt(statistic / 2.0))


def _chi2_tail_df2(statistic: float) -> float:
    return math.exp(-statistic / 2.0)


def kupiec_pof(*, n_observations: int, n_breaches: int, expected_rate: float) -> CoverageTest:
    """Kupiec's proportion-of-failures test: is the breach RATE the promised one?

    Two-sided by construction: a band that almost never misses is too wide
    and fails just as a band that misses too often does. Apply it to the
    below-p10 and above-p90 breaches separately, each against a 10% rate.
    """
    if not 0.0 < expected_rate < 1.0:
        raise ValueError(f"expected_rate must be strictly between 0 and 1, got {expected_rate}")
    if n_breaches < 0 or n_breaches > n_observations:
        raise ValueError(f"n_breaches={n_breaches} is not between 0 and n_observations={n_observations}")
    if n_observations == 0:
        return CoverageTest(None, None)
    hits = n_observations - n_breaches
    observed_rate = n_breaches / n_observations
    under_null = _count_times_log(hits, 1.0 - expected_rate) + _count_times_log(n_breaches, expected_rate)
    under_observed = _count_times_log(hits, 1.0 - observed_rate) + _count_times_log(n_breaches, observed_rate)
    # Clamped at zero: when the observed rate equals the expected one the
    # two log-likelihoods are equal in exact arithmetic and can differ by a
    # rounding error of either sign in floating point.
    statistic = max(0.0, -2.0 * (under_null - under_observed))
    return CoverageTest(statistic, _chi2_tail_df1(statistic))


def christoffersen_independence(breaches: Sequence[bool]) -> CoverageTest:
    """Christoffersen's independence test: does a breach make another more likely?

    Catches the failure the rate test cannot see -- the right number of
    misses, all on consecutive days. Undefined (None) when the sequence has
    no day following a breach, or none following a hit: there is then no
    transition probability to compare.
    """
    sequence = [1 if breach else 0 for breach in breaches]
    if len(sequence) < 2:
        return CoverageTest(None, None)
    transitions = {(previous, current): 0 for previous in (0, 1) for current in (0, 1)}
    for previous, current in zip(sequence[:-1], sequence[1:], strict=True):
        transitions[(previous, current)] += 1
    after_hit = transitions[(0, 0)] + transitions[(0, 1)]
    after_breach = transitions[(1, 0)] + transitions[(1, 1)]
    if after_hit == 0 or after_breach == 0:
        return CoverageTest(None, None)
    breach_after_hit = transitions[(0, 1)] / after_hit
    breach_after_breach = transitions[(1, 1)] / after_breach
    breach_overall = (transitions[(0, 1)] + transitions[(1, 1)]) / (after_hit + after_breach)
    restricted = _count_times_log(transitions[(0, 0)] + transitions[(1, 0)], 1.0 - breach_overall) + _count_times_log(
        transitions[(0, 1)] + transitions[(1, 1)], breach_overall
    )
    unrestricted = (
        _count_times_log(transitions[(0, 0)], 1.0 - breach_after_hit)
        + _count_times_log(transitions[(0, 1)], breach_after_hit)
        + _count_times_log(transitions[(1, 0)], 1.0 - breach_after_breach)
        + _count_times_log(transitions[(1, 1)], breach_after_breach)
    )
    statistic = max(0.0, -2.0 * (restricted - unrestricted))
    return CoverageTest(statistic, _chi2_tail_df1(statistic))


def christoffersen_conditional_coverage(breaches: Sequence[bool], *, expected_rate: float) -> CoverageTest:
    """Rate and independence together: the sum of both statistics, two degrees of freedom.

    Unknown whenever either part is: a "pass" assembled from half a test
    would claim more than was checked.
    """
    sequence = list(breaches)
    independence = christoffersen_independence(sequence)
    rate = kupiec_pof(
        n_observations=len(sequence), n_breaches=sum(1 for breach in sequence if breach), expected_rate=expected_rate
    )
    if independence.statistic is None or rate.statistic is None:
        return CoverageTest(None, None)
    statistic = independence.statistic + rate.statistic
    return CoverageTest(statistic, _chi2_tail_df2(statistic))


def daily_band_series(
    records: Iterable[Mapping[str, Any]], *, component: str = "close"
) -> dict[tuple[str, int], DailyBandSeries]:
    """Fold score records into per-date sums, keyed by (model, horizon).

    Accepts ``horizon_score`` records and legacy ``score`` records (no
    ``horizon`` key means horizon 1), the same convention
    ``metrics.HorizonMetricsAccumulator`` uses. Records written before the
    band scorecard still count toward ``n`` and ``in_band``; their interval
    score is recovered from the stored pinball losses (see
    ``metrics._fold_scorecard``) and they are left out of ``n_sided``.
    """
    # Column order of each per-date row; kept beside the unpacking below.
    by_key: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0] * 6))
    for record in records:
        score = record["score"]
        row = by_key[(record["model"], int(record.get("horizon", 1)))][record["target_date"]]
        row[0] += 1.0
        row[2] += 1.0 if score["in_band"][component] else 0.0
        if "interval_score" in score:
            row[1] += 1.0
            row[3] += 1.0 if score["below_band"][component] else 0.0
            row[4] += 1.0 if score["above_band"][component] else 0.0
            row[5] += score["interval_score"][component]
        else:
            pinball = score["pinball"][component]
            row[5] += INTERVAL_PENALTY * (pinball["p10"] + pinball["p90"])
    series: dict[tuple[str, int], DailyBandSeries] = {}
    for key, days in by_key.items():
        dates = sorted(days)
        table = np.array([days[date] for date in dates], dtype=np.float64)
        series[key] = DailyBandSeries(
            dates=dates,
            n=table[:, 0],
            n_sided=table[:, 1],
            in_band=table[:, 2],
            below=table[:, 3],
            above=table[:, 4],
            interval_score=table[:, 5],
        )
    return series
