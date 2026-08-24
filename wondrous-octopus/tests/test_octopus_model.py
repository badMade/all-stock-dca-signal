"""Tests for the octopus (torch-guarded): leakage, recon, save/load, resume."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

import features  # noqa: E402
import model_interface  # noqa: E402
from harness import HarnessConfig, WalkForwardHarness  # noqa: E402
from ledger import Ledger  # noqa: E402
from market_encoder import MarketDayEncoder  # noqa: E402
from octopus_model import OctopusModel, _OctopusNet  # noqa: E402

from conftest import make_synthetic_ohlcv, write_ticker_csv  # noqa: E402

TINY = dict(
    window_len=16,
    d_model=16,
    n_heads=2,
    num_layers=1,
    ffn=32,
    dropout=0.0,
    n_latents=4,
    batch_size=32,
    fit_epochs=1,
    fit_sample_cap=400,
    online_steps=2,
    recent_days=5,
    replay_capacity=500,
    device="cpu",
)


def _quiet(_message: str) -> None:
    pass


@pytest.fixture
def panel(synthetic_history_dir):
    probe = features.build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[150])
    return features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


@pytest.fixture
def fitted(panel):
    model = OctopusModel(**TINY)
    stats = model.fit(panel, seed=0)
    assert stats["trainable_windows"] > 0
    return model


def _requests(panel, ticker, rows, window_len=16):
    series = panel.series[ticker]
    return [model_interface.make_request(series, row, window_len) for row in rows]


class TestCausality:
    def test_causal_trunk_hidden_states_ignore_later_positions(self):
        # Perturb position q; every hidden state BEFORE q must be unchanged.
        torch.manual_seed(0)
        net = _OctopusNet(
            n_features=4,
            d_model=16,
            n_heads=2,
            num_layers=1,
            ffn=32,
            dropout=0.0,
            window_len=8,
            vocab_size=3,
            n_latents=4,
            market_summary_dim=3,
        )
        net.eval()
        tok = torch.randn(1, 8, 16)
        perturbed = tok.clone()
        perturbed[0, 5:] += 1.0  # positions 5,6,7 change
        with torch.no_grad():
            hidden_a = net.trunk(tok, mask=net.causal_mask)
            hidden_b = net.trunk(perturbed, mask=net.causal_mask)
        assert torch.allclose(hidden_a[0, :5], hidden_b[0, :5], atol=1e-6)
        assert not torch.allclose(hidden_a[0, 5:], hidden_b[0, 5:], atol=1e-6)

    def test_prediction_depends_only_on_window_content(self, panel, fitted):
        # Same window twice must produce the identical forecast (eval mode,
        # no hidden global state).
        requests = _requests(panel, "AAA", [200])
        first = fitted.predict(requests)[0]
        second = fitted.predict(requests)[0]
        assert first.point == second.point
        assert first.quantiles == second.quantiles


class TestMarketEncoder:
    def test_permutation_invariance_over_tickers(self):
        torch.manual_seed(0)
        encoder = MarketDayEncoder(n_features=6, d_model=16, n_latents=4, n_heads=2, vocab_size=10)
        encoder.eval()
        feats = torch.randn(7, 6)
        ids = torch.arange(1, 8)
        permutation = torch.randperm(7)
        with torch.no_grad():
            _latents_a, pooled_a = encoder(feats, ids)
            _latents_b, pooled_b = encoder(feats[permutation], ids[permutation])
        assert torch.allclose(pooled_a, pooled_b, atol=1e-5)

    def test_mask_ticker_reconstruction_uses_cross_ticker_signal(self, tmp_path):
        # Two near-duplicate tickers (shared market factor, tiny idio noise)
        # and one independent ticker. After recon-heavy training, the
        # market-context diagnostic must reconstruct the correlated ticker's
        # masked CLOSE far better than that ticker's own mean-baseline --
        # while the independent ticker, by construction, cannot beat its
        # mean-baseline from market context. Skill is measured per ticker
        # against its own zero (mean) predictor so different vol levels
        # cannot fake the result.
        rng = np.random.default_rng(0)
        market = rng.normal(0.0002, 0.015, size=260)
        history = tmp_path / "ticker_data"
        history.mkdir()
        write_ticker_csv(
            history, "AAA", make_synthetic_ohlcv(seed=1, vol=0.002, market_returns=market, market_beta=1.0)
        )
        write_ticker_csv(
            history, "BBB", make_synthetic_ohlcv(seed=2, vol=0.002, market_returns=market, market_beta=1.0)
        )
        write_ticker_csv(history, "ZZZ", make_synthetic_ohlcv(seed=3, vol=0.015, market_beta=0.0))
        probe = features.build_panel(history, min_history=60)
        cutoff = pd.Timestamp(probe.calendar[200])
        panel = features.build_panel(history, min_history=60, initial_train_end=cutoff)
        model = OctopusModel(
            **{**TINY, "alpha": 0.0, "beta": 1.0, "gamma": 0.1, "fit_epochs": 10, "fit_sample_cap": 800, "lr_fit": 3e-3}
        )
        model.fit(panel, seed=0)
        rows = list(range(210, 250))
        skill = {}
        for ticker in ("AAA", "ZZZ"):
            diagnostics = model.reconstruction_diagnostic(_requests(panel, ticker, rows))
            actual_close = np.stack([panel.series[ticker].features[row, 3] for row in rows])
            mae = float(np.abs(diagnostics["market"][:, 3] - actual_close).mean())
            zero_baseline = float(np.abs(actual_close).mean())
            skill[ticker] = mae / zero_baseline
        assert skill["AAA"] < 0.85  # market context reconstructs the correlated close
        assert skill["ZZZ"] > 0.9  # ...and gives nothing for the independent one
        assert skill["AAA"] < skill["ZZZ"] - 0.15


class TestTraining:
    def test_loss_decreases_on_repeated_batches(self, panel):
        model = OctopusModel(**TINY)
        model._seed = 0
        model._panel = panel
        model._normalization = dict(panel.normalization)
        model._vocab = list(panel.tickers)
        model._vocab_index = {ticker: index + 1 for index, ticker in enumerate(model._vocab)}
        torch.manual_seed(0)
        model._net = _OctopusNet(
            n_features=len(panel.feature_names),
            d_model=16,
            n_heads=2,
            num_layers=1,
            ffn=32,
            dropout=0.0,
            window_len=16,
            vocab_size=len(model._vocab),
            n_latents=4,
            market_summary_dim=3,
        )
        model._optimizer = torch.optim.AdamW(model._net.parameters(), lr=3e-3)
        refs = [("AAA", row) for row in range(120, 152)]
        first = model._train_batch(refs, lr=3e-3)
        for _ in range(20):
            last = model._train_batch(refs, lr=3e-3)
        assert last < first

    def test_update_runs_online_steps_and_invalidates_cache(self, panel, fitted):
        series = panel.series["AAA"]
        revealed = [model_interface.make_revealed(series, row, 16) for row in range(200, 205)]
        fitted._market_embedding_for_day(100, in_graph=False)
        assert fitted._market_cache
        stats = fitted.update(revealed, step_index=1)
        assert stats["steps"] == TINY["online_steps"]
        assert not fitted._market_cache  # stale embeddings dropped

    def test_train_batch_invalidates_the_market_cache_after_every_optimizer_step(self, fitted):
        # The cache used to persist across an ENTIRE fit()/update() call,
        # cleared only once at the very end -- during any multi-batch fit()
        # or the default multi-step online update(), the SAME calendar
        # position recurs across many batches, so every batch after the
        # first that touched a given day trained the temporal network
        # against a market embedding computed from weights as of an
        # EARLIER optimizer step (during initial fitting, often the
        # randomly initialized encoder), while prediction always uses the
        # FINAL learned weights -- a train/inference mismatch (Codex
        # review: "Invalidate market embeddings after each optimizer
        # step"). One _train_batch() call is one optimizer.step(); the
        # cache must be empty again immediately after, not just at the
        # end of the whole fit()/update() call.
        refs = [("AAA", row) for row in range(120, 152)]
        fitted._market_embedding_for_day(100, in_graph=False)
        assert fitted._market_cache  # sanity: something is cached beforehand
        fitted._train_batch(refs, lr=3e-3)
        assert not fitted._market_cache


class TestFitSampling:
    def test_fit_sample_and_replay_are_both_bounded_when_population_exceeds_the_caps(self, panel):
        # A full-history retrain's trainable population can vastly exceed
        # both fit_sample_cap and replay_capacity; fit() must never
        # materialize the whole population as a Python list before
        # applying either cap (Codex review regression: "Bound Octopus
        # references before materializing them").
        tiny_caps = dict(TINY, fit_sample_cap=5, replay_capacity=7)
        model = OctopusModel(**tiny_caps)
        stats = model.fit(panel, seed=0)
        assert stats["trainable_windows"] > 7  # sanity: population exceeds both caps
        assert len(model._replay) == 7
        assert len(model._replay.items) == 7

    def test_fit_sample_is_not_biased_toward_one_ticker(self, panel):
        # Statistical sanity for the reservoir-capped replay pool: an
        # ordering bug (e.g. always keeping the first- or last-dict-
        # iterated ticker) would collapse it onto a single ticker.
        tiny_caps = dict(TINY, fit_sample_cap=40, replay_capacity=40)
        model = OctopusModel(**tiny_caps)
        model.fit(panel, seed=3)
        tickers_seen = {item[0] for item in model._replay.items}
        assert len(tickers_seen) > 1

    def test_zero_trainable_windows_is_refused(self, synthetic_history_dir):
        # A cutoff before every ticker's first trainable label (label_end
        # date is the row's own NEXT trading day) leaves _stream_trainable()
        # empty; fit() must refuse instead of checkpointing a randomly
        # initialized, never-trained network with final_loss=None (Codex
        # review regression: "Refuse fitting Octopus without training
        # windows").
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        impossible_cutoff = pd.Timestamp(probe.calendar[0])  # no row's NEXT day is this early
        panel = features.build_panel(
            synthetic_history_dir, min_history=60, initial_train_end=impossible_cutoff
        )
        model = OctopusModel(**TINY)
        with pytest.raises(ValueError, match="REFUSED"):
            model.fit(panel, seed=0)


class TestDiagnostic:
    def test_both_modes_return_bar_shaped_reconstructions(self, panel, fitted):
        requests = _requests(panel, "BBB", [200, 210])
        diagnostics = fitted.reconstruction_diagnostic(requests)
        assert set(diagnostics) == {"time", "market"}
        for matrix in diagnostics.values():
            assert matrix.shape == (2, 4)
            assert np.isfinite(matrix).all()


class TestOnlineUpdate:
    def test_recent_deque_tracks_distinct_days_not_update_batches(self, panel, fitted):
        # One update() call's revealed batch can span SEVERAL distinct
        # calendar dates when update_every > 1; appending the whole batch
        # as one deque element would make the recent-days pool retain
        # update_every x recent_days days of history instead of the
        # configured recent_days (Codex review regression: "Track recent
        # Octopus examples by day instead of update batch").
        rows = [200, 201, 202]  # three distinct origin dates for AAA
        series = panel.series["AAA"]
        revealed = [model_interface.make_revealed(series, row, fitted.hyper["window_len"]) for row in rows]
        origin_dates = {day.origin_date for day in revealed}
        assert len(origin_dates) == 3  # sanity: genuinely distinct dates
        fitted.update(revealed, step_index=1)
        assert len(fitted._recent) == 3
        pooled_dates = {ref[1] for day_list in fitted._recent for ref in day_list}
        assert pooled_dates == origin_dates

    def test_resolve_refs_matches_despite_an_intraday_stamped_date(self, panel, fitted):
        # origin_date passed to _resolve_refs (from a replay/reservoir
        # reference) is always day-precision, but series.dates could carry
        # an intraday timestamp on a hand-built panel (load_history_csv()
        # now normalizes at ingestion, but this is defense in depth) --
        # exact equality (via searchsorted + ==) used to never resolve the
        # reference: fit() would report many trainable_windows while
        # completing zero training batches (Codex review regression:
        # "Resolve Octopus training references by calendar day").
        import dataclasses

        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = 100
        origin_date_iso = str(np.datetime_as_string(series.dates[origin_row], unit="D"))

        intraday_dates = series.dates.copy()
        intraday_dates[origin_row] = intraday_dates[origin_row] + np.timedelta64(16, "h")
        panel.series[ticker] = dataclasses.replace(series, dates=intraday_dates)

        resolved = fitted._resolve_refs([(ticker, origin_date_iso)])
        assert resolved == [(ticker, origin_row)]


class TestSaveLoadAndResume:
    def test_save_load_predictions_are_bit_identical(self, panel, fitted, tmp_path):
        requests = _requests(panel, "ZZZ", [200, 220])
        original = fitted.predict(requests)
        fitted.save(tmp_path / "octopus")
        restored = OctopusModel.load(tmp_path / "octopus", panel_provider=lambda: panel)
        reloaded = restored.predict(requests)
        for a, b in zip(original, reloaded, strict=True):
            assert a.point == b.point
            assert a.quantiles == b.quantiles

    def test_load_honors_the_requested_device_instead_of_auto(self, panel, fitted, tmp_path):
        # A resume explicitly invoked with --device cpu must land on cpu,
        # not silently re-detect the best available accelerator (Codex
        # review: "Honor the requested device when loading checkpoints").
        fitted.save(tmp_path / "octopus")
        explicit = OctopusModel.load(tmp_path / "octopus", panel_provider=lambda: panel, device="cpu")
        assert explicit.device == "cpu"
        default = OctopusModel.load(tmp_path / "octopus", panel_provider=lambda: panel)
        from octopus_model import resolve_device

        assert default.device == resolve_device("auto")

    def test_bare_resume_stays_on_the_recorded_device_not_a_freshly_detected_one(
        self, panel, fitted, tmp_path, monkeypatch
    ):
        # A checkpoint fit/saved on CPU, later bare-resumed with --device
        # left at its "auto" default, must stay on the device it was
        # actually trained on -- not silently move onto whatever "auto"
        # would freshly detect on THIS host. A silent device switch means
        # no accelerator RNG state to restore and diverging online updates,
        # with no refusal or audit signal (Codex review regression:
        # "Freeze the Octopus device across resumes").
        import octopus_model
        from model_interface import read_metadata

        fitted.save(tmp_path / "octopus")
        assert read_metadata(tmp_path / "octopus")["device"] == "cpu"  # sanity: recorded as fit

        # Simulate resuming on a host that would now auto-detect a GPU.
        monkeypatch.setattr(
            octopus_model, "resolve_device", lambda requested: "cuda" if requested == "auto" else requested
        )
        resumed = OctopusModel.load(tmp_path / "octopus", panel_provider=lambda: panel)  # device left at "auto"
        assert resumed.device == "cpu"  # the RECORDED device, not the freshly-"detected" cuda

    def test_vocab_expansion_starts_new_ticker_at_unknown_embedding(self, synthetic_history_dir, panel, tmp_path):
        subset = features.build_panel(
            synthetic_history_dir,
            tickers=["AAA", "BBB", "DDD", "ZZZ"],
            min_history=60,
            initial_train_end=pd.Timestamp(panel.calendar[150]),
        )
        model = OctopusModel(**TINY)
        model.fit(subset, seed=0)
        assert model._ticker_id("CCC") == 0
        # Prediction for CCC while unknown...
        ccc_requests = _requests(panel, "CCC", [140])
        model._panel = panel  # temporary: materialize CCC's window
        unknown_forecast = model.predict(ccc_requests)[0]
        # ...must be unchanged immediately after expansion (new row = copy of
        # the unknown row), which is what makes expansion deterministic.
        model.bind_panel(panel)
        assert model._ticker_id("CCC") > 0
        expanded_forecast = model.predict(ccc_requests)[0]
        assert unknown_forecast.point == expanded_forecast.point

    def test_harness_resume_equals_uninterrupted_with_octopus(self, panel, tmp_path):
        def build_model():
            return OctopusModel(**TINY)

        start = str(np.datetime_as_string(panel.calendar[151], unit="D"))
        end_full = str(np.datetime_as_string(panel.calendar[158], unit="D"))
        end_half = str(np.datetime_as_string(panel.calendar[154], unit="D"))
        base = dict(
            start=start,
            initial_train_end=str(np.datetime_as_string(panel.calendar[150], unit="D")),
            models=("octopus",),
            window_len=16,
            min_history=60,
            checkpoint_every=4,
            diagnostic_sample=5,
            seed=0,
        )
        config_full = HarnessConfig(**base, end=end_full)
        config_half = HarnessConfig(**base, end=end_half)

        straight = WalkForwardHarness.fresh(
            panel, {"octopus": build_model()}, tmp_path / "straight", config_full, log=_quiet
        )
        assert straight.run() == "completed"

        first = WalkForwardHarness.fresh(
            panel, {"octopus": build_model()}, tmp_path / "resumed", config_half, log=_quiet
        )
        assert first.run() == "completed"
        second = WalkForwardHarness.resume(
            panel, {"octopus": OctopusModel}, tmp_path / "resumed", config_full, log=_quiet
        )
        assert second.run() == "completed"

        for kind in ("forecast", "score", "diagnostic"):
            straight_records = list(Ledger(tmp_path / "straight").iter_records(model="octopus", kind=kind))
            resumed_records = list(Ledger(tmp_path / "resumed").iter_records(model="octopus", kind=kind))
            assert straight_records == resumed_records
