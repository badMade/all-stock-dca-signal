"""The octopus's direct multi-horizon head: all horizons out, embargoed training, checkpoint round trip."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

import features as _features  # noqa: E402
import horizons  # noqa: E402
import model_interface  # noqa: E402
from octopus_model import OctopusModel as _OctopusModel  # noqa: E402

features: Any = _features
OctopusModel: Any = _OctopusModel

TINY: dict[str, Any] = dict(
    window_len=16,
    d_model=16,
    n_heads=2,
    num_layers=1,
    ffn=32,
    dropout=0.0,
    n_latents=4,
    batch_size=32,
    fit_epochs=1,
    fit_sample_cap=300,
    online_steps=2,
    recent_days=5,
    replay_capacity=300,
    device="cpu",
)


@pytest.fixture
def panel(synthetic_history_dir):
    probe = features.build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[150])
    return features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


def _requests(panel, ticker, rows):
    return [model_interface.make_request(panel.series[ticker], row, 16) for row in rows]


@pytest.fixture
def fitted90(panel):
    model = OctopusModel(max_horizon=90, **TINY)
    model.fit(panel, seed=0)
    return model


class TestAllHorizonsEmitted:
    def test_all_ninety_horizons_with_point_and_p10_p50_p90(self, panel, fitted90):
        forecasts = fitted90.predict_horizons(_requests(panel, "AAA", [100, 120]), max_horizon=90)
        assert len(forecasts) == 2
        for forecast in forecasts:
            assert forecast.point.shape == (90, 4)  # OHLC point at every horizon
            assert forecast.quantiles.shape == (90, 4, 3)  # p10/p50/p90 per component
            assert np.isfinite(forecast.point).all() and np.isfinite(forecast.quantiles).all()
            assert (np.diff(forecast.quantiles, axis=-1) >= 0).all()  # no crossed bands
            high, low = forecast.point[:, 1], forecast.point[:, 2]
            assert (high >= forecast.point[:, 0]).all() and (high >= forecast.point[:, 3]).all()
            assert (low <= forecast.point[:, 0]).all() and (low <= forecast.point[:, 3]).all()

    def test_horizon_one_row_is_the_native_forecast(self, panel, fitted90):
        requests = _requests(panel, "AAA", [100])
        (native,) = fitted90.predict(requests)
        (multi,) = fitted90.predict_horizons(requests, max_horizon=90)
        assert multi.point[0, 3] == pytest.approx(native.point["close"], abs=1e-6)
        assert multi.quantiles[0, 3, 2] == pytest.approx(native.quantiles["close"]["p90"], abs=1e-6)

    def test_asking_for_a_different_max_horizon_is_refused(self, panel, fitted90):
        with pytest.raises(ValueError):
            fitted90.predict_horizons(_requests(panel, "AAA", [100]), max_horizon=30)

    def test_single_horizon_model_owns_no_extension_parameters(self, panel):
        legacy = OctopusModel(**TINY)
        legacy.fit(panel, seed=0)
        assert not any(name.startswith("horizon_") for name, _ in legacy._net.named_parameters())
        multi = OctopusModel(max_horizon=5, **TINY)
        multi.fit(panel, seed=0)
        assert any(name.startswith("horizon_") for name, _ in multi._net.named_parameters())


class TestTrainingEmbargo:
    def test_fit_asof_is_the_cutoff_and_horizon_targets_stop_there(self, panel, fitted90):
        cutoff = np.datetime64(panel.normalization["window_end"], "D")
        assert fitted90._asof == cutoff
        series = panel.series["AAA"]
        row = int(np.searchsorted(series.dates.astype("datetime64[D]"), cutoff)) - 3
        tensors = fitted90._tensors_from_rows([("AAA", row)])
        extension = tensors["horizon_targets"][0].numpy()  # horizons 2..90
        usable = horizons.usable_horizon_count(series, row, cutoff, 90)
        assert usable == 3  # only three rows of the future were revealed by the cutoff
        assert np.isfinite(extension[: usable - 1]).all()
        assert np.isnan(extension[usable - 1 :]).all()  # embargoed -- though the bars exist in memory

    def test_fit_loss_cannot_see_post_cutoff_bars(self, panel):
        """Corrupt every bar after the fit cutoff: the trained weights must not change."""
        base = OctopusModel(max_horizon=20, **TINY)
        base.fit(panel, seed=3)
        cutoff = np.datetime64(panel.normalization["window_end"], "D")
        for series in panel.series.values():
            after = series.dates.astype("datetime64[D]") > cutoff
            series.ref_close = series.ref_close.copy()
            series.targets = series.targets.copy()
            series.ref_close[after] *= 7.0
            series.targets[after] = 5.0
            # the last pre-cutoff row's own target is a post-cutoff bar only if it is beyond the
            # cutoff; rows whose label ends after the cutoff are already excluded by the h=1 guard
        corrupted = OctopusModel(max_horizon=20, **TINY)
        corrupted.fit(panel, seed=3)
        a = {k: v for k, v in base._net.state_dict().items() if k.startswith("horizon_")}
        b = {k: v for k, v in corrupted._net.state_dict().items() if k.startswith("horizon_")}
        assert a and all(torch.equal(a[k], b[k]) for k in a)

    def test_update_advances_asof_only_to_revealed_target_dates(self, panel, fitted90):
        series = panel.series["AAA"]
        start = fitted90._asof
        row = int(np.searchsorted(series.dates.astype("datetime64[D]"), start)) + 1
        revealed = model_interface.make_revealed(series, row, 16)
        fitted90.update([revealed], step_index=1)
        assert fitted90._asof == np.datetime64(revealed.target_date, "D")
        assert fitted90._asof > start

    def test_without_a_declared_asof_the_extension_head_gets_no_targets(self, panel):
        model = OctopusModel(max_horizon=10, **TINY)
        model.fit(panel, seed=0)
        model._asof = None
        tensors = model._tensors_from_rows([("AAA", 100)])
        assert torch.isnan(tensors["horizon_targets"]).all()
        total, parts = model.train_step([("AAA", 100)], lr=1e-4)
        assert np.isfinite(total)

    def test_train_step_with_asof_uses_embargoed_targets(self, panel):
        model = OctopusModel(max_horizon=10, **TINY)
        model.fit(panel, seed=0)
        series = panel.series["AAA"]
        asof = series.dates[110]
        total, _parts = model.train_step([("AAA", 100)], lr=1e-4, asof=asof)
        assert np.isfinite(total)
        assert model._asof == np.datetime64(asof, "D")


class TestCheckpointRoundTrip:
    def test_save_load_preserves_weights_asof_and_forecasts(self, panel, fitted90, tmp_path):
        directory = tmp_path / "octopus"
        fitted90.save(directory)
        assert not list(directory.glob("*.pkl")) and not list(directory.glob("*.pt"))
        loaded = OctopusModel.load(directory, panel_provider=lambda: panel, device="cpu")
        assert loaded.max_horizon == 90 and loaded._asof == fitted90._asof
        requests = _requests(panel, "AAA", [100])
        (before,) = fitted90.predict_horizons(requests, max_horizon=90)
        (after,) = loaded.predict_horizons(requests, max_horizon=90)
        assert np.allclose(before.point, after.point, atol=1e-5)
        assert np.allclose(before.quantiles, after.quantiles, atol=1e-5)
