"""Automated tests for numerical stability defense and self-healing mechanisms."""

from __future__ import annotations

import math
from typing import Any, cast

import features as _features
import numpy as np
import pandas as pd
import pytest
import torch
from data import COMPONENTS
from harness import HarnessConfig, WalkForwardHarness, _checkpoint_has_nan
from metrics import score_forecast
from octopus_model import OctopusModel

features: Any = _features

pytestmark = pytest.mark.filterwarnings("ignore::RuntimeWarning")

TINY: dict[str, Any] = dict(
    window_len=16,
    d_model=16,
    n_heads=2,
    num_layers=1,
    ffn=32,
    dropout=0.0,
    n_latents=4,
    batch_size=16,
    fit_epochs=1,
    fit_sample_cap=200,
    online_steps=2,
    recent_days=5,
    replay_capacity=200,
    device="cpu",
)


@pytest.fixture
def panel(synthetic_history_dir):
    probe = features.build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[150])
    return features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


@pytest.fixture
def fitted(panel):
    model = OctopusModel(**TINY)
    model.fit(panel, seed=0)
    return model


class TestNumericalDefense:
    def test_verify_finiteness_detects_nan_in_weights_and_buffers(self, fitted):
        assert fitted.is_healthy()
        is_finite, err = fitted.verify_finiteness()
        assert is_finite
        assert err == ""

        # Corrupt a parameter
        first_param = next(fitted._net.parameters())
        orig_val = first_param.data.clone()
        first_param.data[0] = float("nan")

        is_finite, err = fitted.verify_finiteness()
        assert not is_finite
        assert "non-finite parameter" in err
        assert not fitted.is_healthy()

        # Restore parameter
        first_param.data = orig_val
        assert fitted.is_healthy()

        # Step optimizer so buffers exist
        fitted._optimizer.step()
        state = next(iter(fitted._optimizer.state.values()))
        assert "exp_avg" in state
        state["exp_avg"][0] = float("nan")

        is_finite, err = fitted.verify_finiteness()
        assert not is_finite
        assert "non-finite optimizer buffer" in err
        assert not fitted.is_healthy()

    def test_train_batch_rejects_nan_loss_and_preserves_finite_state(self, fitted, panel):
        ticker = panel.tickers[0]
        panel.series[ticker].targets[100, 0] = float("nan")

        with pytest.warns(RuntimeWarning, match="Non-finite loss"):
            loss = fitted._train_batch([(ticker, 100)], lr=0.001)

        assert math.isnan(loss)
        assert fitted.is_healthy()

    def test_train_batch_rejects_nan_gradient_and_protects_adamw(self, fitted, panel, monkeypatch):
        ticker = panel.tickers[0]
        orig_backward = torch.Tensor.backward

        def poisoned_backward(tensor, *args, **kwargs):
            orig_backward(tensor, *args, **kwargs)
            # Poison one gradient
            p = next(fitted._net.parameters())
            if p.grad is not None:
                p.grad[0] = float("nan")

        monkeypatch.setattr(torch.Tensor, "backward", poisoned_backward)

        with pytest.warns(RuntimeWarning, match="Non-finite gradient"):
            loss = fitted._train_batch([(ticker, 100)], lr=0.001)

        assert math.isnan(loss)
        assert fitted.is_healthy()
        # Verify AdamW momentum buffers remained clean
        for state in fitted._optimizer.state.values():
            for buf_name in ("exp_avg", "exp_avg_sq"):
                if buf_name in state:
                    assert torch.isfinite(state[buf_name]).all()

    def test_bind_panel_preserves_optimizer_moments_on_universe_expansion(self, fitted, panel):
        ticker = panel.tickers[0]
        # Step training
        fitted._train_batch([(ticker, 100)], lr=0.001)

        trunk_param = fitted._net.trunk.layers[0].linear1.weight
        assert trunk_param in fitted._optimizer.state
        old_step = fitted._optimizer.state[trunk_param]["step"]
        old_exp_avg = fitted._optimizer.state[trunk_param]["exp_avg"].clone()
        assert int(old_step) > 0
        assert (old_exp_avg != 0).any()

        # Add a new ticker to panel
        from data import TickerSeries
        dates = panel.calendar
        n = len(dates)
        new_series = TickerSeries(
            ticker="NEW_TICKER",
            ticker_id=len(panel.tickers) + 1,
            dates=dates,
            features=np.ones((n, 20), dtype=np.float32) * 0.05,
            targets=np.zeros((n, 4), dtype=np.float32),
            label_end_dates=panel.series[ticker].label_end_dates,
            day_of_week=panel.series[ticker].day_of_week,
            cluster_id=panel.series[ticker].cluster_id,
            ref_close=panel.series[ticker].ref_close,
            adj_ratio=panel.series[ticker].adj_ratio,
        )
        panel.series["NEW_TICKER"] = new_series
        panel.tickers.append("NEW_TICKER")

        # Bind expanded panel
        fitted.bind_panel(panel)

        # Trunk parameter optimizer state MUST be preserved!
        assert trunk_param in fitted._optimizer.state
        new_step = fitted._optimizer.state[trunk_param]["step"]
        new_exp_avg = fitted._optimizer.state[trunk_param]["exp_avg"]
        assert new_step == old_step
        assert torch.equal(new_exp_avg, old_exp_avg)


class TestSelfHealingCheckpoint:
    def test_autonomous_rollback_from_corrupt_checkpoint(self, tmp_path):
        """If primary checkpoint contains NaNs, load_checkpoint automatically heals from checkpoint.good."""
        import json
        import shutil

        run_dir = tmp_path / "test_run"
        run_dir.mkdir()
        (run_dir / "ledger").mkdir()
        checkpoint_dir = run_dir / "checkpoint"
        good_dir = run_dir / "checkpoint.good"

        # Create healthy dummy checkpoint.good
        good_dir.mkdir(parents=True)
        models_dir = good_dir / "models" / "octopus"
        models_dir.mkdir(parents=True)
        np.savez(models_dir / "weights.npz", weight=np.array([1.0, 2.0], dtype=np.float32))
        (models_dir / "metadata.json").write_text(json.dumps({"name": "octopus"}))

        from harness import _fingerprint_directory
        good_fingerprint = _fingerprint_directory(models_dir)

        # Copy to checkpoint_dir, then corrupt checkpoint_dir with NaN
        shutil.copytree(good_dir, checkpoint_dir)
        nan_weight = np.array([1.0, float("nan")], dtype=np.float32)
        np.savez(checkpoint_dir / "models" / "octopus" / "weights.npz", weight=nan_weight)

        assert _checkpoint_has_nan(checkpoint_dir)
        assert not _checkpoint_has_nan(good_dir)

        # Write state file
        cfg = HarnessConfig(start="2021-01-04", models=("octopus",))
        state = {
            "schema_version": 1,
            "config": cfg.as_dict(),
            "config_hash": cfg.config_hash(),
            "ledger": {},
            "model_fingerprints": {"octopus": good_fingerprint},
        }
        (checkpoint_dir / "harness_state.json").write_text(json.dumps(state))
        (good_dir / "harness_state.json").write_text(json.dumps(state))

        # verify_checkpoint_integrity should detect NaN and heal from checkpoint.good
        WalkForwardHarness.verify_checkpoint_integrity(cast(Any, None), run_dir, cfg, mutate_ledger=True)

        # After healing, checkpoint_dir must now be free of NaNs!
        assert not _checkpoint_has_nan(checkpoint_dir)


class TestMetricsAndCircuitBreaker:
    def test_score_forecast_flags_non_finite_predictions(self):
        point_nan = {"close": float("nan"), "open": 0.01, "high": 0.02, "low": -0.01}
        quantiles = {c: {"p10": -0.02, "p50": 0.0, "p90": 0.02} for c in COMPONENTS}
        actual = {c: 0.005 for c in COMPONENTS}

        scored = score_forecast(
            point=point_nan,
            quantiles=quantiles,
            quantiles_raw=quantiles,
            actual=actual,
            ref_close=100.0,
        )
        assert scored["non_finite"] is True
        # Sentinel bounding still protects downstream arithmetic
        assert scored["abs_err"]["close"] == 50.0 - 0.005

        # Completely finite forecast
        point_finite = {"close": 0.01, "open": 0.01, "high": 0.02, "low": -0.01}
        scored_finite = score_forecast(
            point=point_finite,
            quantiles=quantiles,
            quantiles_raw=quantiles,
            actual=actual,
            ref_close=100.0,
        )
        assert scored_finite["non_finite"] is False


class TestUnifiedMarketRepresentation:
    def test_external_market_table_unifies_representations_across_shards(self, synthetic_history_dir):
        """Disjoint shards share identical macro market representations when provided a unified market table."""
        all_tickers = features.discover_available_tickers(synthetic_history_dir)
        assert len(all_tickers) >= 2
        mid = len(all_tickers) // 2
        shard1_tickers = all_tickers[:mid]
        shard2_tickers = all_tickers[mid:]

        # Without unified market table: each shard computes market averages from its own siloed tickers
        panel1_isolated = features.build_panel(synthetic_history_dir, tickers=shard1_tickers, min_history=60)
        panel2_isolated = features.build_panel(synthetic_history_dir, tickers=shard2_tickers, min_history=60)

        # Market summaries will differ because the universes are disjoint
        assert not np.allclose(panel1_isolated.market_summary, panel2_isolated.market_summary)

        # Compute full-universe market table
        unified_market = features.compute_market_table(synthetic_history_dir, min_history=60)
        assert not unified_market.empty

        # Build both shards with the shared market table
        panel1_unified = features.build_panel(
            synthetic_history_dir,
            tickers=shard1_tickers,
            min_history=60,
            external_market_table=unified_market,
        )
        panel2_unified = features.build_panel(
            synthetic_history_dir,
            tickers=shard2_tickers,
            min_history=60,
            external_market_table=unified_market,
        )

        # Now, both shards share the EXACT SAME macro market targets!
        assert np.array_equal(panel1_unified.market_summary, panel2_unified.market_summary)

