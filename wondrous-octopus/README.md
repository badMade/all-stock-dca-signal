# Wondrous Octopus

Walk-forward, online-updated, whole-market next-day OHLC forecasting: many arms (every ticker
in the archive), one brain (a shared cross-ticker market encoder).

At each day `t` the system predicts day `t+1`'s open, high, low, and close for every eligible
ticker, using only data through `t` — as a point estimate **and** a p10/p50/p90 band per
component. It learns in "all directions" without ever cheating:

- **Backward in time** — a causal Transformer pass over each ticker's trailing window; the
  only path live forecasts use.
- **Both directions in time** — a training-only masked-day reconstruction objective (masked
  days are rebuilt from context on both sides). Never an inference mode, so no leakage; the
  same head doubles as the live "predict yesterday" encoder-health diagnostic.
- **Sideways across the market** — a Perceiver-style per-day market encoder: learned latent
  tokens cross-attend to every ticker's day-`t` features, and every window position consumes
  its day's market embedding. Leakage-free by construction (all inputs are day ≤ `t`).

Validated by a strict walk-forward protocol — predictions start 2000-01-01; **training reaches
as far back as each ticker's archive goes** — with daily online updates (recent days + a
reservoir replay buffer against catastrophic forgetting), continuing as a repeating live daily
cycle after the replay catches up to the present.

Full design, runbook, presets, and reliability mechanics: **`docs/wondrous_octopus.md`** at the
repo root. Quick start:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # CPU torch: pip install torch --index-url https://download.pytorch.org/whl/cpu
python run_walkforward.py --demo --run-dir /tmp/octopus-demo   # self-contained smoke test
python inspect_run.py --run-dir /tmp/octopus-demo --tomorrow   # the headline output
python inspect_run.py --run-dir /tmp/octopus-demo --summary    # the short whole-model analysis
```

## Modules

| Module | Responsibility |
|---|---|
| `data.py` | Archive discovery/loading, full-bar split/dividend adjustment, as-of truncation, panel containers |
| `features.py` | Causal per-ticker features, next-day OHLC log-return targets, cross-sectional market features, correlation-based style clustering, panel assembly, the opt-in `calculus` feature block |
| `model_interface.py` | The `OHLCForecaster` protocol, shared quantile/bar repairs, adaptive conformal calibration, no-pickle artifact helpers |
| `baseline_model.py` | `persistence` (with honest residual bands) and `gbm_quantile` (16 histogram-GBM fits, scheduled refits) |
| `market_encoder.py` | The per-day cross-ticker latent bottleneck ("the brain") |
| `octopus_model.py` | The temporal Transformer: causal forward head, two-mode reconstruction head, market auxiliary head, online fine-tune |
| `replay_buffer.py` | Reservoir buffer of (ticker, date) references — checkpoints as JSON |
| `ledger.py` | Append-only gzip-member JSONL shards with the truncate-or-refuse resume contract |
| `harness.py` | The engine: reveal → score → diagnose → update → predict, checkpoint/resume, deterministic seeding |
| `metrics.py` | Scoring payloads, regime × model × component reports, encoder-health verdicts |
| `stacking.py` | The regime-weighted ensemble over the models' already-ledgered scores (library-only; see `docs/wondrous_octopus.md`) |
| `run_walkforward.py` | CLI: the historical replay (`--demo`, `--preset`, `--resume`, `--retrain`, `--report-only`) |
| `daily_update.py` | CLI: the live one-day cycle; writes the next-day forecast CSV + daily summary |
| `analysis.py` / `inspect_run.py` | The observability layer: `--tomorrow`, `--latest`, `--ticker`, `--summary`, `--html` |

## Design commitments

- **No pickle, ever.** Model artifacts are `metadata.json` + `weights.npz` named arrays
  (`torch.save` is banned — it is a pickle stream); the GBM refits deterministically on load
  instead of serializing trees. Same stance as `ml-forecaster/model.py`, independently applied
  (siblings deliberately do not import each other).
- **Resume equals uninterrupted.** Per-(day, purpose) seeds derive from the master seed;
  ledger appends are whole gzip members fingerprinted into the checkpoint; a resumed run
  reproduces the uninterrupted run byte-for-byte. Enforced by tests.
- **Missing stocks are never blockers.** Broken/short/absent tickers are logged, counted, and
  skipped; short histories join automatically after their 60-row warm-up. Only an archive
  with zero usable tickers refuses.
- **Honest comparison.** The baselines see the same market information (as summary statistics)
  the octopus sees through attention; multi-model reports intersect to shared
  (ticker, origin day) pairs; calibrated and raw bands are both scored.
- **Production-safe-loop safeguards.** A frozen spec (config hash sealed into every
  checkpoint; changed flags REFUSE), a circuit breaker (`--max-steps`/`--max-hours` hard
  per-invocation ceilings, checked pre-flight, tripping with a loud banner + clean
  checkpoint), the append-only fingerprinted ledger, defined-exit-only loop paths, and a
  review surface whose every claim is computed from ledgered measurements. Mapping table in
  `docs/wondrous_octopus.md`.

## What this does NOT do

- It does not trade, and it does not read or write the core repo's `config/`.
- It does not influence live DCA sizing. Forecast artifacts are research outputs under the
  run directory (`dca_paths.stream_root("wondrous_octopus")` by default).
- It does not need Ollama or any model server: the model is a small custom PyTorch network
  (~1–2M parameters), driven directly by the CLIs. `--device auto` prefers CUDA, then
  Apple-Silicon MPS, then CPU.
- It does not fetch data. The archive is maintained by the repo's existing ticker-update
  pipeline; this tool only reads it.
