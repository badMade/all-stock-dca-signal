"""Tests for stacking.py -- the regime-weighted ensemble over ledgered outputs.

The ensemble is built ENTIRELY from score records that already exist: a
score record carries the forecast (point + calibrated bands) AND the
realized truth, so a blend needs no new training pipeline and no second
pass over the models. That is what makes this cheap; it is also what makes
causality the property most worth testing, since the same records hold both
sides of the walk-forward boundary.

The causal contract, mirroring features.py's no-lookahead invariant: the
weights used to blend a forecast whose target is day T are fitted ONLY on
scores whose targets were revealed strictly before T. TestReplayCausality
enforces it the same way TestNoLookahead does -- perturb everything after a
cutoff, and nothing at or before it may move.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest
import stacking
from data import COMPONENTS
from ledger import Ledger
from metrics import score_forecast
from model_interface import QUANTILE_KEYS

ComponentWeights = stacking.ComponentWeights
blend = stacking.blend
mean_pinball = stacking.mean_pinball
replay = stacking.replay
ENSEMBLE_MODEL_NAME = stacking.ENSEMBLE_MODEL_NAME


_EPOCH = date(2020, 1, 1)


def _iso(offset):
    """A real calendar date, so no test ever writes '2020-01-61'."""
    return (_EPOCH + timedelta(days=offset)).isoformat()


def _bands(center, half_width=0.02):
    """A valid, ordered band around ``center`` for every component."""
    return {
        component: {"p10": center - half_width, "p50": center, "p90": center + half_width}
        for component in COMPONENTS
    }


def _point(value):
    return {component: value for component in COMPONENTS}


def _score_record(*, model, ticker, origin_date, target_date, predicted, actual, half_width=0.02):
    """One ledgered score record, the only input the ensemble consumes."""
    quantiles = _bands(predicted, half_width)
    point = _point(predicted)
    actual_map = _point(actual)
    return {
        "schema_version": 1,
        "record_type": "score",
        "model": model,
        "ticker": ticker,
        "origin_date": origin_date,
        "target_date": target_date,
        "seed": 0,
        "point": point,
        "quantiles": quantiles,
        "quantiles_raw": quantiles,
        "actual": actual_map,
        "ref_close": 100.0,
        "adj_ratio": 1.0,
        "score": score_forecast(
            point=point, quantiles=quantiles, quantiles_raw=quantiles,
            actual=actual_map, ref_close=100.0,
        ),
    }


def _write_ledger(run_dir, records):
    ledger = Ledger.create(run_dir)
    ledger.append(list(records))
    return ledger


def _two_model_ledger(run_dir, *, days=40, good_error=0.001, bad_error=0.030, from_day=0, scale=1.0):
    """A ledger where `good` tracks the truth and `bad` does not.

    ``scale`` multiplies both models' errors from ``from_day`` onward -- the
    perturbation handle the causality test uses.
    """
    records = []
    for day in range(days):
        origin, target = _iso(day), _iso(day + 1)
        factor = scale if day >= from_day else 1.0
        for model, error in (("good", good_error), ("bad", bad_error)):
            records.append(
                _score_record(
                    model=model, ticker="AAA", origin_date=origin, target_date=target,
                    predicted=error * factor, actual=0.0,
                )
            )
    _write_ledger(run_dir, records)


class TestMeanPinball:
    def test_averages_the_three_quantile_levels_per_component(self):
        record = _score_record(
            model="m", ticker="AAA", origin_date=_iso(0), target_date=_iso(1),
            predicted=0.01, actual=0.0,
        )
        losses = mean_pinball(record["score"])
        assert set(losses) == set(COMPONENTS)
        expected = float(np.mean([record["score"]["pinball"]["close"][key] for key in QUANTILE_KEYS]))
        assert losses["close"] == pytest.approx(expected, rel=1e-12)

    def test_a_perfect_forecast_scores_lower_than_a_wrong_one(self):
        near = _score_record(model="m", ticker="A", origin_date=_iso(0),
                             target_date=_iso(1), predicted=0.0, actual=0.0)
        far = _score_record(model="m", ticker="A", origin_date=_iso(0),
                            target_date=_iso(1), predicted=0.05, actual=0.0)
        assert mean_pinball(near["score"])["close"] < mean_pinball(far["score"])["close"]


class TestComponentWeights:
    def test_weights_are_uniform_before_any_observation(self):
        # Day one of a replay has nothing revealed yet; an equal blend is
        # the only honest prior.
        weights = ComponentWeights(("a", "b", "c")).weights()
        uniform = {name: pytest.approx(1 / 3) for name in ("a", "b", "c")}
        for component in COMPONENTS:
            assert weights[component] == uniform

    def test_duplicate_model_names_are_refused(self):
        # A duplicate collapses in every per-model dict but still counts
        # toward len(models), so ComponentWeights(("a", "a")) used to return
        # {"a": 0.5} -- a weight vector that does not sum to 1, silently
        # breaking the invariant the blend relies on (Copilot review
        # regression).
        with pytest.raises(ValueError, match="REFUSED: .*duplicate model name"):
            ComponentWeights(("a", "a"))
        with pytest.raises(ValueError, match="REFUSED: .*duplicate model name"):
            ComponentWeights(("a", "b", "a"))

    def test_an_empty_roster_is_refused(self):
        with pytest.raises(ValueError, match="REFUSED: .*at least one model"):
            ComponentWeights(())

    def test_weights_are_non_negative_and_sum_to_one(self):
        tracker = ComponentWeights(("a", "b"))
        tracker.observe_day({"a": _point(0.001), "b": _point(0.050)})
        for component, per_model in tracker.weights().items():
            assert all(weight >= 0.0 for weight in per_model.values()), component
            assert sum(per_model.values()) == pytest.approx(1.0, rel=1e-12)

    def test_the_lower_loss_model_gets_more_weight(self):
        tracker = ComponentWeights(("a", "b"))
        tracker.observe_day({"a": _point(0.001), "b": _point(0.050)})
        weights = tracker.weights()["close"]
        assert weights["a"] > weights["b"]

    def test_a_catastrophic_model_is_floored_not_dropped(self):
        # No model is ever fully dropped: today's worst is next regime's
        # best, and a zero weight cannot recover from evidence it never
        # gets to contribute to.
        tracker = ComponentWeights(("a", "b"), floor=0.05)
        for _ in range(200):
            tracker.observe_day({"a": _point(0.0001), "b": _point(10.0)})
        weights = tracker.weights()["close"]
        assert weights["b"] == pytest.approx(0.05, abs=1e-3)
        assert weights["b"] > 0.0

    def test_a_recovered_model_regains_its_weight(self):
        # The floor is what makes this possible: b keeps contributing while
        # it is bad, so when the regime turns the tracker can actually see
        # it improve.
        tracker = ComponentWeights(("a", "b"), half_life_days=5.0, floor=0.05)
        for _ in range(50):
            tracker.observe_day({"a": _point(0.001), "b": _point(0.050)})
        assert tracker.weights()["close"]["a"] > tracker.weights()["close"]["b"]
        for _ in range(50):
            tracker.observe_day({"a": _point(0.050), "b": _point(0.001)})
        assert tracker.weights()["close"]["b"] > tracker.weights()["close"]["a"]

    def test_one_half_life_halves_an_old_observation(self):
        # Exact EWMA arithmetic, not a vibe: after exactly half_life days
        # of zero loss, the original observation retains half its value.
        tracker = ComponentWeights(("a",), half_life_days=8.0)
        tracker.observe_day({"a": _point(1.0)})
        for _ in range(8):
            tracker.observe_day({"a": _point(0.0)})
        assert tracker.mean_loss("a")["close"] == pytest.approx(0.5, rel=1e-9)

    def test_a_never_observed_model_starts_at_the_average_loss(self):
        # 1/mean(loss), not mean(1/loss). 1/x is convex, so averaging the
        # STRENGTHS is dragged up hard by the best model: on losses of
        # 0.001 and 0.1 it implies a loss of 0.00198 and hands a newcomer
        # 0.333 of the blend where it has earned 0.038 (Antigravity review
        # regression). Constructed so the assertion needs no magic number:
        # "avg" is given exactly the mean of a's and b's losses, and adding
        # it leaves that mean unchanged, so a model with NO history must
        # land on precisely avg's weight.
        tracker = ComponentWeights(("a", "b", "avg", "newcomer"))
        tracker.observe_day(
            {"a": _point(0.001), "b": _point(0.1), "avg": _point((0.001 + 0.1) / 2)}
        )
        weights = tracker.weights()["close"]
        assert weights["newcomer"] == pytest.approx(weights["avg"], rel=1e-9)
        assert weights["newcomer"] < weights["a"]
        assert weights["newcomer"] > weights["b"]

    def test_a_model_that_stops_forecasting_does_not_drift_into_the_lead(self):
        # The trap in an INVERSE-loss scheme: decaying an absent model
        # pulls its loss toward zero, which reads as "getting better", so a
        # model that simply stopped producing forecasts would quietly take
        # over the blend. An absent model is left untouched instead.
        tracker = ComponentWeights(("a", "b"), half_life_days=5.0)
        for _ in range(10):
            tracker.observe_day({"a": _point(0.001), "b": _point(0.050)})
        before = tracker.weights()["close"]["b"]
        for _ in range(100):
            tracker.observe_day({"a": _point(0.001)})  # b sat every one of these out
        after = tracker.weights()["close"]["b"]
        assert after == pytest.approx(before, rel=1e-12)
        assert tracker.weights()["close"]["a"] > after

    def test_weights_are_fitted_per_component_not_per_model(self):
        # "A simple learned meta-weight per component": a model good at
        # close and bad at high must win the close blend and lose the high
        # one, which a single per-model weight could not express.
        tracker = ComponentWeights(("a", "b"))
        for _ in range(20):
            tracker.observe_day(
                {
                    "a": {"open": 0.01, "high": 0.050, "low": 0.01, "close": 0.001},
                    "b": {"open": 0.01, "high": 0.001, "low": 0.01, "close": 0.050},
                }
            )
        weights = tracker.weights()
        assert weights["close"]["a"] > weights["close"]["b"]
        assert weights["high"]["b"] > weights["high"]["a"]


class TestBlend:
    def _forecasts(self, values, half_width=0.02):
        return {
            model: {"point": _point(value), "quantiles": _bands(value, half_width)}
            for model, value in values.items()
        }

    def _equal_weights(self, models):
        return {component: {model: 1 / len(models) for model in models} for component in COMPONENTS}

    def test_blending_identical_forecasts_returns_that_forecast(self):
        forecasts = self._forecasts({"a": 0.01, "b": 0.01, "c": 0.01})
        blended = blend(forecasts, self._equal_weights(("a", "b", "c")))
        assert blended["point"]["close"] == pytest.approx(0.01, rel=1e-12)
        assert blended["quantiles"]["close"]["p90"] == pytest.approx(0.03, rel=1e-12)

    def test_a_weight_of_one_reproduces_that_model_exactly(self):
        forecasts = self._forecasts({"a": 0.01, "b": 0.09})
        weights = {component: {"a": 1.0, "b": 0.0} for component in COMPONENTS}
        blended = blend(forecasts, weights)
        assert blended["point"] == forecasts["a"]["point"]
        assert blended["quantiles"] == forecasts["a"]["quantiles"]

    def test_blend_is_the_weighted_quantile_average(self):
        # Vincentization: average the quantile FUNCTIONS, level by level.
        # Averaging densities instead would smear a bimodal blend into
        # something neither model predicted.
        forecasts = self._forecasts({"a": 0.00, "b": 0.10})
        weights = {component: {"a": 0.25, "b": 0.75} for component in COMPONENTS}
        blended = blend(forecasts, weights)
        assert blended["point"]["close"] == pytest.approx(0.075, rel=1e-12)
        assert blended["quantiles"]["close"]["p10"] == pytest.approx(0.075 - 0.02, rel=1e-12)
        assert blended["quantiles"]["close"]["p50"] == pytest.approx(0.075, rel=1e-12)

    def test_blending_ordered_bands_cannot_produce_a_crossing(self):
        # The structural reason vincentization is the right blend here: a
        # convex combination of sorted vectors stays sorted, so the
        # ensemble can never emit p10 > p50 and never needs the
        # quantile-crossing repair that finalize_forecast() counts.
        rng = np.random.default_rng(4)
        for _ in range(200):
            values = {name: float(rng.normal(0, 0.05)) for name in ("a", "b", "c")}
            widths = {name: float(rng.uniform(0.001, 0.2)) for name in ("a", "b", "c")}
            forecasts = {
                name: {"point": _point(values[name]), "quantiles": _bands(values[name], widths[name])}
                for name in ("a", "b", "c")
            }
            raw = rng.uniform(0.05, 1.0, size=3)
            weights = {
                component: dict(zip(("a", "b", "c"), raw / raw.sum(), strict=True))
                for component in COMPONENTS
            }
            band = blend(forecasts, weights)["quantiles"]
            for component in COMPONENTS:
                assert band[component]["p10"] <= band[component]["p50"] <= band[component]["p90"]

    def test_a_blend_under_shared_weights_needs_no_geometry_repair(self):
        # Under ONE weight vector shared by every component, max() being
        # convex does preserve high >= max(open, close), so the blended bar
        # is already possible and the repair is a no-op. This is the case
        # that holds -- see the next test for the case that does not.
        forecasts = {
            "a": {
                "point": {"open": 0.01, "high": 0.03, "low": -0.01, "close": 0.02},
                "quantiles": {
                    "open": {"p10": 0.00, "p50": 0.01, "p90": 0.02},
                    "high": {"p10": 0.02, "p50": 0.03, "p90": 0.04},
                    "low": {"p10": -0.02, "p50": -0.01, "p90": 0.00},
                    "close": {"p10": 0.01, "p50": 0.02, "p90": 0.03},
                },
            },
            "b": {
                "point": {"open": -0.02, "high": 0.01, "low": -0.04, "close": 0.00},
                "quantiles": {
                    "open": {"p10": -0.03, "p50": -0.02, "p90": -0.01},
                    "high": {"p10": 0.00, "p50": 0.01, "p90": 0.02},
                    "low": {"p10": -0.05, "p50": -0.04, "p90": -0.03},
                    "close": {"p10": -0.01, "p50": 0.00, "p90": 0.01},
                },
            },
        }
        blended = blend(forecasts, self._equal_weights(("a", "b")))
        # Exactly the unrepaired weighted average -- nothing was clamped.
        assert blended["point"]["high"] == pytest.approx(0.02, rel=1e-12)
        assert blended["point"]["open"] == pytest.approx(-0.005, rel=1e-12)
        assert blended["point"]["high"] >= max(blended["point"]["open"], blended["point"]["close"])
        assert blended["point"]["low"] <= min(blended["point"]["open"], blended["point"]["close"])

    def test_per_component_weights_can_require_a_bar_repair_and_report_it(self):
        # The convexity argument above needs ONE weight vector shared by
        # every component. Weights here are deliberately per-component, so
        # open can follow model A while high follows model B, and two
        # individually VALID bars blend into an impossible one. Measured:
        # high 0.001 lands below open 0.100. finalize_forecast() repairs it
        # so what leaves is a possible bar, but the repair is real work and
        # its count must not be silent (Copilot review regression:
        # "blend() can report nonzero repairs even when every input bar is
        # valid").
        def bar(open_, high, low, close):
            values = {"open": open_, "high": high, "low": low, "close": close}
            return {
                "point": dict(values),
                "quantiles": {
                    name: {"p10": value - 0.001, "p50": value, "p90": value + 0.001}
                    for name, value in values.items()
                },
            }

        forecasts = {"a": bar(0.00, 0.001, -0.001, 0.00), "b": bar(0.10, 0.101, 0.099, 0.10)}
        for forecast in forecasts.values():  # both inputs are valid bars
            point = forecast["point"]
            assert point["high"] >= max(point["open"], point["close"])
        weights = {
            "open": {"a": 0.0, "b": 1.0},
            "high": {"a": 1.0, "b": 0.0},
            "low": {"a": 1.0, "b": 0.0},
            "close": {"a": 0.0, "b": 1.0},
        }
        blended = blend(forecasts, weights)
        assert blended["repairs"]["bar_repairs"] > 0
        point = blended["point"]
        assert point["high"] >= max(point["open"], point["close"])
        assert point["low"] <= min(point["open"], point["close"])

    def test_a_broken_input_bar_is_repaired_not_propagated(self):
        forecasts = self._forecasts({"a": 0.01})
        forecasts["a"]["point"] = {"open": 0.05, "high": 0.00, "low": 0.01, "close": 0.04}
        blended = blend(forecasts, {component: {"a": 1.0} for component in COMPONENTS})
        assert blended["point"]["high"] >= max(blended["point"]["open"], blended["point"]["close"])
        assert blended["point"]["low"] <= min(blended["point"]["open"], blended["point"]["close"])

    def test_models_missing_from_a_day_are_renormalized_not_counted_as_zero(self):
        # Coverage differs between models (a GBM refit day, a ticker below
        # one model's warm-up). Treating an absent model as a 0.0 forecast
        # would drag the blend toward zero; the weights must renormalize
        # over the models that actually spoke.
        forecasts = self._forecasts({"a": 0.10})  # b did not forecast this ticker today
        weights = {component: {"a": 0.25, "b": 0.75} for component in COMPONENTS}
        blended = blend(forecasts, weights)
        assert blended["point"]["close"] == pytest.approx(0.10, rel=1e-12)

    def test_no_overlapping_model_refuses(self):
        # Fail closed rather than emit a forecast backed by nothing.
        with pytest.raises(ValueError, match="REFUSED"):
            blend({}, {component: {"a": 1.0} for component in COMPONENTS})


class TestReplayCausality:
    def test_ensemble_records_at_or_before_a_cutoff_are_unaffected_by_later_scores(self, tmp_path):
        # THE test. Same shape as features.py's no-lookahead regression:
        # perturb everything after day D and nothing at or before D may
        # move. If the weights ever saw the day they are blending, the
        # ensemble's whole walk-forward claim is void.
        clean_dir, perturbed_dir = tmp_path / "clean", tmp_path / "perturbed"
        _two_model_ledger(clean_dir, days=30)
        _two_model_ledger(perturbed_dir, days=30, from_day=20, scale=25.0)
        # The last target whose inputs AND weights predate the perturbation:
        # origin day 19 matures on day 20, and day 20 is the first scaled one.
        cutoff = _iso(20)

        clean = {(r["ticker"], r["origin_date"]): r for r in replay(clean_dir, ("good", "bad"))}
        perturbed = {(r["ticker"], r["origin_date"]): r for r in replay(perturbed_dir, ("good", "bad"))}
        compared = 0
        for key, record in clean.items():
            if record["target_date"] <= cutoff:
                assert record["point"] == perturbed[key]["point"], key
                assert record["quantiles"] == perturbed[key]["quantiles"], key
                compared += 1
        assert compared > 0  # the assertions above actually ran

    def test_the_first_revealed_day_blends_with_uniform_weights(self, tmp_path):
        _two_model_ledger(tmp_path / "run", days=10, good_error=0.001, bad_error=0.030)
        first = min(replay(tmp_path / "run", ("good", "bad")), key=lambda record: record["target_date"])
        assert first["point"]["close"] == pytest.approx((0.001 + 0.030) / 2, rel=1e-9)

    def test_weights_move_toward_the_better_model_as_evidence_accumulates(self, tmp_path):
        _two_model_ledger(tmp_path / "run", days=60, good_error=0.001, bad_error=0.030)
        records = sorted(replay(tmp_path / "run", ("good", "bad")), key=lambda r: r["target_date"])
        # The blend starts halfway between the two and migrates toward the
        # accurate one; it never overshoots past it.
        assert records[-1]["point"]["close"] < records[0]["point"]["close"]
        assert records[-1]["point"]["close"] > 0.001

    def test_weight_decay_is_per_revealed_day_not_per_ticker(self, tmp_path):
        # The rule ConformalCalibrator.observe_batch() already follows in
        # this repo: a full-universe day matures thousands of records, so
        # decaying once per RECORD would collapse a 60-day half-life into
        # about a day of replay and make the weights depend on universe
        # size. Two ledgers with identical per-record losses, one carrying
        # 1 ticker a day and one carrying 50, must blend identically.
        def build(run_dir, n_tickers):
            records = []
            for day in range(30):
                for index in range(n_tickers):
                    for model, error in (("good", 0.001), ("bad", 0.030)):
                        records.append(
                            _score_record(
                                model=model, ticker=f"T{index:03d}", origin_date=_iso(day),
                                target_date=_iso(day + 1), predicted=error, actual=0.0,
                            )
                        )
            _write_ledger(run_dir, records)

        def final_blend(run_dir):
            records = sorted(replay(run_dir, ("good", "bad")), key=lambda r: (r["target_date"], r["ticker"]))
            return records[-1]["point"]["close"]

        build(tmp_path / "sparse", 1)
        build(tmp_path / "dense", 50)
        assert final_blend(tmp_path / "sparse") == pytest.approx(final_blend(tmp_path / "dense"), rel=1e-9)

    def test_replay_intersects_to_shared_ticker_origin_pairs(self, tmp_path):
        # The repo's honest-comparison rule: multi-model reports intersect
        # to shared (ticker, origin day) pairs. A ticker only one model
        # forecast that day is not an ensemble forecast.
        run_dir = tmp_path / "run"
        records = [
            _score_record(model="good", ticker="AAA", origin_date=_iso(0),
                          target_date=_iso(1), predicted=0.01, actual=0.0),
            _score_record(model="bad", ticker="AAA", origin_date=_iso(0),
                          target_date=_iso(1), predicted=0.02, actual=0.0),
            _score_record(model="good", ticker="SOLO", origin_date=_iso(0),
                          target_date=_iso(1), predicted=0.01, actual=0.0),
        ]
        _write_ledger(run_dir, records)
        tickers = {record["ticker"] for record in replay(run_dir, ("good", "bad"))}
        assert tickers == {"AAA"}

    def test_emitted_records_carry_the_ensemble_name_and_the_score_shape(self, tmp_path):
        # Emitted under a synthetic model name in the SAME record shape, so
        # metrics.py's regime x model x component tables pick the ensemble
        # up with no changes at all.
        _two_model_ledger(tmp_path / "run", days=5)
        record = next(iter(replay(tmp_path / "run", ("good", "bad"))))
        assert record["model"] == ENSEMBLE_MODEL_NAME
        assert record["record_type"] == "score"
        for field in ("ticker", "origin_date", "target_date", "point", "quantiles", "actual", "score"):
            assert field in record, field
        # Repairs are counted, never silent -- a per-component blend really
        # can need one, so the count travels with the record.
        assert set(record["repairs"]) == {"quantile_crossings", "bar_repairs"}
        assert set(record["score"]["pinball"]) == set(COMPONENTS)

    def test_replay_refuses_duplicate_model_names(self, tmp_path):
        # replay() calls a pair shared by every model via
        # len(entries) == len(models), and a pooled entry can only ever hold
        # one key per distinct name -- so ("good", "good") skipped every pair
        # and yielded NOTHING rather than refusing (Copilot review
        # regression). Verified before the fix: ("good",) gave 10 records on
        # this ledger, ("good", "good") gave 0.
        _two_model_ledger(tmp_path / "run", days=10)
        assert list(replay(tmp_path / "run", ("good", "bad")))
        with pytest.raises(ValueError, match="REFUSED: .*duplicate model name"):
            list(replay(tmp_path / "run", ("good", "good")))

    def test_an_unknown_model_in_the_ledger_refuses(self, tmp_path):
        _two_model_ledger(tmp_path / "run", days=3)
        with pytest.raises(ValueError, match="REFUSED"):
            list(replay(tmp_path / "run", ("good", "missing")))


class TestReplayStreaming:
    """Memory bounded by one year, not by the run."""

    def _multi_year_ledger(self, run_dir, spans):
        records = []
        for origin, target in spans:
            for model, error in (("good", 0.001), ("bad", 0.030)):
                records.append(
                    _score_record(model=model, ticker="AAA", origin_date=origin,
                                  target_date=target, predicted=error, actual=0.0)
                )
        _write_ledger(run_dir, records)

    def test_replay_reads_one_year_shard_at_a_time(self, tmp_path, monkeypatch):
        # A full-universe multi-decade run is millions of records; reading
        # them all up front made peak memory scale with the whole replay
        # (Antigravity review regression). The ledger already shards by the
        # origin year, so every read must name a year.
        run_dir = tmp_path / "run"
        self._multi_year_ledger(
            run_dir,
            [("2019-06-03", "2019-06-04"), ("2020-06-02", "2020-06-03"), ("2021-06-01", "2021-06-02")],
        )
        calls = []
        original = Ledger.iter_records

        def spy(self, *, model, kind, year=None):
            calls.append((model, kind, year))
            return original(self, model=model, kind=kind, year=year)

        monkeypatch.setattr(Ledger, "iter_records", spy)
        assert list(replay(run_dir, ("good", "bad")))
        assert calls, "iter_records was never called"
        assert all(year is not None for _, _, year in calls), calls
        assert {year for _, _, year in calls} == {"2019", "2020", "2021"}

    def test_a_target_crossing_the_new_year_is_emitted_exactly_once(self, tmp_path):
        # A forecast originating on the last trading day of a year matures
        # in the NEXT year, so it sits in year Y's shard with a year Y+1
        # target. It has to survive the carry-over rather than being
        # dropped at the boundary or emitted twice.
        run_dir = tmp_path / "run"
        self._multi_year_ledger(
            run_dir,
            [("2020-12-30", "2020-12-31"), ("2020-12-31", "2021-01-04"), ("2021-01-04", "2021-01-05")],
        )
        records = list(replay(run_dir, ("good", "bad")))
        crossing = [r for r in records if r["origin_date"] == "2020-12-31"]
        assert len(crossing) == 1
        assert crossing[0]["target_date"] == "2021-01-04"

    def test_a_crossing_target_survives_when_no_later_shard_exists(self, tmp_path):
        # The trailing flush: the run ends with a forecast whose target is
        # in a year that has no shard of its own, so nothing after the
        # per-year loop would emit it.
        run_dir = tmp_path / "run"
        self._multi_year_ledger(run_dir, [("2020-12-30", "2020-12-31"), ("2020-12-31", "2021-01-04")])
        targets = [record["target_date"] for record in replay(run_dir, ("good", "bad"))]
        assert targets == ["2020-12-31", "2021-01-04"]

    def test_replay_refuses_when_a_models_shards_are_all_post_checkpoint(self, tmp_path):
        # Discovery and reading have to expose the SAME checkpoint-bounded
        # view. When they did not, a shard created entirely after the last
        # checkpoint was reported by available_years(), passed replay()'s
        # "no score records" guard, and then yielded nothing through
        # iter_records() -- silently producing no ensemble records instead
        # of refusing a model with no durable scores (Copilot review
        # regression).
        run_dir = tmp_path / "run"
        self._multi_year_ledger(run_dir, [("2020-06-02", "2020-06-03")])
        with pytest.raises(ValueError, match="REFUSED: no score records"):
            list(replay(run_dir, ("good", "bad"), read_fingerprints={}))

    def test_multi_year_replay_stays_in_target_date_order(self, tmp_path):
        # Order across the shard boundary is what keeps the weights
        # walk-forward: a day blended out of order would be blended with
        # weights that had already seen it.
        run_dir = tmp_path / "run"
        spans = [(f"{year}-06-0{day}", f"{year}-06-0{day + 1}") for year in (2019, 2020, 2021) for day in (1, 2, 3)]
        self._multi_year_ledger(run_dir, spans)
        targets = [record["target_date"] for record in replay(run_dir, ("good", "bad"))]
        assert targets == sorted(targets)
        assert len(targets) == len(spans)


class TestEnsembleValue:
    def test_the_blend_beats_both_components_when_their_errors_cancel(self, tmp_path):
        # The case an ensemble exists for, constructed so the result is
        # provable rather than seed-dependent: one model is biased +0.02,
        # the other -0.02, and the equal blend is exact.
        run_dir = tmp_path / "run"
        records = []
        for day in range(30):
            origin, target = _iso(day), _iso(day + 1)
            for model, error in (("high_bias", 0.02), ("low_bias", -0.02)):
                records.append(
                    _score_record(model=model, ticker="AAA", origin_date=origin,
                                  target_date=target, predicted=error, actual=0.0)
                )
        _write_ledger(run_dir, records)
        blended = list(replay(run_dir, ("high_bias", "low_bias")))
        for record in blended:
            assert abs(record["score"]["abs_err"]["close"]) < 1e-9
