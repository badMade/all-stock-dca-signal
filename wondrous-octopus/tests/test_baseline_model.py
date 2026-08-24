"""Tests for baseline_model.py: persistence bands, GBM refit-on-load."""

from __future__ import annotations

import baseline_model
import features
import model_interface
import numpy as np
import pandas as pd
import pytest
from data import COMPONENTS


@pytest.fixture
def panel(synthetic_history_dir):
    # Freeze normalization/fit window at ~60% through the archive so both
    # "initial fit" and "later reveals" exist.
    probe = features.build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[int(len(probe.calendar) * 0.6)])
    return features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


def _requests(panel, ticker, rows, window_len=32):
    series = panel.series[ticker]
    return [model_interface.make_request(series, row, window_len) for row in rows]


class TestPersistenceModel:
    def test_point_is_todays_bar_vs_todays_close(self, panel):
        model = baseline_model.PersistenceModel()
        model.fit(panel, seed=0)
        series = panel.series["AAA"]
        row = 200
        [forecast] = model.predict(_requests(panel, "AAA", [row]))
        # ln(High_t / Close_t) reconstructed from the features must match the
        # point 'high' forecast (modulo the shared bar repair).
        ret_high = series.features[row, features.FEATURE_NAMES.index("ret_high")]
        ret_close = series.features[row, features.FEATURE_NAMES.index("ret_close")]
        assert forecast.point["high"] == pytest.approx(float(ret_high - ret_close), abs=1e-6)
        assert forecast.point["close"] == 0.0

    def test_band_coverage_near_nominal_on_gaussian_residuals(self):
        # Feed the pool with gaussian residuals; the p10-p90 band built from
        # them must cover ~80% of fresh draws from the same distribution.
        model = baseline_model.PersistenceModel()
        rng = np.random.default_rng(0)
        sigma = 0.02
        for value in rng.normal(0.0, sigma, size=2000):
            residual = np.full(4, value, dtype=np.float32)
            model._pool.append(residual)
        point = {component: 0.0 for component in COMPONENTS}
        bands = model._bands("UNSEEN", point)
        fresh = rng.normal(0.0, sigma, size=5000)
        covered = np.mean((fresh >= bands["close"]["p10"]) & (fresh <= bands["close"]["p90"]))
        assert covered == pytest.approx(0.8, abs=0.03)

    def test_update_appends_residuals(self, panel):
        model = baseline_model.PersistenceModel()
        model.fit(panel, seed=0)
        series = panel.series["AAA"]
        before = len(model._buffer("AAA"))
        revealed = [model_interface.make_revealed(series, 210, 32)]
        model.update(revealed, step_index=1)
        assert len(model._buffer("AAA")) == before + 1

    def test_refit_replaces_state_instead_of_duplicating_residuals(self, panel):
        model = baseline_model.PersistenceModel()
        model.fit(panel, seed=0)
        first_pool = len(model._pool)
        first_aaa = len(model._buffer("AAA"))
        model.fit(panel, seed=0)  # recovery-retrain path calls fit again
        assert len(model._pool) == first_pool
        assert len(model._buffer("AAA")) == first_aaa

    def test_pool_sampling_is_not_biased_toward_alphabetically_last_tickers(self, panel):
        # Appending ticker-by-ticker into a bounded deque would silently
        # keep only the last few alphabetical tickers' residuals once the
        # archive holds more candidates than pool_window (Codex review
        # regression: "Populate the pooled residual window without
        # ticker-order bias"). AAA is alphabetically first and would be
        # evicted entirely by that bug.
        model = baseline_model.PersistenceModel(pool_window=50)
        model.fit(panel, seed=0)
        assert len(model._pool) == 50
        aaa_residuals = {tuple(residual) for residual in model._buffer("AAA")}
        pool_residuals = {tuple(residual) for residual in model._pool}
        assert aaa_residuals & pool_residuals

    def test_update_pool_sampling_is_not_biased_toward_alphabetically_last_tickers(self):
        # update()'s pool insertion must not silently keep only the LAST
        # pool_window entries once a single day's batch (the harness
        # supplies `revealed` ticker-sorted) exceeds pool_window --
        # reintroducing the exact ordering bias fit()'s uniform sampling
        # already avoids (Codex review regression: "Sample online pooled
        # residuals without ticker-order bias"). Low-index tickers are
        # alphabetically first and would be evicted entirely by the old
        # ordered-append bug.
        model = baseline_model.PersistenceModel(pool_window=50)
        n = 200
        window = np.zeros((4, len(features.FEATURE_NAMES)), dtype=np.float32)
        mask = np.array([False, False, False, True])
        dates = np.array([np.datetime64("2021-01-01")] * 4, dtype="datetime64[ns]")
        revealed = [
            model_interface.RevealedDay(
                ticker=f"T{index:04d}",  # already ticker-sorted, like the harness's own batch
                ticker_id=index,
                origin_date="2021-06-01",
                target_date="2021-06-02",
                # feature_row is all zeros, so _persistence_point is all
                # zeros too, and the residual becomes `actual` verbatim --
                # "close" directly identifies which index survived.
                actual={"open": 0.0, "high": 0.0, "low": 0.0, "close": float(index)},
                features_window=window,
                window_mask=mask,
                day_of_week=np.zeros(4, dtype=np.int8),
                window_dates=dates,
                ref_close=100.0,
            )
            for index in range(n)
        ]
        model.update(revealed, step_index=1)
        assert len(model._pool) == 50
        survivor_indices = {int(residual[3]) for residual in model._pool}
        # Under correct uniform reservoir sampling, the probability that
        # every one of 50 survivors comes from the last 50 of 200 offered
        # is astronomically small; the old ordered-append bug made it
        # certain.
        assert any(index < 150 for index in survivor_indices)

    def test_update_pool_seen_survives_save_load_roundtrip(self, panel, tmp_path):
        # A resume must continue the SAME reservoir (correct replacement
        # probabilities), not one reset to zero -- checkpointed alongside
        # the pool contents themselves.
        model = baseline_model.PersistenceModel(pool_window=50)
        model.fit(panel, seed=0)
        before = model._pool_seen
        assert before >= 50
        model.save(tmp_path / "persistence")
        restored = baseline_model.PersistenceModel.load(tmp_path / "persistence")
        assert restored._pool_seen == before

    def test_save_load_roundtrip_preserves_bands(self, panel, tmp_path):
        model = baseline_model.PersistenceModel()
        model.fit(panel, seed=0)
        requests = _requests(panel, "BBB", [190, 200])
        original = model.predict(requests)
        model.save(tmp_path / "persistence")
        restored = baseline_model.PersistenceModel.load(tmp_path / "persistence")
        reloaded = restored.predict(requests)
        for a, b in zip(original, reloaded, strict=True):
            assert a.point == b.point
            assert a.quantiles == b.quantiles


class TestGbmQuantileModel:
    def test_fit_predict_produces_ordered_bands(self, panel):
        model = baseline_model.GbmQuantileModel(max_iter=15)
        stats = model.fit(panel, seed=0)
        assert stats["training_rows"] > 100
        forecasts = model.predict(_requests(panel, "AAA", [200, 210]))
        for forecast in forecasts:
            for component in COMPONENTS:
                band = forecast.quantiles[component]
                assert band["p10"] <= band["p50"] <= band["p90"]

    def test_fit_with_step_index_anchors_the_refit_cadence(self, panel):
        # A recovery retrain calls fit() again without resetting the
        # harness's absolute step_index; fit() must anchor
        # _last_refit_step to the CALLER's step_index (not hardcode 0), or
        # the very next update() immediately forces another full refit
        # right after recovery already refit through the cursor (Codex
        # review regression: "Preserve the GBM refit cadence after
        # recovery retraining").
        model = baseline_model.GbmQuantileModel(refit_every=21, max_iter=15)
        model.fit(panel, seed=0, step_index=5000)
        assert model._last_refit_step == 5000
        series = panel.series["AAA"]
        revealed = [model_interface.make_revealed(series, 220, 32)]
        result = model.update(revealed, step_index=5005)  # only 5 steps since the retrain
        assert result["refit"] is False

    def test_scheduled_refit_fires_on_cadence(self, panel):
        model = baseline_model.GbmQuantileModel(refit_every=5, max_iter=15)
        model.fit(panel, seed=0)
        series = panel.series["AAA"]
        revealed = [model_interface.make_revealed(series, 220, 32)]
        assert model.update(revealed, step_index=3)["refit"] is False
        result = model.update(revealed, step_index=5)
        assert result["refit"] is True
        assert model._last_refit_step == 5

    def test_refit_on_load_reproduces_predictions_exactly(self, panel, tmp_path):
        model = baseline_model.GbmQuantileModel(max_iter=15)
        model.fit(panel, seed=7)
        requests = _requests(panel, "ZZZ", [200, 215])
        original = model.predict(requests)
        model.save(tmp_path / "gbm")
        restored = baseline_model.GbmQuantileModel.load(tmp_path / "gbm", panel_provider=lambda: panel)
        reloaded = restored.predict(requests)
        for a, b in zip(original, reloaded, strict=True):
            assert a.point == b.point
            assert a.quantiles == b.quantiles

    def test_load_without_panel_provider_is_refused(self, panel, tmp_path):
        model = baseline_model.GbmQuantileModel(max_iter=15)
        model.fit(panel, seed=0)
        model.save(tmp_path / "gbm")
        with pytest.raises(ValueError, match="panel_provider"):
            baseline_model.GbmQuantileModel.load(tmp_path / "gbm")

    def test_load_refuses_when_panel_changed_since_checkpoint(self, synthetic_history_dir, tmp_path):
        # Persistence and Octopus restore their EXACT saved state; GBM
        # instead refits on load, so a changed panel (an archive correction)
        # must not silently become a different model before any new day is
        # processed (Codex review regression: "Detect training-data changes
        # before refitting GBM checkpoints").
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        cutoff = pd.Timestamp(probe.calendar[int(len(probe.calendar) * 0.6)])
        panel_before = features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)
        model = baseline_model.GbmQuantileModel(max_iter=15)
        model.fit(panel_before, seed=0)
        model.save(tmp_path / "gbm")

        # Simulate an archive correction: perturb one value well inside the
        # fit window (past the rolling-feature warm-up, before the cutoff).
        path = synthetic_history_dir / "AAA.csv"
        lines = path.read_text().splitlines()
        header = lines[0].split(",")
        close_index = header.index("Close")
        row_cells = lines[110].split(",")
        row_cells[close_index] = str(float(row_cells[close_index]) * 1.001)
        lines[110] = ",".join(row_cells)
        path.write_text("\n".join(lines) + "\n")

        panel_after = features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)
        with pytest.raises(baseline_model.TrainingDataChanged, match="DIFFERENT"):
            baseline_model.GbmQuantileModel.load(tmp_path / "gbm", panel_provider=lambda: panel_after)
        # strict=False (an explicit --resume --retrain) accepts the refit as
        # a stepping stone instead -- retrain() immediately refits anyway.
        restored = baseline_model.GbmQuantileModel.load(
            tmp_path / "gbm", panel_provider=lambda: panel_after, strict=False
        )
        assert restored is not None

    def test_load_fails_fast_before_fitting_estimators_on_a_mismatch(
        self, synthetic_history_dir, tmp_path, monkeypatch
    ):
        # A strict load against a changed archive must refuse BEFORE
        # spending the full refit -- fitting all 16 HistGradientBoosting
        # estimators, up to row_cap=500k rows in a real run -- not after
        # (Antigravity review regression: "Fail fast on stale checkpoints
        # in GbmQuantileModel.load").
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        cutoff = pd.Timestamp(probe.calendar[int(len(probe.calendar) * 0.6)])
        panel_before = features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)
        model = baseline_model.GbmQuantileModel(max_iter=15)
        model.fit(panel_before, seed=0)
        model.save(tmp_path / "gbm")

        path = synthetic_history_dir / "AAA.csv"
        lines = path.read_text().splitlines()
        header = lines[0].split(",")
        close_index = header.index("Close")
        row_cells = lines[110].split(",")
        row_cells[close_index] = str(float(row_cells[close_index]) * 1.001)
        lines[110] = ",".join(row_cells)
        path.write_text("\n".join(lines) + "\n")
        panel_after = features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)

        calls = []
        original = baseline_model.GbmQuantileModel._fit_estimators

        def spy(self, design, targets):
            calls.append(True)
            return original(self, design, targets)

        monkeypatch.setattr(baseline_model.GbmQuantileModel, "_fit_estimators", spy)
        with pytest.raises(baseline_model.TrainingDataChanged, match="DIFFERENT"):
            baseline_model.GbmQuantileModel.load(tmp_path / "gbm", panel_provider=lambda: panel_after)
        assert calls == []

    def test_load_unchanged_panel_does_not_raise(self, panel, tmp_path):
        model = baseline_model.GbmQuantileModel(max_iter=15)
        model.fit(panel, seed=0)
        model.save(tmp_path / "gbm")
        restored = baseline_model.GbmQuantileModel.load(tmp_path / "gbm", panel_provider=lambda: panel)
        assert restored is not None

    def test_row_cap_subsampling_is_deterministic(self, panel):
        small = baseline_model.GbmQuantileModel(row_cap=200, max_iter=15)
        small.fit(panel, seed=3)
        again = baseline_model.GbmQuantileModel(row_cap=200, max_iter=15)
        again.fit(panel, seed=3)
        requests = _requests(panel, "AAA", [205])
        assert small.predict(requests)[0].point == again.predict(requests)[0].point

    def test_row_cap_selects_the_same_rows_as_a_full_materialize_reference(self, panel):
        # The row-cap subsample must select the SAME global rows as the
        # original single-shot algorithm (materialize the full design
        # matrix, then rng.choice(total, row_cap)) -- refit-on-load
        # reproducibility depends on the exact rows selected -- while no
        # longer building that full concatenated matrix to get there
        # (Codex review regression: "Apply the GBM row cap before
        # concatenating all rows").
        cap = 37
        model = baseline_model.GbmQuantileModel(row_cap=cap, max_iter=5)
        model.fit(panel, seed=11)
        cutoff = baseline_model._fit_cutoff(panel)
        cutoff_str = np.datetime_as_string(cutoff, unit="D") if cutoff is not None else None
        cutoff64 = np.datetime64(cutoff_str) if cutoff_str else None

        # Reference: the ORIGINAL full-materialize-then-subsample algorithm.
        blocks_x, blocks_y = [], []
        for series in panel.series.values():
            rows = np.flatnonzero(model._usable_mask(series, cutoff64))
            if not len(rows):
                continue
            blocks_x.append(
                np.column_stack([series.features[rows], series.day_of_week[rows].astype(np.float32)])
            )
            blocks_y.append(series.targets[rows])
        ref_design = np.concatenate(blocks_x)
        ref_targets = np.concatenate(blocks_y)
        assert len(ref_design) > cap  # sanity: this test actually exercises the capped path
        rng = np.random.default_rng(11)
        chosen = np.sort(rng.choice(len(ref_design), size=cap, replace=False))
        expected_design = ref_design[chosen]
        expected_targets = ref_targets[chosen]

        design, targets = model._training_matrix(cutoff_str)
        np.testing.assert_array_equal(design, expected_design)
        np.testing.assert_array_equal(targets, expected_targets)

    def test_below_cap_matches_full_materialize_reference(self, panel):
        # The common/small-scale path (population <= row_cap) must return
        # every usable row, unchanged from before this refactor.
        model = baseline_model.GbmQuantileModel(row_cap=1_000_000, max_iter=5)
        model.fit(panel, seed=7)
        cutoff = baseline_model._fit_cutoff(panel)
        cutoff_str = np.datetime_as_string(cutoff, unit="D") if cutoff is not None else None
        cutoff64 = np.datetime64(cutoff_str) if cutoff_str else None
        blocks_x, blocks_y = [], []
        for series in panel.series.values():
            rows = np.flatnonzero(model._usable_mask(series, cutoff64))
            if not len(rows):
                continue
            blocks_x.append(
                np.column_stack([series.features[rows], series.day_of_week[rows].astype(np.float32)])
            )
            blocks_y.append(series.targets[rows])
        expected_design = np.concatenate(blocks_x)
        expected_targets = np.concatenate(blocks_y)
        design, targets = model._training_matrix(cutoff_str)
        np.testing.assert_array_equal(design, expected_design)
        np.testing.assert_array_equal(targets, expected_targets)


def test_attach_target_dates_fills_from_panel(panel):
    model = baseline_model.PersistenceModel()
    model.fit(panel, seed=0)
    series = panel.series["AAA"]
    forecasts = model.predict(_requests(panel, "AAA", [200]))
    assert forecasts[0].target_date is None
    filled = baseline_model.attach_target_dates(forecasts, panel)
    expected = np.datetime_as_string(series.label_end_dates[200], unit="D")
    assert filled[0].target_date == expected


def test_attach_target_dates_matches_despite_an_intraday_stamped_origin_date(panel):
    # forecast.origin_date is always day-precision (make_request formats it
    # via np.datetime_as_string(..., unit="D")), but series.dates could
    # carry an intraday timestamp on a hand-built panel (load_history_csv()
    # now normalizes at ingestion, but this is defense in depth) -- exact
    # equality used to never resolve the origin row, silently leaving
    # target_date=None instead of the ticker's actual next label (Codex
    # review: "Match forecast origins by calendar day").
    import dataclasses

    model = baseline_model.PersistenceModel()
    model.fit(panel, seed=0)
    ticker = "AAA"
    series = panel.series[ticker]
    row = 200
    forecasts = model.predict(_requests(panel, ticker, [row]))
    expected = np.datetime_as_string(series.label_end_dates[row], unit="D")

    intraday_dates = series.dates.copy()
    intraday_dates[row] = intraday_dates[row] + np.timedelta64(16, "h")
    panel.series[ticker] = dataclasses.replace(series, dates=intraday_dates)

    filled = baseline_model.attach_target_dates(forecasts, panel)
    assert filled[0].target_date == expected
