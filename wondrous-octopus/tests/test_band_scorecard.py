"""Rock 1 of the band-calibration cycle: the band scorecard.

Written BEFORE the implementation (TDD-first; see
``claude/band-calibration/PLAN.md`` and ``docs/adr-octopus-band-scorecard.md``).
Every expected number below is derived from the published definition of the
interval score, never from metrics.py:

    IS_alpha(l, u; x) = (u - l)
                      + (2 / alpha) * (l - x) * 1{x < l}
                      + (2 / alpha) * (x - u) * 1{x > u}

(Gneiting & Raftery 2007, "Strictly Proper Scoring Rules, Prediction, and
Estimation", JASA 102:359-378, eq. 43.) The p10-p90 band is the central 80%
interval, so alpha = 0.2 and the miss penalty is 10x the distance outside.

Why these three quantities and nothing else: ``coverage`` and ``band_width``
are already reported, but separately, so a wider band always looks better on
coverage and a narrower one always looks better on width. The interval score
is the single proper score that prices both. The below/above split exists
because the calibrator scales the band symmetrically around p50 -- a band can
hit 80% overall while missing 15% below and 5% above, and the pooled number
hides that.
"""

from __future__ import annotations

import metrics
import pytest
from data import COMPONENTS

#: 2 / alpha for the central 80% interval.
PENALTY = 10.0


def _score(*, low=-0.02, high=0.02, actual_close=0.0, raw_low=None, raw_high=None):
    """One scored forecast whose close band is [low, high] around p50 = 0."""
    raw_low = low if raw_low is None else raw_low
    raw_high = high if raw_high is None else raw_high
    point = {c: 0.0 for c in COMPONENTS}
    quantiles = {c: {"p10": low, "p50": 0.0, "p90": high} for c in COMPONENTS}
    quantiles_raw = {c: {"p10": raw_low, "p50": 0.0, "p90": raw_high} for c in COMPONENTS}
    actual = {"open": 0.0, "high": max(0.0, actual_close), "low": min(0.0, actual_close), "close": actual_close}
    return metrics.score_forecast(
        point=point, quantiles=quantiles, quantiles_raw=quantiles_raw, actual=actual, ref_close=100.0
    )


def _record(score, *, model="octopus", target_date="2021-06-02", ticker="AAA", horizon=None):
    record = {
        "model": model,
        "ticker": ticker,
        "origin_date": "2021-06-01",
        "target_date": target_date,
        "score": score,
    }
    if horizon is not None:
        record["horizon"] = horizon
    return record


def _strip_scorecard_keys(score):
    """A score record as ledgers written before this change hold it."""
    legacy = dict(score)
    for key in ("interval_score", "interval_score_raw", "below_band", "above_band"):
        legacy.pop(key, None)
    return legacy


class TestIntervalScorePerForecast:
    def test_inside_the_band_the_score_is_the_width(self):
        score = _score(low=-0.02, high=0.02, actual_close=0.005)
        assert score["interval_score"]["close"] == pytest.approx(0.04)

    def test_below_the_band_adds_ten_times_the_shortfall(self):
        score = _score(low=-0.02, high=0.02, actual_close=-0.05)
        assert score["interval_score"]["close"] == pytest.approx(0.04 + PENALTY * 0.03)

    def test_above_the_band_adds_ten_times_the_overshoot(self):
        score = _score(low=-0.02, high=0.02, actual_close=0.03)
        assert score["interval_score"]["close"] == pytest.approx(0.04 + PENALTY * 0.01)

    def test_on_the_edge_counts_as_inside(self):
        # in_band uses closed bounds (p10 <= x <= p90); the scorecard must
        # agree with it or coverage and the breach rates would not sum to 1.
        score = _score(low=-0.02, high=0.02, actual_close=0.02)
        assert score["interval_score"]["close"] == pytest.approx(0.04)
        assert score["below_band"]["close"] is False
        assert score["above_band"]["close"] is False

    def test_raw_band_is_scored_separately(self):
        # Calibrated band wide enough to cover, raw band too narrow: the two
        # scores must differ, or calibration's contribution is invisible.
        score = _score(low=-0.04, high=0.04, raw_low=-0.01, raw_high=0.01, actual_close=0.03)
        assert score["interval_score"]["close"] == pytest.approx(0.08)
        assert score["interval_score_raw"]["close"] == pytest.approx(0.02 + PENALTY * 0.02)

    def test_every_component_is_scored(self):
        score = _score()
        assert set(score["interval_score"]) == set(COMPONENTS)
        assert set(score["interval_score_raw"]) == set(COMPONENTS)

    def test_a_wider_band_that_still_covers_scores_worse(self):
        # The property that makes this worth adding: coverage alone cannot
        # tell these two bands apart, the interval score can.
        narrow = _score(low=-0.02, high=0.02, actual_close=0.0)
        wide = _score(low=-0.10, high=0.10, actual_close=0.0)
        assert narrow["in_band"]["close"] and wide["in_band"]["close"]
        assert narrow["interval_score"]["close"] < wide["interval_score"]["close"]


class TestBreachSide:
    def test_below(self):
        score = _score(actual_close=-0.05)
        assert score["below_band"]["close"] is True
        assert score["above_band"]["close"] is False

    def test_above(self):
        score = _score(actual_close=0.05)
        assert score["below_band"]["close"] is False
        assert score["above_band"]["close"] is True

    def test_inside_is_neither(self):
        score = _score(actual_close=0.0)
        assert score["below_band"]["close"] is False
        assert score["above_band"]["close"] is False

    @pytest.mark.parametrize("actual_close", [-0.05, -0.02, 0.0, 0.02, 0.05])
    def test_exactly_one_of_inside_below_above(self, actual_close):
        score = _score(actual_close=actual_close)
        flags = (score["in_band"]["close"], score["below_band"]["close"], score["above_band"]["close"])
        assert sum(1 for flag in flags if flag) == 1


class TestHorizonAggregation:
    def _report(self, records):
        accumulator = metrics.HorizonMetricsAccumulator()
        for record in records:
            accumulator.add_score(record)
        return accumulator.report()["by_horizon"]

    def test_mean_interval_score_and_breach_rates(self):
        # Four forecasts at horizon 5: two inside, one below by 0.03, one
        # above by 0.01. Width 0.04 each.
        closes = [0.0, 0.01, -0.05, 0.03]
        records = [_record(_score(actual_close=close), horizon=5) for close in closes]
        cell = self._report(records)["octopus"]["5"]["close"]
        assert cell["interval_score"] == pytest.approx((4 * 0.04 + PENALTY * 0.03 + PENALTY * 0.01) / 4)
        assert cell["below_rate"] == pytest.approx(0.25)
        assert cell["above_rate"] == pytest.approx(0.25)

    def test_breach_rates_and_coverage_sum_to_one(self):
        closes = [0.0, -0.05, -0.06, 0.03, 0.01]
        records = [_record(_score(actual_close=close), horizon=1) for close in closes]
        cell = self._report(records)["octopus"]["1"]["close"]
        assert cell["coverage"] + cell["below_rate"] + cell["above_rate"] == pytest.approx(1.0)

    def test_horizons_are_kept_apart(self):
        records = [
            _record(_score(actual_close=0.0), horizon=1),
            _record(_score(actual_close=-0.05), horizon=30),
        ]
        by_horizon = self._report(records)["octopus"]
        assert by_horizon["1"]["close"]["below_rate"] == pytest.approx(0.0)
        assert by_horizon["30"]["close"]["below_rate"] == pytest.approx(1.0)

    def test_raw_interval_score_is_reported_beside_calibrated(self):
        records = [
            _record(_score(low=-0.04, high=0.04, raw_low=-0.01, raw_high=0.01, actual_close=0.03), horizon=1)
        ]
        cell = self._report(records)["octopus"]["1"]["close"]
        assert cell["interval_score"] == pytest.approx(0.08)
        assert cell["interval_score_raw"] == pytest.approx(0.02 + PENALTY * 0.02)


class TestRegimeAggregation:
    def test_regime_table_carries_the_scorecard(self):
        accumulator = metrics.MetricsAccumulator()
        # 2008-10-15 is inside the "gfc" window; 2021-06-02 lands in "other".
        accumulator.add_score(_record(_score(actual_close=-0.05), target_date="2008-10-15"))
        accumulator.add_score(_record(_score(actual_close=0.0), target_date="2021-06-02"))
        by_regime = accumulator.report()["by_regime"]
        gfc = by_regime["gfc"]["octopus"]["close"]
        other = by_regime["other"]["octopus"]["close"]
        assert gfc["below_rate"] == pytest.approx(1.0)
        assert gfc["interval_score"] == pytest.approx(0.04 + PENALTY * 0.03)
        assert other["below_rate"] == pytest.approx(0.0)
        assert other["interval_score"] == pytest.approx(0.04)
        assert by_regime["all"]["octopus"]["close"]["below_rate"] == pytest.approx(0.5)


class TestLedgersWrittenBeforeThisChange:
    """A 26-year run ledger already on disk has no scorecard keys.

    Re-reporting it must not crash and must not invent numbers. The interval
    score of the CALIBRATED band is exactly recoverable from what those
    records do hold (it equals 2/alpha times the sum of the p10 and p90
    pinball losses, which follows from the two definitions), so it is
    reported. Breach side and the raw-band score are not recoverable, so they
    are reported as None, never as 0.0 -- a zero would read as "no breaches".
    """

    def _legacy_records(self):
        closes = [0.0, -0.05, 0.03]
        return [_record(_strip_scorecard_keys(_score(actual_close=close)), horizon=1) for close in closes]

    def test_horizon_report_recovers_interval_score(self):
        accumulator = metrics.HorizonMetricsAccumulator()
        for record in self._legacy_records():
            accumulator.add_score(record)
        cell = accumulator.report()["by_horizon"]["octopus"]["1"]["close"]
        assert cell["interval_score"] == pytest.approx((3 * 0.04 + PENALTY * 0.03 + PENALTY * 0.01) / 3)

    def test_horizon_report_marks_unrecoverable_fields_none(self):
        accumulator = metrics.HorizonMetricsAccumulator()
        for record in self._legacy_records():
            accumulator.add_score(record)
        cell = accumulator.report()["by_horizon"]["octopus"]["1"]["close"]
        assert cell["below_rate"] is None
        assert cell["above_rate"] is None
        assert cell["interval_score_raw"] is None

    def test_regime_report_survives_legacy_records(self):
        accumulator = metrics.MetricsAccumulator()
        for record in self._legacy_records():
            accumulator.add_score(record)
        cell = accumulator.report()["by_regime"]["all"]["octopus"]["close"]
        assert cell["interval_score"] == pytest.approx((3 * 0.04 + PENALTY * 0.03 + PENALTY * 0.01) / 3)
        assert cell["below_rate"] is None

    def test_mixed_old_and_new_records_rate_only_the_new_ones(self):
        # A resumed run appends new-format records to an old ledger. The
        # breach rates must be over the records that carry the flag, not
        # diluted by the ones that cannot say.
        accumulator = metrics.HorizonMetricsAccumulator()
        accumulator.add_score(_record(_strip_scorecard_keys(_score(actual_close=0.0)), horizon=1))
        accumulator.add_score(_record(_score(actual_close=-0.05), horizon=1))
        cell = accumulator.report()["by_horizon"]["octopus"]["1"]["close"]
        assert cell["below_rate"] == pytest.approx(1.0)
        assert cell["n"] == 2


def test_existing_score_fields_are_unchanged():
    # Additive only: nothing a current reader depends on moves.
    score = _score(actual_close=0.005)
    for key in ("abs_err", "sq_err", "price_abs_err", "pinball", "in_band", "in_band_raw", "band_width"):
        assert key in score
    assert score["band_width"]["close"] == pytest.approx(0.04)
    assert score["in_band"]["close"] is True
