"""Rock 2 of the band-calibration cycle: honest uncertainty.

Written BEFORE the implementation (TDD-first; see
``claude/band-calibration/PLAN.md``). ``band_uncertainty`` does not exist yet.

Why this module exists: a report that says "coverage 79.85%" over millions
of ticker-days invites a binomial reading in which 79.85% is many standard
errors from 80%. That reading is wrong twice over. A horizon-h error from one
origin day shares h-1 days of realized returns with the next origin day's, and
every ticker on a date shares that date's market move. The honest unit of
evidence is the DATE, and neighbouring dates are not independent. So:

- intervals come from a moving-block bootstrap over dates (Kunsch 1989), with
  every ticker on a date resampled together and blocks at least as long as
  the horizon;
- the classic coverage tests (Kupiec 1995 proportion-of-failures;
  Christoffersen 1998 independence and conditional coverage) are offered for
  ONE time-ordered breach sequence, which is what they are defined on.

Expected test statistics below are computed from the published likelihood
ratios, never from the implementation.
"""

from __future__ import annotations

import math

import band_uncertainty as bu
import metrics
import numpy as np
import pytest
from data import COMPONENTS


def _chi2_sf_df1(x: float) -> float:
    return math.erfc(math.sqrt(x / 2.0))


def _chi2_sf_df2(x: float) -> float:
    return math.exp(-x / 2.0)


# --------------------------------------------------------------------------
# Moving-block resampling of dates
# --------------------------------------------------------------------------


class TestMovingBlockResample:
    def test_returns_one_index_per_date(self):
        rng = np.random.default_rng(0)
        indices = bu.moving_block_resample(100, 20, rng)
        assert len(indices) == 100
        assert indices.min() >= 0 and indices.max() < 100

    def test_is_made_of_contiguous_blocks(self):
        # The whole point of a block bootstrap: within a block, the original
        # day-to-day order (and so the serial dependence) is preserved.
        rng = np.random.default_rng(1)
        indices = bu.moving_block_resample(100, 20, rng)
        for start in range(0, 100, 20):
            block = indices[start : start + 20]
            assert np.array_equal(np.diff(block), np.ones(len(block) - 1, dtype=block.dtype))

    def test_last_block_is_truncated_when_length_does_not_divide(self):
        rng = np.random.default_rng(2)
        indices = bu.moving_block_resample(50, 20, rng)
        assert len(indices) == 50
        tail = indices[40:]
        assert np.array_equal(np.diff(tail), np.ones(9, dtype=tail.dtype))

    def test_block_length_one_is_the_ordinary_bootstrap(self):
        rng = np.random.default_rng(3)
        indices = bu.moving_block_resample(1000, 1, rng)
        # Sampling with replacement: some dates repeat, some never appear.
        assert len(np.unique(indices)) < 1000

    @pytest.mark.parametrize("block_length", [0, -1, 101])
    def test_rejects_impossible_block_lengths(self, block_length):
        with pytest.raises(ValueError):
            bu.moving_block_resample(100, block_length, np.random.default_rng(0))


# --------------------------------------------------------------------------
# Ratio interval (coverage, mean interval score)
# --------------------------------------------------------------------------


class TestRatioInterval:
    def test_point_estimate_is_the_pooled_ratio(self):
        hits = np.array([40.0, 45.0, 30.0, 50.0] * 30)
        totals = np.full(120, 50.0)
        result = bu.ratio_interval(hits, totals, block_length=5, seed=0)
        assert result.point == pytest.approx(hits.sum() / totals.sum())
        assert result.n_dates == 120
        assert result.block_length == 5

    def test_days_are_weighted_by_their_size(self):
        # A 10-ticker day and a 1000-ticker day are not equal evidence: the
        # statistic is sum(hits) / sum(n), not the mean of daily rates.
        hits = np.array([10.0, 0.0] * 40)
        totals = np.array([10.0, 1000.0] * 40)
        result = bu.ratio_interval(hits, totals, block_length=2, seed=0)
        assert result.point == pytest.approx(400.0 / 40400.0)

    def test_no_variation_gives_a_zero_width_interval(self):
        totals = np.array([50.0, 80.0, 20.0, 100.0] * 25)
        hits = 0.8 * totals
        result = bu.ratio_interval(hits, totals, block_length=10, seed=0)
        assert result.low == pytest.approx(0.8)
        assert result.high == pytest.approx(0.8)

    def test_interval_brackets_the_point(self):
        rng = np.random.default_rng(11)
        totals = np.full(400, 50.0)
        hits = rng.binomial(50, 0.8, size=400).astype(float)
        result = bu.ratio_interval(hits, totals, block_length=5, seed=0)
        assert result.low <= result.point <= result.high
        assert result.low < result.high

    def test_recovers_a_known_coverage(self):
        # 500 independent days of 50 tickers each at a true 80% hit rate.
        rng = np.random.default_rng(12)
        totals = np.full(500, 50.0)
        hits = rng.binomial(50, 0.8, size=500).astype(float)
        result = bu.ratio_interval(hits, totals, block_length=5, seed=0)
        assert result.low <= 0.8 <= result.high
        assert result.high - result.low < 0.02

    def test_clustered_misses_widen_the_interval(self):
        # The SAME 200 daily coverage values, arranged two ways. Interleaved,
        # every 20-day block averages 80%. In runs, a block is all-60% or
        # all-100%. A method that ignored serial dependence would report the
        # same interval for both; this one must not.
        totals = np.full(200, 100.0)
        interleaved = np.array([60.0, 100.0] * 100)
        in_runs = np.array(([60.0] * 20 + [100.0] * 20) * 5)
        calm = bu.ratio_interval(interleaved, totals, block_length=20, seed=0)
        stressed = bu.ratio_interval(in_runs, totals, block_length=20, seed=0)
        assert calm.point == pytest.approx(stressed.point)
        assert (stressed.high - stressed.low) > 5 * (calm.high - calm.low)

    def test_same_seed_same_interval(self):
        rng = np.random.default_rng(13)
        totals = np.full(300, 50.0)
        hits = rng.binomial(50, 0.8, size=300).astype(float)
        first = bu.ratio_interval(hits, totals, block_length=5, seed=7)
        second = bu.ratio_interval(hits, totals, block_length=5, seed=7)
        assert (first.low, first.high) == (second.low, second.high)

    def test_wider_level_gives_wider_interval(self):
        rng = np.random.default_rng(14)
        totals = np.full(300, 50.0)
        hits = rng.binomial(50, 0.8, size=300).astype(float)
        narrow = bu.ratio_interval(hits, totals, block_length=5, level=0.80, seed=0)
        wide = bu.ratio_interval(hits, totals, block_length=5, level=0.99, seed=0)
        assert (wide.high - wide.low) > (narrow.high - narrow.low)

    def test_too_few_blocks_reports_unknown_not_a_number(self):
        # 100 dates at horizon 90 is barely one independent window. The
        # honest interval is "unknown": the point is still reported, the
        # bounds are None with a reason, and nothing raises -- a short run
        # must not crash its own report.
        totals = np.full(100, 50.0)
        hits = np.full(100, 40.0)
        result = bu.ratio_interval(hits, totals, block_length=90, seed=0)
        assert result.point == pytest.approx(0.8)
        assert result.low is None and result.high is None
        assert result.reason == "too_few_blocks"

    def test_empty_denominator_reports_unknown(self):
        result = bu.ratio_interval(np.zeros(50), np.zeros(50), block_length=5, seed=0)
        assert result.point is None
        assert result.low is None and result.high is None
        assert result.reason == "no_observations"

    def test_mismatched_lengths_are_rejected(self):
        with pytest.raises(ValueError):
            bu.ratio_interval(np.ones(10), np.ones(11), block_length=2, seed=0)


# --------------------------------------------------------------------------
# Skill interval (1 - model loss / baseline loss), resampled jointly
# --------------------------------------------------------------------------


class TestSkillInterval:
    def test_point_is_one_minus_the_loss_ratio(self):
        baseline = np.array([2.0, 4.0, 6.0, 8.0] * 30)
        model = 0.75 * baseline
        result = bu.skill_interval(model, baseline, block_length=5, seed=0)
        assert result.point == pytest.approx(0.25)

    def test_constant_advantage_gives_zero_width(self):
        # Model and baseline are resampled on the SAME dates. If the model
        # is exactly 25% better every day, every resample agrees, however
        # much the daily loss level itself swings.
        rng = np.random.default_rng(21)
        baseline = rng.uniform(1.0, 10.0, size=200)
        model = 0.75 * baseline
        result = bu.skill_interval(model, baseline, block_length=10, seed=0)
        assert result.low == pytest.approx(0.25)
        assert result.high == pytest.approx(0.25)

    def test_no_real_advantage_interval_covers_zero(self):
        rng = np.random.default_rng(22)
        baseline = rng.uniform(1.0, 10.0, size=400)
        model = baseline * rng.uniform(0.9, 1.1, size=400)
        result = bu.skill_interval(model, baseline, block_length=5, seed=0)
        assert result.low < 0.0 < result.high

    def test_real_advantage_interval_excludes_zero(self):
        rng = np.random.default_rng(23)
        baseline = rng.uniform(1.0, 10.0, size=400)
        model = baseline * rng.uniform(0.7, 0.9, size=400)
        result = bu.skill_interval(model, baseline, block_length=5, seed=0)
        assert result.low > 0.0

    def test_zero_baseline_loss_reports_unknown(self):
        result = bu.skill_interval(np.ones(50), np.zeros(50), block_length=5, seed=0)
        assert result.point is None
        assert result.reason == "no_baseline_loss"


# --------------------------------------------------------------------------
# Kupiec proportion-of-failures test
# --------------------------------------------------------------------------


def _kupiec_statistic(n: int, x: int, p: float) -> float:
    """LR_pof = -2 ln[ (1-p)^(n-x) p^x / ((1-x/n)^(n-x) (x/n)^x) ] (Kupiec 1995)."""
    rate = x / n
    return -2.0 * ((n - x) * math.log((1 - p) / (1 - rate)) + x * math.log(p / rate))


class TestKupiec:
    def test_observed_rate_equal_to_expected_cannot_reject(self):
        result = bu.kupiec_pof(n_observations=100, n_breaches=10, expected_rate=0.1)
        assert result.statistic == pytest.approx(0.0, abs=1e-9)
        assert result.p_value == pytest.approx(1.0)

    def test_matches_the_published_likelihood_ratio(self):
        # 20 breaches in 100 where 10 are expected: LR = 8.88, p = 0.0029.
        expected = _kupiec_statistic(100, 20, 0.1)
        assert expected == pytest.approx(8.8806, abs=1e-3)
        result = bu.kupiec_pof(n_observations=100, n_breaches=20, expected_rate=0.1)
        assert result.statistic == pytest.approx(expected)
        assert result.p_value == pytest.approx(_chi2_sf_df1(expected))
        assert result.p_value < 0.01

    def test_too_few_breaches_is_also_a_failure(self):
        # A band that never misses is too wide, not perfect.
        result = bu.kupiec_pof(n_observations=250, n_breaches=5, expected_rate=0.1)
        assert result.statistic == pytest.approx(_kupiec_statistic(250, 5, 0.1))
        assert result.p_value < 0.01

    def test_zero_breaches_uses_the_limit_not_log_of_zero(self):
        # x = 0: the x*ln(.) term vanishes, LR = -2 n ln(1 - p).
        result = bu.kupiec_pof(n_observations=50, n_breaches=0, expected_rate=0.1)
        assert result.statistic == pytest.approx(-2.0 * 50 * math.log(0.9))
        assert math.isfinite(result.p_value)

    def test_every_observation_a_breach_uses_the_limit(self):
        result = bu.kupiec_pof(n_observations=50, n_breaches=50, expected_rate=0.1)
        assert result.statistic == pytest.approx(-2.0 * 50 * math.log(0.1))

    def test_no_observations_reports_unknown(self):
        result = bu.kupiec_pof(n_observations=0, n_breaches=0, expected_rate=0.1)
        assert result.statistic is None and result.p_value is None

    @pytest.mark.parametrize("rate", [0.0, 1.0, -0.1, 1.5])
    def test_rejects_an_expected_rate_outside_the_open_unit_interval(self, rate):
        with pytest.raises(ValueError):
            bu.kupiec_pof(n_observations=10, n_breaches=1, expected_rate=rate)

    def test_rejects_more_breaches_than_observations(self):
        with pytest.raises(ValueError):
            bu.kupiec_pof(n_observations=10, n_breaches=11, expected_rate=0.1)


# --------------------------------------------------------------------------
# Christoffersen independence and conditional coverage
# --------------------------------------------------------------------------


def _independence_statistic(breaches: list[bool]) -> float:
    """LR_ind from first-order transition counts (Christoffersen 1998)."""
    n = {(a, b): 0 for a in (0, 1) for b in (0, 1)}
    for previous, current in zip(breaches[:-1], breaches[1:], strict=True):
        n[(int(previous), int(current))] += 1
    pi01 = n[(0, 1)] / (n[(0, 0)] + n[(0, 1)])
    pi11 = n[(1, 1)] / (n[(1, 0)] + n[(1, 1)])
    pi = (n[(0, 1)] + n[(1, 1)]) / sum(n.values())

    def term(count: int, probability: float) -> float:
        return count * math.log(probability) if count else 0.0

    restricted = term(n[(0, 0)] + n[(1, 0)], 1 - pi) + term(n[(0, 1)] + n[(1, 1)], pi)
    unrestricted = (
        term(n[(0, 0)], 1 - pi01) + term(n[(0, 1)], pi01) + term(n[(1, 0)], 1 - pi11) + term(n[(1, 1)], pi11)
    )
    return -2.0 * (restricted - unrestricted)


class TestChristoffersen:
    def test_clustered_breaches_are_rejected(self):
        # 20 breaches in 100, all in one run: the right count, the wrong
        # timing. Kupiec alone would be content with a 20% target.
        breaches = [False] * 80 + [True] * 20
        expected = _independence_statistic(breaches)
        result = bu.christoffersen_independence(breaches)
        assert result.statistic == pytest.approx(expected)
        assert result.p_value == pytest.approx(_chi2_sf_df1(expected))
        assert result.p_value < 0.001

    def test_breaches_unrelated_to_yesterday_are_not_rejected(self):
        # "0011" repeated: after a hit and after a miss the next day is a
        # breach half the time either way.
        breaches = [False, False, True, True] * 25
        expected = _independence_statistic(breaches)
        result = bu.christoffersen_independence(breaches)
        assert result.statistic == pytest.approx(expected)
        assert result.p_value > 0.5

    def test_no_breaches_at_all_reports_unknown(self):
        # With no breach there is no "day after a breach" to estimate from.
        result = bu.christoffersen_independence([False] * 100)
        assert result.statistic is None and result.p_value is None

    def test_fewer_than_two_observations_reports_unknown(self):
        result = bu.christoffersen_independence([True])
        assert result.statistic is None and result.p_value is None

    def test_conditional_coverage_is_the_sum_of_both_tests(self):
        breaches = [False] * 80 + [True] * 20
        pof = _kupiec_statistic(100, 20, 0.1)
        independence = _independence_statistic(breaches)
        result = bu.christoffersen_conditional_coverage(breaches, expected_rate=0.1)
        assert result.statistic == pytest.approx(pof + independence)
        assert result.p_value == pytest.approx(_chi2_sf_df2(pof + independence))

    def test_conditional_coverage_unknown_when_independence_is(self):
        result = bu.christoffersen_conditional_coverage([False] * 100, expected_rate=0.1)
        assert result.statistic is None and result.p_value is None


# --------------------------------------------------------------------------
# Per-date series built from score records
# --------------------------------------------------------------------------


def _score(*, actual_close, low=-0.02, high=0.02):
    point = {c: 0.0 for c in COMPONENTS}
    quantiles = {c: {"p10": low, "p50": 0.0, "p90": high} for c in COMPONENTS}
    actual = {"open": 0.0, "high": max(0.0, actual_close), "low": min(0.0, actual_close), "close": actual_close}
    return metrics.score_forecast(
        point=point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
    )


def _record(score, *, target_date, model="octopus", ticker="AAA", horizon=None):
    record = {"model": model, "ticker": ticker, "origin_date": "2021-01-04", "target_date": target_date, "score": score}
    if horizon is not None:
        record["horizon"] = horizon
    return record


class TestDailyBandSeries:
    def test_one_row_per_target_date_in_date_order(self):
        records = [
            _record(_score(actual_close=0.0), target_date="2021-06-03", ticker="AAA"),
            _record(_score(actual_close=-0.05), target_date="2021-06-02", ticker="AAA"),
            _record(_score(actual_close=0.0), target_date="2021-06-02", ticker="BBB"),
        ]
        series = bu.daily_band_series(records)[("octopus", 1)]
        assert series.dates == ["2021-06-02", "2021-06-03"]
        assert series.n.tolist() == [2.0, 1.0]
        assert series.in_band.tolist() == [1.0, 1.0]
        assert series.below.tolist() == [1.0, 0.0]
        assert series.above.tolist() == [0.0, 0.0]

    def test_interval_score_is_summed_per_date(self):
        records = [
            _record(_score(actual_close=0.0), target_date="2021-06-02", ticker="AAA"),
            _record(_score(actual_close=-0.05), target_date="2021-06-02", ticker="BBB"),
        ]
        series = bu.daily_band_series(records)[("octopus", 1)]
        assert series.interval_score.tolist() == pytest.approx([0.04 + 0.04 + 10 * 0.03])

    def test_models_and_horizons_are_kept_apart(self):
        records = [
            _record(_score(actual_close=0.0), target_date="2021-06-02", model="octopus", horizon=1),
            _record(_score(actual_close=0.0), target_date="2021-06-02", model="octopus", horizon=5),
            _record(_score(actual_close=0.0), target_date="2021-06-02", model="persistence"),
        ]
        series = bu.daily_band_series(records)
        assert set(series) == {("octopus", 1), ("octopus", 5), ("persistence", 1)}

    def test_feeds_the_interval_directly(self):
        # The intended use, end to end: records -> daily series -> coverage
        # interval. 120 dates, 4 tickers each, one of the four misses below.
        records = []
        for day in range(120):
            date = f"2021-{1 + day // 28:02d}-{1 + day % 28:02d}"
            for index, ticker in enumerate(("AAA", "BBB", "CCC", "DDD")):
                close = -0.05 if index == 0 else 0.0
                records.append(_record(_score(actual_close=close), target_date=date, ticker=ticker))
        series = bu.daily_band_series(records)[("octopus", 1)]
        result = bu.ratio_interval(series.in_band, series.n, block_length=5, seed=0)
        assert result.point == pytest.approx(0.75)
        assert result.low == pytest.approx(0.75) and result.high == pytest.approx(0.75)

    def test_records_from_before_the_scorecard_still_count_for_coverage(self):
        legacy = dict(_score(actual_close=-0.05))
        for key in ("interval_score", "interval_score_raw", "below_band", "above_band"):
            legacy.pop(key)
        series = bu.daily_band_series([_record(legacy, target_date="2021-06-02")])[("octopus", 1)]
        assert series.n.tolist() == [1.0]
        assert series.in_band.tolist() == [0.0]
        # Side is not recoverable for such a record: it is counted in n but
        # not in n_sided, so a side rate is taken over n_sided only.
        assert series.n_sided.tolist() == [0.0]
        assert series.interval_score.tolist() == pytest.approx([0.04 + 10 * 0.03])
