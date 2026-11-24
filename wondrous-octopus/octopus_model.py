"""The octopus: a temporal Transformer over per-ticker windows + market brain.

One trunk, two passes (the "all directions" design):

- **Causal pass** (backward in time -- the only pass live forecasts use): a
  causal attention mask over the ticker's trailing window; the last
  position's representation feeds the forward head, which emits 4
  components x (1 point + 3 quantiles).
- **Bidirectional pass** (training + diagnostics only): no causal mask, and
  a fraction of real positions are masked in one of two modes --
  ``mask_day`` (the day's own features AND its market embedding are
  replaced by a learned mask token: reconstruct the day from temporal
  context on both sides) and ``mask_ticker`` (only the ticker's OWN
  features are masked; the day's market embedding stays: reconstruct this
  ticker's day from what the rest of the market did). The same
  reconstruction head, evaluated with exactly ONE mask at the window's
  final position, IS the live "predict yesterday" encoder-health
  diagnostic -- same weights, no separate machinery.

Sideways across the market: every window position's token includes that
calendar day's market embedding from market_encoder.MarketDayEncoder (the
Perceiver-style bottleneck over ALL tickers that day), and a market
auxiliary head predicts the NEXT day's market summary from the final day's
latent tokens -- training the brain to be predictive, not just descriptive.

Loss = alpha * (huber_point + sum_q pinball_q)
     + beta  * mean(recon_mask_day, recon_mask_ticker)
     + gamma * huber_market.

Cost/benefit tradeoffs, documented deliberately:

- Market embeddings for window positions come from a per-day cache computed
  with current weights and invalidated after every optimizer step (not just
  every fit()/update() call -- a stale cross-batch entry would otherwise
  train against an EARLIER encoder state while prediction always uses the
  final learned weights); gradients flow through the market encoder only via
  each batch window's FINAL day (recomputed in-graph) and the market
  auxiliary head. Recomputing every window position's day in-graph would
  multiply update cost by the window length for marginal benefit.
- The ``mask_ticker`` objective's market embedding m_d is built from ALL
  tickers that day, INCLUDING the one being reconstructed -- its own bar is
  one of ~N inputs squeezed through a small latent bottleneck, not an
  identity channel, but strictly leave-one-out it is not. Building a
  per-masked-ticker leave-one-out embedding would multiply the day-encoder
  cost by the batch size; accepted and disclosed instead (the cross-ticker
  test bounds the effect: an uncorrelated ticker CANNOT recover its own bar
  through m_d, so the shortcut is weak). Read the "market" diagnostic as
  "reconstructed from the day's market state" rather than "from all OTHER
  tickers".

Artifacts: torch never pickles anything to disk here. state_dict and
optimizer tensors cross the boundary as named arrays in weights.npz;
everything else is metadata.json (see model_interface's "no pickle, ever").

torch is imported at module top: this module is only ever imported lazily
(run_walkforward._build_models, tests via importorskip), so the rest of the
package stays importable without torch.
"""

from __future__ import annotations

import hashlib
import math
import sys
import warnings
from collections import deque
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd
import torch  # type: ignore[import-not-found,import-untyped]
from data import COMPONENTS, TrainingPanel
from horizons import horizon_targets
from market_encoder import MarketDayEncoder
from model_interface import (
    QUANTILE_KEYS,
    QUANTILES,
    ForecastRequest,
    HorizonForecast,
    OHLCForecast,
    RepairCounts,
    RevealedDay,
    finalize_forecast,
    load_arrays,
    read_metadata,
    repair_horizon_arrays,
    save_arrays,
    write_metadata,
)
from replay_buffer import ReservoirBuffer
from torch import nn  # type: ignore[import-not-found,import-untyped]

#: Feature columns holding the day's own bar -- the reconstruction target.
_BAR_SLICE = slice(0, 4)
_PREDICT_CHUNK = 512


def _derive(seed: int, tag: str) -> int:
    digest = hashlib.sha256(f"{seed}|{tag}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


#: Embedding-table size for the style cluster. Sized once at construction
#: and checkpointed with the weights, so it is a ceiling rather than the
#: panel's actual k -- a panel clustered more finely clamps into it instead
#: of failing a resume on a shape mismatch.
_MAX_CLUSTERS = 16


class _OctopusNet(nn.Module):
    def __init__(
        self,
        *,
        n_features: int,
        d_model: int,
        n_heads: int,
        num_layers: int,
        ffn: int,
        dropout: float,
        window_len: int,
        vocab_size: int,
        n_latents: int,
        market_summary_dim: int,
        n_clusters: int = _MAX_CLUSTERS,
        max_horizon: int = 1,
    ) -> None:
        super().__init__()
        self.window_len = window_len
        self.max_horizon = max_horizon
        self.feat_proj = nn.Linear(n_features, d_model)
        self.market_proj = nn.Linear(d_model, d_model)
        self.position_embedding = nn.Parameter(torch.randn(window_len, d_model) * 0.02)
        # Index 0 is the unknown ticker -- the graceful-degradation identity
        # every post-vocabulary IPO wears until expansion gives it a row.
        self.ticker_embedding = nn.Embedding(vocab_size + 1, d_model)
        self.dow_embedding = nn.Embedding(7, d_model)
        self.mask_day_token = nn.Parameter(torch.randn(d_model) * 0.02)
        self.mask_ticker_token = nn.Parameter(torch.randn(d_model) * 0.02)
        layer = nn.TransformerEncoderLayer(
            d_model, n_heads, dim_feedforward=ffn, dropout=dropout, batch_first=True
        )
        self.trunk = nn.TransformerEncoder(layer, num_layers, enable_nested_tensor=False)
        self.forward_head = nn.Sequential(
            nn.Linear(d_model, ffn), nn.GELU(), nn.Linear(ffn, len(COMPONENTS) * (1 + len(QUANTILES)))
        )
        self.recon_head = nn.Sequential(nn.Linear(d_model, ffn), nn.GELU(), nn.Linear(ffn, 4))
        self.market_head = nn.Sequential(nn.Linear(d_model, d_model), nn.GELU(), nn.Linear(d_model, market_summary_dim))
        self.market_encoder = MarketDayEncoder(
            n_features=n_features,
            d_model=d_model,
            n_latents=n_latents,
            n_heads=n_heads,
            ticker_embedding=self.ticker_embedding,
        )
        # Constructed LAST on purpose. Every nn.Module above draws its
        # initial weights from the same torch RNG stream, so inserting a new
        # parameter earlier would shift every later tensor's initialization
        # and silently change the trajectory of runs that are otherwise
        # unaffected by this feature -- which showed up as the mask-ticker
        # reconstruction diagnostic regressing on a fixed seed. Appending
        # leaves every pre-existing parameter initialized exactly as before.
        # +1 because index 0 is the reserved "not assigned yet" label a
        # ticker wears until the correlation window has filled.
        # Sized to a FIXED capacity, not to the panel that happened to fit
        # this model. bind_panel() swaps panels during a live cycle and
        # expands only the ticker embedding, so a table sized from the
        # initial panel would either clamp genuinely new styles together or,
        # for a model first fit on a single-cluster universe, drop them
        # entirely -- silently, in forecasts and online updates alike
        # (Copilot review). A constant capacity is also what makes the
        # state_dict layout stable across a resume. +1 because index 0 is
        # the reserved "not assigned yet" label a ticker wears until the
        # correlation window has filled.
        self.cluster_embedding = nn.Embedding(n_clusters + 1, d_model)
        causal = torch.triu(torch.ones(window_len, window_len, dtype=torch.bool), diagonal=1)
        self.register_buffer("causal_mask", causal)
        # Multi-horizon extension head (horizons 2..max_horizon; horizon 1 is
        # the legacy forward_head), constructed after everything else for the
        # same RNG-stream reason as cluster_embedding: a single-horizon model
        # (max_horizon == 1) owns no extra parameter and initializes exactly
        # as before. DIRECT multi-horizon with a learned horizon embedding:
        # one shared trunk summary z, then hidden_h = GELU(W z + E[h]),
        # out_h = V hidden_h. See docs/wondrous_octopus.md for why not
        # recursive rollouts. Outputs live in per-sqrt(h) units (the caller
        # multiplies by sqrt(h)) so every horizon's loss is on a comparable
        # scale instead of the longest horizon dominating it.
        if max_horizon > 1:
            self.horizon_embedding = nn.Embedding(max_horizon - 1, ffn)
            self.horizon_in = nn.Linear(d_model, ffn)
            self.horizon_out = nn.Linear(ffn, len(COMPONENTS) * (1 + len(QUANTILES)))

    def tokens(
        self,
        windows: torch.Tensor,  # (B, L, F) normalized
        market: torch.Tensor,  # (B, L, d)
        ticker_ids: torch.Tensor,  # (B,)
        day_of_week: torch.Tensor,  # (B, L)
        cluster_id: torch.Tensor | None = None,  # (B, L)
        mask_positions: torch.Tensor | None = None,  # (B, L) bool
        mask_mode: str | None = None,
    ) -> torch.Tensor:
        # day_of_week is intentionally stored compactly as int8 in panel/request
        # artifacts, but nn.Embedding requires integer index tensors (Long/Int).
        # Normalize both identity streams at the call edge so every path
        # (train/predict/update/diagnostic) feeds legal embedding indices.
        ticker_ids = ticker_ids.to(dtype=torch.long)
        day_of_week = day_of_week.to(dtype=torch.long)
        base = (
            self.position_embedding.unsqueeze(0)
            + self.ticker_embedding(ticker_ids.long()).unsqueeze(1)
            + self.dow_embedding(day_of_week.long())
        )
        if cluster_id is not None:
            # cluster_id is stored compactly as int8 in panel and request
            # artifacts, and nn.Embedding needs Long/Int indices -- the same
            # normalization ticker_ids and day_of_week already get here.
            # Clamped because the table is sized for the configured k and a
            # panel built with a larger one must degrade to a valid index
            # rather than index out of bounds mid-forecast.
            clusters = cluster_id.to(dtype=torch.long).clamp(0, self.cluster_embedding.num_embeddings - 1)
            base = base + self.cluster_embedding(clusters)
        f = self.feat_proj(windows)
        m = self.market_proj(market)
        tok = base + f + m
        if mask_positions is not None:
            expanded = mask_positions.unsqueeze(-1)
            if mask_mode == "mask_day":
                # Everything about the day disappears: pure temporal recon.
                masked = base + self.mask_day_token
            elif mask_mode == "mask_ticker":
                # The market's view of the day survives: cross-sectional recon.
                masked = base + m + self.mask_ticker_token
            else:  # pragma: no cover - defensive
                raise ValueError(f"unknown mask mode {mask_mode!r}")
            tok = torch.where(expanded, masked, tok)
        return tok

    def causal_forward(self, tok: torch.Tensor, padding: torch.Tensor) -> torch.Tensor:
        hidden = self.trunk(tok, mask=self.causal_mask, src_key_padding_mask=padding)
        return self.forward_head(hidden[:, -1, :])  # last position = day t (front padding)

    def causal_forward_all(
        self, tok: torch.Tensor, padding: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor | None]:
        """One trunk pass -> (horizon-1 head ``(B, 16)``, horizons 2..H ``(B, H-1, 16)`` or None).

        The extension output is in per-sqrt(h) units (see ``__init__``).
        """
        hidden = self.trunk(tok, mask=self.causal_mask, src_key_padding_mask=padding)
        summary = hidden[:, -1, :]
        legacy = self.forward_head(summary)
        if self.max_horizon <= 1:
            return legacy, None
        mixed = self.horizon_in(summary).unsqueeze(1) + self.horizon_embedding.weight.unsqueeze(0)
        return legacy, self.horizon_out(nn.functional.gelu(mixed))

    def bidirectional_recon(self, tok: torch.Tensor, padding: torch.Tensor) -> torch.Tensor:
        hidden = self.trunk(tok, src_key_padding_mask=padding)
        return self.recon_head(hidden)  # (B, L, 4)


class OctopusModel:
    name = "octopus"

    def __init__(
        self,
        *,
        window_len: int = 128,
        d_model: int = 128,
        n_heads: int = 4,
        num_layers: int = 4,
        ffn: int = 256,
        dropout: float = 0.1,
        n_latents: int = 16,
        alpha: float = 1.0,
        beta: float = 0.3,
        gamma: float = 0.1,
        online_steps: int = 4,
        replay_capacity: int = 50_000,
        recent_days: int = 30,
        batch_size: int = 256,
        lr_fit: float = 3e-4,
        lr_online: float = 3e-5,
        fit_epochs: int = 8,
        fit_sample_cap: int = 200_000,
        mask_fraction: float = 0.15,
        device: str = "auto",
        max_horizon: int = 1,
        horizon_weight: float = 0.5,
    ) -> None:
        self.hyper = {
            "window_len": window_len,
            "d_model": d_model,
            "n_heads": n_heads,
            "num_layers": num_layers,
            "ffn": ffn,
            "dropout": dropout,
            "n_latents": n_latents,
            "alpha": alpha,
            "beta": beta,
            "gamma": gamma,
            "online_steps": online_steps,
            "replay_capacity": replay_capacity,
            "recent_days": recent_days,
            "batch_size": batch_size,
            "lr_fit": lr_fit,
            "lr_online": lr_online,
            "fit_epochs": fit_epochs,
            "fit_sample_cap": fit_sample_cap,
            "mask_fraction": mask_fraction,
            "max_horizon": max_horizon,
            "horizon_weight": horizon_weight,
        }
        self.max_horizon = max_horizon
        #: Revealed-through date for the horizon embargo: a horizon-h target
        #: of origin row i is trainable only if row i+h is dated on/before
        #: this. None = nothing beyond horizon 1 is usable (the safe default
        #: for an external loop that never declared its cutoff).
        self._asof: np.datetime64 | None = None
        self.last_horizon_repairs: RepairCounts | None = None
        self.device = resolve_device(device)
        self._net: _OctopusNet | None = None
        self._optimizer: torch.optim.AdamW | None = None
        self._panel: TrainingPanel | None = None
        self._vocab: list[str] = []
        self._vocab_index: dict[str, int] = {}
        self._normalization: dict | None = None
        self._replay: ReservoirBuffer | None = None
        self._recent: deque = deque(maxlen=recent_days)
        self._seed = 0
        self._market_cache: dict[int, torch.Tensor] = {}
        self._update_rounds = 0
        #: Per-task breakdown of the MOST RECENT _train_batch() call, or None
        #: before the first one. Purely additive instrumentation: nothing in
        #: this module reads it, and no training behavior depends on it. It
        #: exists so an external curriculum runner (train_foundation.py) can
        #: report forward/recon/market convergence separately without
        #: forking the loss definition out of _train_batch().
        self._last_loss_components: dict[str, float] | None = None

    # ------------------------------------------------------------------
    # Protocol: fit / predict / update / diagnostic / save / load
    # ------------------------------------------------------------------
    def fit(self, panel: TrainingPanel, *, seed: int, step_index: int = 0) -> dict[str, Any]:
        del step_index  # no internal refit cadence to anchor -- update() drives everything
        self._seed = seed
        self._panel = panel
        self._normalization = dict(panel.normalization)
        self._vocab = list(panel.tickers)
        self._vocab_index = {ticker: index + 1 for index, ticker in enumerate(self._vocab)}
        torch.manual_seed(_derive(seed, "init"))
        self._net = _OctopusNet(
            n_features=len(panel.feature_names),
            d_model=self.hyper["d_model"],
            n_heads=self.hyper["n_heads"],
            num_layers=self.hyper["num_layers"],
            ffn=self.hyper["ffn"],
            dropout=self.hyper["dropout"],
            window_len=self.hyper["window_len"],
            vocab_size=len(self._vocab),
            n_latents=self.hyper["n_latents"],
            market_summary_dim=panel.market_summary.shape[1],
            n_clusters=_MAX_CLUSTERS,
            max_horizon=self.max_horizon,
        ).to(self.device)
        self._optimizer = torch.optim.AdamW(self._net.parameters(), lr=self.hyper["lr_fit"])
        self._replay = ReservoirBuffer(self.hyper["replay_capacity"], _derive(seed, "replay"))
        self._recent = deque(maxlen=self.hyper["recent_days"])
        self._market_cache = {}

        cutoff = self._normalization.get("window_end")
        # The horizon embargo's revealed-through date for this fit: the fit
        # cutoff, or -- when there is none, meaning "every row is trainable"
        # (the horizon-1 convention) -- the archive's newest date.
        self._asof = np.datetime64(cutoff, "D") if cutoff else self._panel_last_date()
        # A full-history recovery retrain over the whole archive can have
        # ~39M trainable (ticker, row) pairs; materializing that list before
        # fit_sample_cap ever applies is a multi-gigabyte peak on top of the
        # resident panel (Codex review: "Bound Octopus references before
        # materializing them"). Stream the panel ONCE, filling the
        # fit-sample reservoir (capped at fit_sample_cap, used for THIS
        # fit's gradient steps) and the replay reservoir (capped at
        # replay_capacity, the anti-catastrophic-forgetting pool used by
        # every future online update) together -- each ReservoirBuffer owns
        # its own RNG, so interleaving the two add() calls doesn't change
        # either one's output versus visiting the panel twice.
        fit_reservoir = ReservoirBuffer(self.hyper["fit_sample_cap"], _derive(seed, "fit-sample"))
        n_trainable = 0
        for ticker, row in self._stream_trainable(cutoff):
            n_trainable += 1
            series = self._panel.series[ticker]
            date_iso = str(np.datetime_as_string(series.dates[row], unit="D"))
            fit_reservoir.add(ticker, date_iso)
            self._replay.add(ticker, date_iso)
        if not n_trainable:
            # A zero-example fit would checkpoint a randomly initialized
            # network with final_loss=None and no way to tell it apart from
            # a genuinely trained model later (Codex review: "Refuse
            # fitting Octopus without training windows").
            raise ValueError("REFUSED: no trainable windows at or before cutoff for octopus fit")
        refs = self._resolve_refs([(item[0], item[1]) for item in fit_reservoir.items])
        rng = np.random.default_rng(_derive(seed, "epoch-shuffle"))
        losses: list[float] = []
        is_tty = sys.stdout.isatty()
        total_epochs = self.hyper["fit_epochs"]
        for epoch in range(total_epochs):
            order = rng.permutation(len(refs))
            total_batches = (len(order) + self.hyper["batch_size"] - 1) // self.hyper["batch_size"]
            batch_idx = 0
            for begin in range(0, len(order), self.hyper["batch_size"]):
                batch = [refs[index] for index in order[begin : begin + self.hyper["batch_size"]]]
                loss = self._train_batch(batch, lr=self.hyper["lr_fit"])
                losses.append(loss)
                batch_idx += 1
                if is_tty and (batch_idx % 5 == 0 or batch_idx == total_batches):
                    pct = (batch_idx / total_batches) * 100
                    sys.stdout.write(
                        f"\r[fit] octopus: epoch {epoch + 1}/{total_epochs} "
                        f"[{batch_idx}/{total_batches} ({pct:3.0f}%)] loss: {loss:.4f}  "
                    )
                    sys.stdout.flush()
                elif not is_tty and (batch_idx % 50 == 0 or batch_idx == total_batches):
                    pct = (batch_idx / total_batches) * 100
                    sys.stdout.write(
                        f"[fit] octopus: epoch {epoch + 1}/{total_epochs} "
                        f"[{batch_idx}/{total_batches} ({pct:3.0f}%)] loss: {loss:.4f}\n"
                    )
                    sys.stdout.flush()
        if total_epochs > 0 and losses:
            msg = f"[fit] octopus: {total_epochs} epochs complete! final loss: {losses[-1]:.4f}\n"
            sys.stdout.write(f"\r{msg}" if is_tty else msg)
            sys.stdout.flush()
        self._market_cache = {}
        return {
            "epochs": self.hyper["fit_epochs"],
            "trainable_windows": n_trainable,
            "final_loss": losses[-1] if losses else None,
            "device": self.device,
            "parameters": sum(parameter.numel() for parameter in self._net.parameters()),
        }

    def predict(self, requests: Sequence[ForecastRequest]) -> list[OHLCForecast]:
        assert self._net is not None
        self._net.eval()
        repairs = RepairCounts()
        forecasts: list[OHLCForecast] = []
        with torch.no_grad():
            for begin in range(0, len(requests), _PREDICT_CHUNK):
                chunk = requests[begin : begin + _PREDICT_CHUNK]
                tensors = self._tensors_from_requests(chunk)
                market = self._market_tensor(tensors["cal_pos"], in_graph_final=False)
                tok = self._net.tokens(
                    tensors["windows"],
                    market,
                    tensors["ticker_ids"],
                    tensors["day_of_week"],
                    tensors["cluster_id"],
                )
                raw = self._net.causal_forward(tok, tensors["padding"])
                raw = raw.view(len(chunk), len(COMPONENTS), 1 + len(QUANTILES)).cpu().numpy()
                for position, request in enumerate(chunk):
                    point = {
                        component: float(raw[position, slot, 0]) for slot, component in enumerate(COMPONENTS)
                    }
                    quantiles = {
                        component: {
                            key: float(raw[position, slot, 1 + qindex])
                            for qindex, key in enumerate(QUANTILE_KEYS)
                        }
                        for slot, component in enumerate(COMPONENTS)
                    }
                    forecasts.append(
                        finalize_forecast(
                            ticker=request.ticker,
                            origin_date=request.origin_date,
                            target_date=None,
                            point=point,
                            quantiles=quantiles,
                            ref_close=request.ref_close,
                            adj_ratio=request.adj_ratio,
                            repairs=repairs,
                        )
                    )
        self.last_repairs = repairs
        return forecasts

    def predict_horizons(self, requests: Sequence[ForecastRequest], *, max_horizon: int) -> list[HorizonForecast]:
        """Forecast EVERY horizon 1..``max_horizon`` for each request (direct heads).

        Horizon 1 is the legacy head (so the h=1 row equals what ``predict``
        returns); horizons 2..H come from the horizon-embedding head and are
        rescaled by sqrt(h) out of the per-sqrt(h) units they train in. Every
        row is then geometry-repaired (counts in ``last_horizon_repairs``).
        """
        assert self._net is not None
        if max_horizon != self.max_horizon:
            raise ValueError(f"octopus was built for max_horizon={self.max_horizon}, asked for {max_horizon}")
        self._net.eval()
        slots = len(COMPONENTS)
        scale = np.sqrt(np.arange(2, max_horizon + 1, dtype=np.float32))[None, :, None, None]
        forecasts: list[HorizonForecast] = []
        totals = RepairCounts()
        with torch.no_grad():
            for begin in range(0, len(requests), _PREDICT_CHUNK):
                chunk = requests[begin : begin + _PREDICT_CHUNK]
                tensors = self._tensors_from_requests(chunk)
                market = self._market_tensor(tensors["cal_pos"], in_graph_final=False)
                tok = self._net.tokens(
                    tensors["windows"],
                    market,
                    tensors["ticker_ids"],
                    tensors["day_of_week"],
                    tensors["cluster_id"],
                )
                legacy, extension = self._net.causal_forward_all(tok, tensors["padding"])
                grid = legacy.view(len(chunk), 1, slots, 1 + len(QUANTILES)).cpu().numpy()
                if extension is not None:
                    ext = extension.view(len(chunk), max_horizon - 1, slots, 1 + len(QUANTILES)).cpu().numpy()
                    grid = np.concatenate([grid, ext * scale], axis=1)
                point, quantiles, counts = repair_horizon_arrays(
                    grid[..., 0].astype(np.float64), grid[..., 1:].astype(np.float64)
                )
                totals.quantile_crossings += counts.quantile_crossings
                totals.bar_repairs += counts.bar_repairs
                for position, request in enumerate(chunk):
                    forecasts.append(
                        HorizonForecast(
                            ticker=request.ticker,
                            origin_date=request.origin_date,
                            point=point[position],
                            quantiles=quantiles[position],
                            ref_close=request.ref_close,
                            adj_ratio=request.adj_ratio,
                        )
                    )
        self.last_horizon_repairs = totals
        return forecasts

    def _panel_last_date(self) -> np.datetime64:
        assert self._panel is not None
        return max(series.dates[-1] for series in self._panel.series.values() if len(series.dates)).astype(
            "datetime64[D]"
        )

    def update(self, revealed: Sequence[RevealedDay], *, step_index: int) -> dict[str, Any]:
        assert self._net is not None and self._panel is not None and self._replay is not None
        by_date: dict[str, list[tuple[str, str]]] = {}
        for day in revealed:
            by_date.setdefault(day.origin_date, []).append((day.ticker, day.origin_date))
            self._replay.add(day.ticker, day.origin_date)
            # Revealed-through date for the horizon embargo only ever moves
            # to a target date the harness has actually revealed.
            target = np.datetime64(day.target_date, "D")
            if self._asof is None or target > self._asof:
                self._asof = target
        # One deque element per DISTINCT calendar date, not per update()
        # call: with update_every > 1 a single call's revealed batch can
        # span several dates, and appending the whole batch as one element
        # would make the recent-days deque retain update_every x
        # recent_days days of history instead of the configured
        # recent_days (Codex review: "Track recent Octopus examples by day
        # instead of update batch").
        for origin_date in sorted(by_date):
            self._recent.append(by_date[origin_date])
        rng = np.random.default_rng(_derive(self._seed, f"update:{step_index}"))
        recent_pool = [ref for day_list in self._recent for ref in day_list]
        losses: list[float] = []
        skipped_batches = 0
        for _step in range(self.hyper["online_steps"]):
            half = self.hyper["batch_size"] // 2
            batch_refs: list[tuple[str, str]] = []
            if recent_pool:
                chosen = rng.choice(len(recent_pool), size=min(half, len(recent_pool)), replace=False)
                batch_refs.extend(recent_pool[index] for index in chosen)
            batch_refs.extend(self._replay.sample(self.hyper["batch_size"] - len(batch_refs)))
            resolved = self._resolve_refs(batch_refs)
            if not resolved:
                break
            loss = self._train_batch(resolved, lr=self.hyper["lr_online"])
            if math.isnan(loss) or math.isinf(loss):
                skipped_batches += 1
            else:
                losses.append(loss)
        # Weights moved: every cached market embedding is stale.
        self._market_cache = {}
        self._update_rounds += 1
        return {"steps": len(losses), "skipped": skipped_batches, "loss": losses[-1] if losses else None}

    def reconstruction_diagnostic(self, requests: Sequence[ForecastRequest]) -> dict[str, np.ndarray] | None:
        assert self._net is not None
        self._net.eval()
        mean, std = self._bar_stats()
        output: dict[str, np.ndarray] = {}
        with torch.no_grad():
            tensors = self._tensors_from_requests(list(requests))
            market = self._market_tensor(tensors["cal_pos"], in_graph_final=False)
            final_only = torch.zeros_like(tensors["padding"])
            final_only[:, -1] = True
            for mode in ("mask_day", "mask_ticker"):
                tok = self._net.tokens(
                    tensors["windows"],
                    market,
                    tensors["ticker_ids"],
                    tensors["day_of_week"],
                    tensors["cluster_id"],
                    mask_positions=final_only,
                    mask_mode=mode,
                )
                recon = self._net.bidirectional_recon(tok, tensors["padding"])[:, -1, :].cpu().numpy()
                label = "time" if mode == "mask_day" else "market"
                output[label] = recon * std + mean  # back to raw bar log-return units
        return output

    def verify_finiteness(self) -> tuple[bool, str]:
        """Verify that all network parameters and optimizer momentum/variance buffers are finite.

        Returns (True, "") if healthy, or (False, error_description) if any NaN or Inf is found.
        """
        if self._net is not None:
            for name, param in self._net.named_parameters():
                if not torch.isfinite(param).all():
                    return False, f"non-finite parameter in {name}"
        if self._optimizer is not None:
            for param_id, state in self._optimizer.state.items():
                for buf_name in ("exp_avg", "exp_avg_sq"):
                    buf = state.get(buf_name)
                    if isinstance(buf, torch.Tensor) and not torch.isfinite(buf).all():
                        return False, f"non-finite optimizer buffer {buf_name} for param_id {param_id}"
        return True, ""

    def is_healthy(self) -> bool:
        """Return True if model parameters and optimizer buffers are completely finite."""
        is_finite, _ = self.verify_finiteness()
        return is_finite

    def bind_panel(self, panel: TrainingPanel) -> None:
        """Swap panels (live cycle); expand the vocabulary for new tickers.

        New embedding rows initialize from the unknown-ticker row (index 0):
        the new ticker starts exactly where it stood while unknown and then
        differentiates. Expansion rebuilds the optimizer, preserving
        existing trunk and layer Adam moments while adding slots for new tickers.
        """
        self._panel = panel
        self._market_cache = {}
        if self._net is None:
            return
        new_tickers = [ticker for ticker in panel.tickers if ticker not in self._vocab_index]
        if not new_tickers:
            return
        old_embedding = self._net.ticker_embedding
        old_rows = old_embedding.weight.detach()
        unknown_row = old_rows[0:1]
        added = unknown_row.repeat(len(new_tickers), 1)
        new_embedding = nn.Embedding(old_rows.shape[0] + len(new_tickers), old_rows.shape[1]).to(self.device)
        with torch.no_grad():
            new_embedding.weight[: old_rows.shape[0]] = old_rows
            new_embedding.weight[old_rows.shape[0] :] = added
        self._net.ticker_embedding = new_embedding
        self._net.market_encoder.ticker_embedding = new_embedding
        for ticker in new_tickers:
            self._vocab.append(ticker)
            self._vocab_index[ticker] = len(self._vocab)
        old_state_by_param = dict(self._optimizer.state) if self._optimizer is not None else {}
        self._optimizer = torch.optim.AdamW(self._net.parameters(), lr=self.hyper["lr_online"])
        if old_state_by_param:
            for p in self._net.parameters():
                if p in old_state_by_param:
                    self._optimizer.state[p] = old_state_by_param[p]

    def save(self, directory: Path) -> None:
        assert self._net is not None and self._optimizer is not None and self._replay is not None
        is_finite, err = self.verify_finiteness()
        if not is_finite:
            raise ValueError(f"REFUSED to save non-finite OctopusModel: {err}")
        arrays: dict[str, np.ndarray] = {}
        for key, tensor in self._net.state_dict().items():
            arrays[f"net.{key}"] = tensor.detach().cpu().numpy()
        optimizer_state = self._optimizer.state_dict()
        for param_id, buffers in optimizer_state["state"].items():
            for key, value in buffers.items():
                arrays[f"optim.{param_id}.{key}"] = (
                    value.detach().cpu().numpy() if isinstance(value, torch.Tensor) else np.asarray(value)
                )
        arrays["torch_rng"] = torch.get_rng_state().numpy()
        # Accelerator generators advance independently of the CPU one
        # (dropout on CUDA/MPS draws from the device RNG), so byte-identical
        # resume on the same device type requires checkpointing them too
        # (Codex review: "Checkpoint accelerator RNG state"). Guarded: a
        # checkpoint written on a GPU box restores on CPU by ignoring these.
        if self.device == "cuda" and torch.cuda.is_available():
            for index, state in enumerate(torch.cuda.get_rng_state_all()):
                arrays[f"torch_cuda_rng_{index}"] = state.cpu().numpy()
        elif self.device == "mps" and getattr(torch, "mps", None) is not None:
            arrays["torch_mps_rng"] = torch.mps.get_rng_state().cpu().numpy()
        save_arrays(directory, arrays)
        write_metadata(
            directory,
            {
                "name": self.name,
                "hyper": self.hyper,
                "device": self.device,
                "seed": self._seed,
                "vocab": self._vocab,
                "normalization": self._normalization,
                "param_groups": optimizer_state["param_groups"],
                "optim_param_ids": sorted(int(key) for key in optimizer_state["state"]),
                "replay": self._replay.to_state(),
                "recent": [list(map(list, day_list)) for day_list in self._recent],
                "update_rounds": self._update_rounds,
                "asof": str(self._asof) if self._asof is not None else None,
            },
        )

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        panel_provider: Callable[[], TrainingPanel] | None = None,
        strict: bool = True,
        device: str = "auto",
    ) -> "OctopusModel":
        del strict  # state is fully in the artifact; nothing is re-derived from the panel
        if panel_provider is None:
            raise ValueError("OctopusModel.load requires a panel_provider (windows re-materialize from the panel)")
        metadata = read_metadata(directory)
        # Honor the invocation's requested device instead of always
        # re-detecting the best accelerator: an operator resuming with
        # --device cpu (e.g. to free a shared GPU) must not silently land
        # back on cuda/mps (Codex review: "Honor the requested device when
        # loading checkpoints"). But "auto" -- run_walkforward.py's
        # --device default -- means "no explicit operator preference", not
        # "re-detect the best accelerator on THIS host": a bare --resume on
        # a different machine (or one that later gained a GPU) would
        # otherwise silently move onto CUDA/MPS with no accelerator RNG
        # state to restore, diverging online updates from an uninterrupted
        # run with no refusal or audit signal. Prefer the checkpoint's own
        # recorded device in that case; an explicit --device request still
        # always wins (Codex review: "Freeze the Octopus device across
        # resumes"). Older checkpoints saved before this fix have no
        # recorded device -- fall back to the requested one, same as before.
        effective_device = metadata.get("device", device) if device == "auto" else device
        model = cls(device=effective_device, **metadata["hyper"])
        model._seed = metadata["seed"]
        model._vocab = list(metadata["vocab"])
        model._vocab_index = {ticker: index + 1 for index, ticker in enumerate(model._vocab)}
        model._normalization = metadata["normalization"]
        panel = panel_provider()
        model._panel = panel
        arrays = load_arrays(directory)
        model._net = _OctopusNet(
            n_features=len(panel.feature_names),
            d_model=model.hyper["d_model"],
            n_heads=model.hyper["n_heads"],
            num_layers=model.hyper["num_layers"],
            ffn=model.hyper["ffn"],
            dropout=model.hyper["dropout"],
            window_len=model.hyper["window_len"],
            vocab_size=len(model._vocab),
            n_latents=model.hyper["n_latents"],
            market_summary_dim=panel.market_summary.shape[1],
            n_clusters=_MAX_CLUSTERS,
            max_horizon=model.max_horizon,
        ).to(model.device)
        state = {
            key[len("net.") :]: torch.from_numpy(np.array(value))
            for key, value in arrays.items()
            if key.startswith("net.")
        }
        model._net.load_state_dict(state)
        model._optimizer = torch.optim.AdamW(model._net.parameters(), lr=model.hyper["lr_online"])
        optim_state: dict[int, dict[str, Any]] = {}
        for param_id in metadata["optim_param_ids"]:
            buffers: dict[str, Any] = {}
            for key in ("step", "exp_avg", "exp_avg_sq"):
                array = arrays.get(f"optim.{param_id}.{key}")
                if array is not None:
                    buffers[key] = torch.from_numpy(np.array(array))
            optim_state[param_id] = buffers
        param_groups = []
        for group in metadata["param_groups"]:
            group = dict(group)
            if "betas" in group:  # JSON turned the tuple into a list
                group["betas"] = tuple(group["betas"])
            param_groups.append(group)
        model._optimizer.load_state_dict({"state": optim_state, "param_groups": param_groups})
        torch.set_rng_state(torch.from_numpy(np.array(arrays["torch_rng"])).to(torch.uint8))
        cuda_keys = sorted(
            (key for key in arrays if key.startswith("torch_cuda_rng_")),
            key=lambda key: int(key.rsplit("_", 1)[1]),
        )
        if cuda_keys and model.device == "cuda" and torch.cuda.is_available():
            torch.cuda.set_rng_state_all(
                [torch.from_numpy(np.array(arrays[key])).to(torch.uint8) for key in cuda_keys]
            )
        if "torch_mps_rng" in arrays and model.device == "mps" and getattr(torch, "mps", None) is not None:
            torch.mps.set_rng_state(torch.from_numpy(np.array(arrays["torch_mps_rng"])).to(torch.uint8))
        model._replay = ReservoirBuffer.from_state(metadata["replay"])
        model._recent = deque(
            [[tuple(ref) for ref in day_list] for day_list in metadata["recent"]],
            maxlen=model.hyper["recent_days"],
        )
        model._update_rounds = metadata["update_rounds"]
        # .get(): checkpoints written before the horizon embargo existed.
        saved_asof = metadata.get("asof")
        model._asof = np.datetime64(saved_asof, "D") if saved_asof else None
        # bind_panel AFTER vocab restore: a panel with new tickers expands here.
        model.bind_panel(panel)
        return model

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _bar_stats(self) -> tuple[np.ndarray, np.ndarray]:
        assert self._normalization is not None
        mean = np.asarray(self._normalization["mean"], dtype=np.float32)[_BAR_SLICE]
        std = np.asarray(self._normalization["std"], dtype=np.float32)[_BAR_SLICE]
        return mean, std

    def _normalize(self, windows: np.ndarray) -> np.ndarray:
        assert self._normalization is not None
        mean = np.asarray(self._normalization["mean"], dtype=np.float32)
        std = np.asarray(self._normalization["std"], dtype=np.float32)
        normalized = (windows - mean) / std
        # Warm-up rows inside a young ticker's window carry NaN trailing
        # stats; zero (= the mean) is the neutral imputation.
        return np.nan_to_num(normalized, nan=0.0, posinf=0.0, neginf=0.0)

    def _ticker_id(self, ticker: str) -> int:
        return self._vocab_index.get(ticker, 0)

    def _stream_trainable(self, cutoff: str | None):
        """Yield every trainable (ticker, row) pair one at a time.

        A generator, not a materialized list: at full-universe scale the
        trainable population is tens of millions of pairs, and fit() only
        ever needs to feed them through two bounded reservoirs, never hold
        them all at once (Codex review: "Bound Octopus references before
        materializing them").
        """
        assert self._panel is not None
        cutoff64 = np.datetime64(cutoff) if cutoff else None
        for ticker, series in self._panel.series.items():
            usable = np.isfinite(series.targets).all(axis=1)
            usable &= np.isfinite(series.features[:, _BAR_SLICE]).all(axis=1)
            if cutoff64 is not None:
                usable &= series.label_end_dates <= cutoff64
            for row in np.flatnonzero(usable):
                yield ticker, int(row)

    def _resolve_refs(self, refs: list[tuple[str, str]]) -> list[tuple[str, int]]:
        """(ticker, origin_date) -> (ticker, row) against the CURRENT panel."""
        assert self._panel is not None
        resolved: list[tuple[str, int]] = []
        for ticker, origin_date in refs:
            series = self._panel.series.get(ticker)
            if series is None:
                continue
            # Calendar-day comparison: origin_date is always day-precision,
            # series.dates is now normalized at ingestion but a hand-built
            # panel could still carry an intraday timestamp -- defense in
            # depth (Codex review: "Resolve Octopus training references by
            # calendar day").
            day_dates = series.dates.astype("datetime64[D]")
            origin64 = np.datetime64(origin_date)
            position = np.searchsorted(day_dates, origin64)
            if position < len(day_dates) and day_dates[position] == origin64:
                if np.isfinite(series.targets[position]).all():
                    resolved.append((ticker, int(position)))
        return resolved

    def _tensors_from_requests(self, requests: Sequence[ForecastRequest]) -> dict[str, torch.Tensor]:
        windows = np.stack([self._normalize(request.features_window) for request in requests])
        masks = np.stack([request.window_mask for request in requests])
        dows = np.stack([np.clip(request.day_of_week, 0, 6) for request in requests]).astype(np.int64)
        clusters = np.stack(
            [np.clip(request.cluster_id, 0, _MAX_CLUSTERS) for request in requests]
        ).astype(np.int64)
        ids = np.array([self._ticker_id(request.ticker) for request in requests], dtype=np.int64)
        cal_pos = np.stack([self._calendar_positions(request.window_dates) for request in requests])
        return {
            "windows": torch.from_numpy(windows).to(self.device),
            "padding": torch.from_numpy(~masks).to(self.device),
            "day_of_week": torch.from_numpy(dows).long().to(self.device),
            "cluster_id": torch.from_numpy(clusters).long().to(self.device),
            "ticker_ids": torch.from_numpy(ids).to(self.device),
            "cal_pos": torch.from_numpy(cal_pos).to(self.device),
        }

    def _tensors_from_rows(self, refs: list[tuple[str, int]]) -> dict[str, torch.Tensor]:
        from model_interface import make_request

        assert self._panel is not None
        requests = [
            make_request(self._panel.series[ticker], row, self.hyper["window_len"]) for ticker, row in refs
        ]
        tensors = self._tensors_from_requests(requests)
        targets = np.stack(
            [self._panel.series[ticker].targets[row] for ticker, row in refs]
        ).astype(np.float32)
        tensors["targets"] = torch.from_numpy(targets).to(self.device)
        if self.max_horizon > 1:
            # Horizons 2..H, every one gated by the embargo against the
            # revealed-through date -- NEVER by "is the target finite": the
            # replay panel holds the whole archive, so a future bar always
            # exists in memory. No declared asof => no extension targets.
            extension = np.full((len(refs), self.max_horizon - 1, len(COMPONENTS)), np.nan, dtype=np.float32)
            if self._asof is not None:
                for position, (ticker, row) in enumerate(refs):
                    full = horizon_targets(self._panel.series[ticker], row, self.max_horizon, self._asof)
                    extension[position] = full[1:]
            tensors["horizon_targets"] = torch.from_numpy(extension).to(self.device)
        market_targets = np.full((len(refs), self._panel.market_summary.shape[1]), np.nan, dtype=np.float32)
        for position, (ticker, row) in enumerate(refs):
            series = self._panel.series[ticker]
            date = pd.Timestamp(series.dates[row])
            calendar_position = self._panel.calendar_pos.get(date)
            if calendar_position is not None and calendar_position + 1 < len(self._panel.market_summary):
                market_targets[position] = self._panel.market_summary[calendar_position + 1]
        tensors["market_targets"] = torch.from_numpy(market_targets).to(self.device)
        return tensors

    def _calendar_positions(self, window_dates: np.ndarray) -> np.ndarray:
        assert self._panel is not None
        positions = np.full(len(window_dates), -1, dtype=np.int64)
        for index, date in enumerate(window_dates):
            if not np.isnat(date):
                found = self._panel.calendar_pos.get(pd.Timestamp(date))
                if found is not None:
                    positions[index] = found
        return positions

    def _day_inputs(self, calendar_position: int) -> tuple[torch.Tensor, torch.Tensor] | None:
        assert self._panel is not None
        date = pd.Timestamp(self._panel.calendar[calendar_position])
        pairs = self._panel.date_index.get(date, [])
        if not pairs:
            return None
        rows = np.stack([self._panel.series[ticker].features[row] for ticker, row in pairs])
        ids = np.array([self._ticker_id(ticker) for ticker, _row in pairs], dtype=np.int64)
        features = torch.from_numpy(self._normalize(rows)).to(self.device)
        return features, torch.from_numpy(ids).to(self.device)

    def _market_embedding_for_day(self, calendar_position: int, *, in_graph: bool) -> torch.Tensor:
        assert self._net is not None
        if not in_graph and calendar_position in self._market_cache:
            return self._market_cache[calendar_position]
        inputs = self._day_inputs(calendar_position)
        d_model = self.hyper["d_model"]
        if inputs is None:
            embedding = torch.zeros(d_model, device=self.device)
        elif in_graph:
            features, ids = inputs
            _latents, embedding = self._net.market_encoder(features, ids)
        else:
            features, ids = inputs
            # Cached embeddings are ALWAYS computed in eval mode: (a) they
            # must be dropout-free deterministic inputs, and (b) torch's
            # train-mode and eval-fastpath transformer paths differ in
            # low-order bits, so letting the caller's mode leak in here made
            # "cached during predict" and "computed fresh during update"
            # disagree -- which broke resume-equals-uninterrupted.
            was_training = self._net.market_encoder.training
            self._net.market_encoder.eval()
            try:
                with torch.no_grad():
                    _latents, embedding = self._net.market_encoder(features, ids)
            finally:
                if was_training:
                    self._net.market_encoder.train()
        if not in_graph:
            self._market_cache[calendar_position] = embedding.detach()
            return self._market_cache[calendar_position]
        return embedding

    def _market_latents_for_day(self, calendar_position: int) -> torch.Tensor | None:
        assert self._net is not None
        inputs = self._day_inputs(calendar_position)
        if inputs is None:
            return None
        features, ids = inputs
        latents, _embedding = self._net.market_encoder(features, ids)
        return latents

    def _market_tensor(self, cal_pos: torch.Tensor, *, in_graph_final: bool) -> torch.Tensor:
        batch, length = cal_pos.shape
        d_model = self.hyper["d_model"]
        market = torch.zeros(batch, length, d_model, device=self.device)
        positions = cal_pos.cpu().numpy()
        unique_positions = sorted({int(value) for value in positions.flatten() if value >= 0})
        cached = {
            position: self._market_embedding_for_day(position, in_graph=False)
            for position in unique_positions
        }
        for row in range(batch):
            for column in range(length):
                position = int(positions[row, column])
                if position >= 0:
                    market[row, column] = cached[position]
        if in_graph_final:
            # Gradient reaches the market encoder through each window's
            # FINAL day only (see the module docstring's tradeoff note).
            final_positions = sorted({int(value) for value in positions[:, -1] if value >= 0})
            live = {position: self._market_embedding_for_day(position, in_graph=True) for position in final_positions}
            for row in range(batch):
                position = int(positions[row, -1])
                if position >= 0:
                    market[row, -1, :] = live[position]
        return market

    @staticmethod
    def _pinball(predicted: torch.Tensor, target: torch.Tensor, quantile: float) -> torch.Tensor:
        error = target - predicted
        return torch.maximum(quantile * error, (quantile - 1.0) * error).mean()

    def _horizon_loss(self, raw: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        """Masked point + pinball loss over horizons 2..H, in per-sqrt(h) units.

        ``targets`` is ``(B, H-1, 4)`` with NaN wherever the horizon is
        embargoed (or past the archive's end); those entries carry no
        gradient. Both sides are divided by sqrt(h) so a t+90 residual (~9x a
        daily one) does not drown the short horizons' loss.
        """
        valid = torch.isfinite(targets)
        if not valid.any():
            return torch.zeros((), device=self.device)
        steps = torch.sqrt(torch.arange(2, self.max_horizon + 1, device=self.device, dtype=raw.dtype))
        standardized = torch.where(valid, targets, torch.zeros_like(targets)) / steps.view(1, -1, 1)
        weight = valid.to(raw.dtype)
        count = weight.sum()
        point = (nn.functional.smooth_l1_loss(raw[..., 0], standardized, reduction="none") * weight).sum() / count
        pinball = raw.new_zeros(())
        for index, quantile in enumerate(QUANTILES):
            error = standardized - raw[..., 1 + index]
            pinball = pinball + (torch.maximum(quantile * error, (quantile - 1.0) * error) * weight).sum() / count
        return point + pinball

    def train_step(
        self, refs: Sequence[tuple[str, int]], *, lr: float, asof: np.datetime64 | None = None
    ) -> tuple[float, dict[str, float]]:
        """Run one multi-task gradient step and return ``(total, components)``.

        The public face of :meth:`_train_batch` for external training loops
        (train_foundation.py's curriculum pre-training) that need their own
        batch ordering and learning-rate schedule but must NOT fork the loss
        definition. ``total`` is NaN when the step was skipped by the
        non-finite loss/gradient guards; ``components`` still carries the
        per-task breakdown that produced it.
        """
        if asof is not None:
            # An external curriculum declares how far the world is revealed
            # for this step; without it the horizon head simply gets no
            # targets (horizon 1 is trainable by the caller's own guard).
            self._asof = np.datetime64(asof, "D")
        total = self._train_batch(list(refs), lr=lr)
        return total, dict(self._last_loss_components or {})

    def _train_batch(self, refs: list[tuple[str, int]], *, lr: float) -> float:
        assert self._net is not None and self._optimizer is not None
        self._net.train()
        for group in self._optimizer.param_groups:
            group["lr"] = lr
        tensors = self._tensors_from_rows(refs)
        market = self._market_tensor(tensors["cal_pos"], in_graph_final=True)
        batch, length = tensors["padding"].shape

        # -- forward (causal) loss ------------------------------------------
        tok = self._net.tokens(
            tensors["windows"],
            market,
            tensors["ticker_ids"],
            tensors["day_of_week"],
            tensors["cluster_id"],
        )
        legacy_out, extension_out = self._net.causal_forward_all(tok, tensors["padding"])
        raw = legacy_out.view(batch, len(COMPONENTS), 1 + len(QUANTILES))
        targets = tensors["targets"]
        point_loss = nn.functional.smooth_l1_loss(raw[:, :, 0], targets)
        pinball_loss = sum(
            self._pinball(raw[:, :, 1 + index], targets, quantile) for index, quantile in enumerate(QUANTILES)
        )
        forward_loss = point_loss + pinball_loss
        horizon_loss = torch.zeros((), device=self.device)
        if extension_out is not None:
            horizon_loss = self._horizon_loss(
                extension_out.view(batch, self.max_horizon - 1, len(COMPONENTS), 1 + len(QUANTILES)),
                tensors["horizon_targets"],
            )
            forward_loss = forward_loss + self.hyper["horizon_weight"] * horizon_loss

        # -- reconstruction (bidirectional) loss ----------------------------
        real = ~tensors["padding"]
        seed_tag = f"mask:{self._update_rounds}:{len(refs)}"
        generator = torch.Generator(device="cpu").manual_seed(_derive(self._seed, seed_tag))
        noise = torch.rand(batch, length, generator=generator).to(self.device)
        mask_positions = (noise < self.hyper["mask_fraction"]) & real
        # Guarantee at least one masked position per window (tiny windows).
        mask_positions[:, -1] |= ~mask_positions.any(dim=1)
        bar_actual = tensors["windows"][:, :, _BAR_SLICE]
        recon_losses = []
        for mode in ("mask_day", "mask_ticker"):
            tok_masked = self._net.tokens(
                tensors["windows"],
                market.detach(),
                tensors["ticker_ids"],
                tensors["day_of_week"],
                tensors["cluster_id"],
                mask_positions=mask_positions,
                mask_mode=mode,
            )
            recon = self._net.bidirectional_recon(tok_masked, tensors["padding"])
            per_element = nn.functional.smooth_l1_loss(recon, bar_actual, reduction="none").mean(dim=-1)
            selected = per_element[mask_positions]
            if selected.numel():
                recon_losses.append(selected.mean())
        recon_loss = torch.stack(recon_losses).mean() if recon_losses else torch.zeros((), device=self.device)

        # -- market auxiliary loss ------------------------------------------
        market_targets = tensors["market_targets"]
        valid = torch.isfinite(market_targets).all(dim=1)
        market_loss = torch.zeros((), device=self.device)
        if valid.any():
            positions = tensors["cal_pos"][:, -1].cpu().numpy()
            # Deduplicate by calendar position: predict each distinct day once against its target,
            # avoiding redundant backward accumulation across duplicate ticker rows for the same day.
            unique_targets: dict[int, torch.Tensor] = {}
            for row in np.flatnonzero(valid.cpu().numpy()):
                pos = int(positions[row])
                if pos >= 0 and pos not in unique_targets:
                    unique_targets[pos] = market_targets[row]
            predictions = []
            targets_rows = []
            for position, tgt in sorted(unique_targets.items()):
                latents = self._market_latents_for_day(position)
                if latents is None:
                    continue
                predictions.append(self._net.market_head(latents.mean(dim=0)))
                targets_rows.append(tgt)
            if predictions:
                market_loss = nn.functional.smooth_l1_loss(
                    torch.stack(predictions), torch.stack(targets_rows)
                )

        total = (
            self.hyper["alpha"] * forward_loss
            + self.hyper["beta"] * recon_loss
            + self.hyper["gamma"] * market_loss
        )
        # Recorded BEFORE the finiteness guard below, so a skipped step still
        # leaves an honest record of WHICH term blew up instead of an empty
        # dict. Stacked into one tensor for a SINGLE device->host sync: four
        # separate float(...cpu()) calls would add three extra MPS/CUDA
        # stalls to every step of a multi-hour pre-training run.
        _components = torch.stack(
            (forward_loss.detach(), recon_loss.detach(), market_loss.detach(), total.detach())
        ).cpu()
        self._last_loss_components = {
            "forward": float(_components[0]),
            "recon": float(_components[1]),
            "market": float(_components[2]),
            "total": float(_components[3]),
        }
        if not torch.isfinite(total):
            warnings.warn(
                f"Non-finite loss in _train_batch (forward={float(forward_loss.detach()):.4f}, "
                f"recon={float(recon_loss.detach()):.4f}, market={float(market_loss.detach()):.4f}); skipping step.",
                RuntimeWarning,
                stacklevel=2,
            )
            self._optimizer.zero_grad()
            return float("nan")

        # Snapshot parameters before step for self-healing rollback if needed
        pre_step_state = {k: v.detach().clone() for k, v in self._net.state_dict().items()}
        self._optimizer.zero_grad()
        total.backward()

        # Check gradient finiteness across all parameters before clipping and optimizer step
        grads_finite = True
        non_finite_param = ""
        for name, param in self._net.named_parameters():
            if param.grad is not None and not torch.isfinite(param.grad).all():
                grads_finite = False
                non_finite_param = name
                break

        if not grads_finite:
            warnings.warn(
                f"Non-finite gradient in parameter {non_finite_param!r}; "
                "skipping clip_grad_norm_ and optimizer.step() to protect AdamW buffers.",
                RuntimeWarning,
                stacklevel=2,
            )
            self._optimizer.zero_grad()
            return float("nan")

        nn.utils.clip_grad_norm_(self._net.parameters(), 1.0)
        self._optimizer.step()

        # Post-step verification: ensure parameters and optimizer state are completely finite
        is_finite, err_msg = self.verify_finiteness()
        if not is_finite:
            warnings.warn(
                f"Model or optimizer state became non-finite after step ({err_msg}); "
                "rolling back weights to pre-step snapshot.",
                RuntimeWarning,
                stacklevel=2,
            )
            self._net.load_state_dict(pre_step_state)
            self._optimizer.zero_grad()
            return float("nan")
        # Every cached (detached, in_graph=False) market embedding was
        # computed from the market encoder's weights AS OF THE LAST STEP --
        # this step just moved them, so every entry is now stale. Within a
        # single fit() (many epochs/batches) or the default multi-step
        # online update(), the SAME calendar position recurs across many
        # batches; without clearing here, every batch after the first that
        # touches a given day trained the temporal network against a market
        # representation frozen at an EARLIER (during initial fitting, often
        # near-randomly-initialized) encoder state, while prediction always
        # uses the FINAL learned weights -- a train/inference mismatch
        # (Codex review: "Invalidate market embeddings after each optimizer
        # step"). The fit()/update() end-of-call clears become redundant
        # once every step already clears, but are left in place as
        # explicit, harmless defense-in-depth.
        self._market_cache = {}
        # The already-synced value from the component snapshot above, not a
        # second total.detach().cpu(): recording the breakdown costs this
        # method's existing callers (the online-update path) zero extra
        # device->host stalls that way.
        return self._last_loss_components["total"]
