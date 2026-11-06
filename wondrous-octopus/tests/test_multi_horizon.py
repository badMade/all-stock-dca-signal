"""Multi-horizon (t+1..t+H) forecasting: embargo, all-horizon output, per-horizon calibration.

All synthetic and hermetic. The three load-bearing properties:

- the horizon-h embargo: a horizon-h target is knowable only once row t+h has
  been revealed, and nothing after the revealed-through date can move it;
- every horizon 1..H is emitted (including the full 90);
- calibration, metrics and the ledger are per horizon, and a resumed run is
  byte-identical to an uninterrupted one.
"""

from __future__ import annotations

import json
from typing import Any

import baseline_model
import features as _features
import harness as harness_module
import horizon_tracker
import horizons
import metrics
import model_interface
import numpy as np
import pandas as pd
import pytest
from data import COMPONENTS
from harness import HarnessConfig, HarnessError, WalkForwardHarness
from ledger import Ledger

features: Any = _features
MAX_H = 12
EVAL = (3, 6)


def _quiet(_message: str) -> None:
    pass


@pytest.fixture
def panel(synthetic_history_dir):
    probe = features.build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[150])
    return features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


def _config(panel, *, n_steps=None, **overrides):
    start = np.datetime_as_string(panel.calendar[151], unit="D")
    end = None
    if n_steps is not None:
        end = np.datetime_as_string(panel.calendar[151 + n_steps - 1], unit="D")
    defaults: dict[str, Any] = dict(
        start=start,
        end=end,
        initial_train_end=np.datetime_as_string(panel.calendar[150], unit="D"),
        models=("persistence",),
        window_len=16,
        min_history=60,
        checkpoint_every=6,
        diagnostic_sample=10,
        seed=0,
        max_horizon=MAX_H,
        eval_horizons=EVAL,
    )
    defaults.update(overrides)
    return HarnessConfig(**defaults)


def _models(config):
    return {name: baseline_model.PersistenceModel() for name in config.models}


def _horizon_records(run_dir, model="persistence"):
    return list(Ledger(run_dir).iter_records(model=model, kind="horizon_score"))


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


class TestHorizonConfig:
    def test_legacy_default_and_default_checkpoints(self):
        assert horizons.validate_horizons(1, ()) == (1, ())
        assert horizons.validate_horizons(90, ()) == (90, (5, 10, 15, 20))
        assert horizons.validate_horizons(12, ()) == (12, (5, 10))
        assert horizons.validate_horizons(3, ()) == (3, (3,))
        assert horizons.validate_horizons(90, (20, 5, 5, 40)) == (90, (5, 20, 40))

    @pytest.mark.parametrize(
        "max_horizon, evals",
        [(0, ()), (253, ()), (1, (5,)), (10, (1,)), (10, (11,)), (10, (0,)), (True, ())],
    )
    def test_invalid_config_is_refused(self, max_horizon, evals):
        with pytest.raises(ValueError):
            horizons.validate_horizons(max_horizon, evals)

    def test_harness_config_hash_is_unchanged_for_the_legacy_default(self):
        config = HarnessConfig(start="2020-01-01")
        payload = {
            key: value
            for key, value in config.as_dict().items()
            if key not in harness_module._RESUME_SAFE_FIELDS and key not in ("max_horizon", "eval_horizons")
        }
        import hashlib

        legacy = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        assert config.config_hash() == legacy

    def test_horizon_fields_enter_the_hash_when_enabled(self):
        base = HarnessConfig(start="2020-01-01", max_horizon=10, eval_horizons=(5,))
        assert base.config_hash() != HarnessConfig(start="2020-01-01").config_hash()
        assert base.config_hash() != HarnessConfig(start="2020-01-01", max_horizon=10, eval_horizons=(6,)).config_hash()
        assert base.config_hash() != HarnessConfig(start="2020-01-01", max_horizon=11, eval_horizons=(5,)).config_hash()

    def test_json_roundtrip_equals_original(self):
        config = HarnessConfig(start="2020-01-01", max_horizon=10, eval_horizons=(5, 3))
        stored = json.loads(json.dumps(config.as_dict()))
        stored["models"] = tuple(stored["models"])
        assert HarnessConfig(**stored).config_hash() == config.config_hash()


# --------------------------------------------------------------------------
# The embargo
# --------------------------------------------------------------------------


class TestEmbargo:
    def test_horizon_one_is_the_native_target(self, panel):
        series = panel.series["AAA"]
        row = 100
        out = horizons.horizon_targets(series, row, 5, series.dates[-1])
        assert np.allclose(out[0], series.targets[row])

    def test_close_target_is_the_h_day_log_return(self, panel):
        series = panel.series["AAA"]
        row = 100
        out = horizons.horizon_targets(series, row, 8, series.dates[-1])
        close = COMPONENT_CLOSE = 3
        for h in range(1, 9):
            expected = np.log(series.ref_close[row + h] / series.ref_close[row])
            assert out[h - 1, close] == pytest.approx(expected, abs=2e-6), (h, COMPONENT_CLOSE)

    def test_horizon_h_unlocks_exactly_on_the_day_t_plus_h_is_revealed(self, panel):
        series = panel.series["AAA"]
        row = 100
        for h in (1, 2, 5, 9):
            reveal = series.dates[row + h]
            day_before = reveal - np.timedelta64(1, "D")
            assert horizons.usable_horizon_count(series, row, day_before, 12) == h - 1
            assert horizons.usable_horizon_count(series, row, reveal, 12) == h
            out = horizons.horizon_targets(series, row, 12, day_before)
            assert np.isfinite(out[: h - 1]).all()
            assert np.isnan(out[h - 1 :]).all()

    def test_nothing_after_asof_can_move_a_usable_target(self, panel):
        """The embargo is structural: corrupt every bar after asof and the usable targets don't move."""
        series = panel.series["AAA"]
        row, asof_row = 100, 106
        asof = series.dates[asof_row]
        before = horizons.horizon_targets(series, row, 12, asof).copy()
        corrupted = type(series)(**{**series.__dict__})
        corrupted.targets = series.targets.copy()
        corrupted.ref_close = series.ref_close.copy()
        corrupted.targets[asof_row:] = 99.0  # row asof_row's target is bar asof_row+1: post-asof
        corrupted.ref_close[asof_row + 1 :] = 1e9
        after = horizons.horizon_targets(corrupted, row, 12, asof)
        usable = horizons.usable_horizon_count(series, row, asof, 12)
        assert usable == asof_row - row
        assert np.array_equal(before[:usable], after[:usable])
        assert np.isnan(after[usable:]).all()

    def test_no_asof_is_not_an_unbounded_default(self, panel):
        with pytest.raises(ValueError):
            horizons.usable_horizon_count(panel.series["AAA"], 100, None, 5)

    def test_embargo_mask_is_a_prefix(self, panel):
        series = panel.series["AAA"]
        mask = horizons.horizon_embargo_mask(series, [100, 101, 110], series.dates[108], 12)
        assert mask.sum(axis=1).tolist() == [8, 7, 0]
        for row in mask:
            assert list(row) == sorted(row, reverse=True)

    def test_row_past_the_end_has_no_horizons(self, panel):
        series = panel.series["AAA"]
        last = len(series.dates) - 1
        assert horizons.usable_horizon_count(series, last, series.dates[-1], 5) == 0
        assert np.isnan(horizons.horizon_targets(series, last, 5, series.dates[-1])).all()


# --------------------------------------------------------------------------
# Per-horizon calibration
# --------------------------------------------------------------------------


def _obs(width, actual, n=10):
    band = {c: {"p10": -width, "p50": 0.0, "p90": width} for c in COMPONENTS}
    return [(band, {c: actual for c in COMPONENTS})] * n


class TestHorizonCalibration:
    def test_each_horizon_learns_only_from_its_own_scores(self):
        calibrator = model_interface.HorizonConformalCalibrator((5, 20))
        for day in range(30):
            calibrator.observe_batch(5, _obs(0.01, 1.0), target_date=f"2020-02-{day + 1:02d}")  # always misses
        assert calibrator.calibrators[5].scales["close"] > 1.2
        assert calibrator.calibrators[20].scales["close"] == 1.0

    def test_scales_widen_on_miss_and_narrow_on_cover(self):
        calibrator = model_interface.HorizonConformalCalibrator((5, 20))
        for day in range(40):
            calibrator.observe_batch(5, _obs(0.01, 1.0), target_date=f"2020-03-{day + 1:02d}")
            calibrator.observe_batch(20, _obs(0.01, 0.0), target_date=f"2020-03-{day + 1:02d}")
        assert calibrator.scales_for(5)[3] > 1.0 > calibrator.scales_for(20)[3]

    def test_off_checkpoint_horizons_interpolate_geometrically_and_clamp(self):
        calibrator = model_interface.HorizonConformalCalibrator((5, 20))
        calibrator.calibrators[5].scales = {c: 1.0 for c in COMPONENTS}
        calibrator.calibrators[20].scales = {c: 4.0 for c in COMPONENTS}
        assert calibrator.scales_for(1)[0] == 1.0  # clamped below the first checkpoint
        assert calibrator.scales_for(5)[0] == 1.0
        assert calibrator.scales_for(20)[0] == 4.0
        assert calibrator.scales_for(90)[0] == 4.0  # clamped above the last
        middle = calibrator.scales_for(10)[0]  # log-midpoint of 5 and 20 -> geometric mean
        assert middle == pytest.approx(2.0)
        matrix = calibrator.scales_matrix(30)
        assert matrix.shape == (30, 4)
        assert (np.diff(matrix[:, 0]) >= -1e-12).all()  # monotone between monotone checkpoints

    def test_watermark_is_per_horizon(self):
        calibrator = model_interface.HorizonConformalCalibrator((5, 20))
        calibrator.observe_batch(5, _obs(0.01, 1.0), target_date="2020-01-10")
        once = calibrator.calibrators[5].scales["close"]
        calibrator.observe_batch(5, _obs(0.01, 1.0), target_date="2020-01-10")  # same day: no-op
        assert calibrator.calibrators[5].scales["close"] == once
        calibrator.observe_batch(20, _obs(0.01, 1.0), target_date="2020-01-10")  # other horizon, same day: applied
        assert calibrator.calibrators[20].scales["close"] > 1.0

    def test_state_roundtrip(self):
        calibrator = model_interface.HorizonConformalCalibrator((5, 20))
        calibrator.observe_batch(5, _obs(0.01, 1.0), target_date="2020-01-10")
        restored = model_interface.HorizonConformalCalibrator.from_state(json.loads(json.dumps(calibrator.to_state())))
        assert np.array_equal(restored.scales_matrix(30), calibrator.scales_matrix(30))
        assert restored.calibrators[5].last_calibrated_target == "2020-01-10"

    def test_apply_scales_around_the_median_and_leaves_it_alone(self):
        q = np.array([[[[-0.02, 0.0, 0.03]] * 4] * 3])  # (1, 3, 4, 3)
        scales = np.array([[1.0] * 4, [2.0] * 4, [0.5] * 4])
        out = model_interface.HorizonConformalCalibrator.apply(q, scales)
        assert np.allclose(out[0, 1, 0], [-0.04, 0.0, 0.06])
        assert np.allclose(out[0, 2, 0], [-0.01, 0.0, 0.015])
        assert np.allclose(out[..., 1], q[..., 1])

    def test_repair_horizon_arrays_orders_quantiles_and_fixes_bars(self):
        point = np.array([[[0.0, -0.01, 0.0, 0.02]]])  # high below close
        quantiles = np.array([[[[0.03, 0.0, -0.03]] * 4]])  # crossed
        fixed_point, fixed_q, counts = model_interface.repair_horizon_arrays(point, quantiles)
        assert (np.diff(fixed_q, axis=-1) >= 0).all()
        assert fixed_point[0, 0, 1] >= fixed_point[0, 0, 3]
        assert counts.quantile_crossings == 4 and counts.bar_repairs >= 1


# --------------------------------------------------------------------------
# Persistence's multi-horizon yardstick
# --------------------------------------------------------------------------


class TestPersistenceHorizons:
    def test_emits_every_horizon_and_widens_with_sqrt_h(self, panel):
        config = _config(panel, n_steps=3)
        model = baseline_model.PersistenceModel()
        model.fit(panel, seed=0)
        requests = [model_interface.make_request(panel.series["AAA"], 120, 16)]
        (one,) = model.predict(requests)
        (forecast,) = model.predict_horizons(requests, max_horizon=90)
        assert forecast.point.shape == (90, 4) and forecast.quantiles.shape == (90, 4, 3)
        assert np.isfinite(forecast.point).all() and np.isfinite(forecast.quantiles).all()
        # horizon 1 is exactly the native forecast
        assert forecast.quantiles[0, 3, 0] == pytest.approx(one.quantiles["close"]["p10"])
        width = forecast.quantiles[:, 3, 2] - forecast.quantiles[:, 3, 0]
        assert width[89] / width[0] == pytest.approx(np.sqrt(90), rel=1e-6)
        assert config.max_horizon == MAX_H  # config fixture unaffected


# --------------------------------------------------------------------------
# The harness end to end
# --------------------------------------------------------------------------


class TestHarnessMultiHorizon:
    def test_scores_land_only_at_evaluation_horizons_on_the_right_day(self, panel, tmp_path):
        config = _config(panel, n_steps=14)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        assert harness.run() == "completed"
        scored = _horizon_records(tmp_path / "run")
        assert scored, "no horizon scores were ledgered"
        assert {record["horizon"] for record in scored} == set(EVAL)
        for record in scored:
            series = panel.series[record["ticker"]]
            origin = int(np.flatnonzero(series.dates.astype("datetime64[D]") == np.datetime64(record["origin_date"]))[0])
            # the target is the ticker's own h-th next row ...
            assert record["target_date"] == str(series.dates[origin + record["horizon"]].astype("datetime64[D]"))
            # ... and the realized close is the h-day log return, never an earlier or later one
            expected = np.log(series.ref_close[origin + record["horizon"]] / series.ref_close[origin])
            assert record["actual"]["close"] == pytest.approx(float(expected), abs=2e-6)
            assert record["target_date"] > record["origin_date"]

    def test_no_score_is_ledgered_before_its_target_day_is_reached(self, panel, tmp_path):
        """A run cut after N steps has scored no horizon whose target is beyond its last step."""
        config = _config(panel, n_steps=8)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        last_step_date = np.datetime_as_string(panel.calendar[151 + 8 - 1], unit="D")
        for record in _horizon_records(tmp_path / "run"):
            assert record["target_date"] <= last_step_date

    def test_per_horizon_calibrators_diverge_and_state_checkpoints(self, panel, tmp_path):
        config = _config(panel, n_steps=24)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        calibrator = harness.horizon_tracker.calibrators["persistence"]
        fingerprints = {h: tuple(calibrator.calibrators[h].scales.values()) for h in EVAL}
        assert fingerprints[3] != fingerprints[6]
        checkpoint = tmp_path / "run" / "checkpoint"
        state = json.loads((checkpoint / "harness_state.json").read_bytes())
        assert state["horizon"]["eval_horizons"] == list(EVAL)
        with np.load(checkpoint / horizon_tracker.PENDING_FILENAME, allow_pickle=False) as archive:
            assert all(archive[name].dtype != object for name in archive.files)

    def test_full_horizon_export_has_every_horizon(self, panel, tmp_path):
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        frame = pd.read_csv(tmp_path / "run" / "horizons" / "persistence.csv")
        assert sorted(frame["horizon"].unique()) == list(range(1, MAX_H + 1))
        assert {"close_p10", "close_p50", "close_p90", "open_point"} <= set(frame.columns)
        assert (frame["close_p10"] <= frame["close_p50"]).all() and (frame["close_p50"] <= frame["close_p90"]).all()

    def test_resume_equals_uninterrupted(self, panel, tmp_path):
        full = _config(panel, n_steps=14)
        straight = WalkForwardHarness.fresh(panel, _models(full), tmp_path / "straight", full, log=_quiet)
        assert straight.run() == "completed"
        half = _config(panel, n_steps=7)
        first = WalkForwardHarness.fresh(panel, _models(half), tmp_path / "resumed", half, log=_quiet)
        assert first.run() == "completed"
        registry = {"persistence": baseline_model.PersistenceModel}
        second = WalkForwardHarness.resume(panel, registry, tmp_path / "resumed", full, log=_quiet)
        assert second.run() == "completed"
        assert _horizon_records(tmp_path / "straight") == _horizon_records(tmp_path / "resumed")
        for kind in ("score", "horizon_score"):
            a = sorted((tmp_path / "straight" / "ledger" / "persistence").glob(f"{kind}_*"))
            b = sorted((tmp_path / "resumed" / "ledger" / "persistence").glob(f"{kind}_*"))
            assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]
        calibrators = lambda h: h.horizon_tracker.calibrators["persistence"].to_state()  # noqa: E731
        assert calibrators(straight) == calibrators(second)

    @pytest.mark.parametrize(
        "override", [{"max_horizon": MAX_H + 1}, {"eval_horizons": (3, 7)}, {"eval_horizons": (3,)}]
    )
    def test_resume_refuses_changed_horizon_config(self, panel, tmp_path, override):
        half = _config(panel, n_steps=5)
        WalkForwardHarness.fresh(panel, _models(half), tmp_path / "run", half, log=_quiet).run()
        changed = _config(panel, n_steps=5, **override)
        with pytest.raises(HarnessError, match="REFUSED"):
            WalkForwardHarness.resume(
                panel, {"persistence": baseline_model.PersistenceModel}, tmp_path / "run", changed, log=_quiet
            )

    def test_resume_refuses_turning_horizons_on_or_off(self, panel, tmp_path):
        legacy = _config(panel, n_steps=5, max_horizon=1, eval_horizons=())
        WalkForwardHarness.fresh(panel, _models(legacy), tmp_path / "run", legacy, log=_quiet).run()
        with pytest.raises(HarnessError, match="REFUSED"):
            WalkForwardHarness.resume(
                panel,
                {"persistence": baseline_model.PersistenceModel},
                tmp_path / "run",
                _config(panel, n_steps=5),
                log=_quiet,
            )

    def test_resume_refuses_a_tampered_pending_file(self, panel, tmp_path):
        half = _config(panel, n_steps=5)
        WalkForwardHarness.fresh(panel, _models(half), tmp_path / "run", half, log=_quiet).run()
        path = tmp_path / "run" / "checkpoint" / horizon_tracker.PENDING_FILENAME
        path.write_bytes(path.read_bytes() + b"\0")
        with pytest.raises(HarnessError, match="REFUSED"):
            WalkForwardHarness.resume(
                panel, {"persistence": baseline_model.PersistenceModel}, tmp_path / "run", half, log=_quiet
            )

    def test_a_model_without_horizon_support_alone_is_refused(self, panel, tmp_path):
        config = _config(panel, n_steps=3, models=("gbm_quantile",))
        models = {"gbm_quantile": baseline_model.GbmQuantileModel(max_iter=5)}
        with pytest.raises(HarnessError, match="REFUSED"):
            WalkForwardHarness.fresh(panel, models, tmp_path / "run", config, log=_quiet)

    def test_legacy_run_writes_no_horizon_artifacts(self, panel, tmp_path):
        config = _config(panel, n_steps=4, max_horizon=1, eval_horizons=())
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        assert harness.horizon_tracker is None
        assert not (tmp_path / "run" / "horizons").exists()
        assert not (tmp_path / "run" / "checkpoint" / horizon_tracker.PENDING_FILENAME).exists()
        assert "horizon" not in json.loads((tmp_path / "run" / "checkpoint" / "harness_state.json").read_bytes())


# --------------------------------------------------------------------------
# Per-horizon metrics
# --------------------------------------------------------------------------


class TestHorizonMetrics:
    def test_error_is_visible_by_horizon_and_year(self, panel, tmp_path):
        config = _config(panel, n_steps=20)
        WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet).run()
        ledger = Ledger(tmp_path / "run")

        def stream():
            yield from ledger.iter_records(model="persistence", kind="score")
            yield from ledger.iter_records(model="persistence", kind="horizon_score")

        report = metrics.build_horizon_report({"persistence": stream})
        by_horizon = report["by_horizon"]["persistence"]
        assert sorted(by_horizon, key=int) == ["1", "3", "6"]
        for horizon in by_horizon:
            assert by_horizon[horizon]["close"]["n"] > 0
            assert set(by_horizon[horizon]) == set(COMPONENTS)
        assert report["by_horizon_over_time"]["persistence"]["3"]
        text = "\n".join(metrics.render_horizon_markdown(report))
        assert "Error by forecast horizon" in text

    def test_skill_is_measured_against_persistence_at_the_same_horizon(self):
        def record(model, horizon, err):
            band = {c: {"p10": -1.0, "p50": 0.0, "p90": 1.0} for c in COMPONENTS}
            score = metrics.score_forecast(
                point={c: err for c in COMPONENTS},
                quantiles=band,
                quantiles_raw=band,
                actual={c: 0.0 for c in COMPONENTS},
                ref_close=100.0,
            )
            return {
                "model": model,
                "ticker": "AAA",
                "origin_date": "2020-01-02",
                "target_date": "2020-02-03",
                "horizon": horizon,
                "score": score,
            }

        streams = {
            "persistence": lambda: [record("persistence", 5, 0.04), record("persistence", 10, 0.08)],
            "octopus": lambda: [record("octopus", 5, 0.02), record("octopus", 10, 0.08)],
        }
        skill = metrics.build_horizon_report(streams)["skill_vs_persistence_by_horizon"]["octopus"]
        assert skill["5"]["close"] == pytest.approx(0.5)
        assert skill["10"]["close"] == pytest.approx(0.0)

    def test_horizon_one_with_no_horizon_key_counts_as_horizon_one(self):
        band = {c: {"p10": -1.0, "p50": 0.0, "p90": 1.0} for c in COMPONENTS}
        score = metrics.score_forecast(
            point={c: 0.0 for c in COMPONENTS},
            quantiles=band,
            quantiles_raw=band,
            actual={c: 0.0 for c in COMPONENTS},
            ref_close=100.0,
        )
        rec = {"model": "m", "ticker": "A", "origin_date": "2020-01-02", "target_date": "2020-01-03", "score": score}
        report = metrics.build_horizon_report({"m": lambda: [rec]}, intersect=False)
        assert list(report["by_horizon"]["m"]) == ["1"]
