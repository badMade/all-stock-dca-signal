# Wondrous Octopus — walk-forward, whole-market OHLC forecasting

The `wondrous-octopus/` sibling tree: many arms (every ticker in the archive), one brain (a
shared cross-ticker market encoder). At each day *t* it predicts day *t+1*'s open, high, low,
and close for every eligible ticker, using only data through *t*, and validates itself with a
strict walk-forward protocol plus daily online updates.

**Scope boundary (binding): this tool never reads or writes `config/`, never trades, and does
not influence live DCA sizing.** Its outputs are research artifacts under its own run
directory. It is a separate mechanism from `ml-forecaster/` (the Ridge sizing-nudge pipeline)
and does not share code with it — sibling trees deliberately do not import each other.

## Learning in "all directions" without leakage

| Direction | Mechanism | Used live? |
|---|---|---|
| Backward in time | Causal attention over each ticker's trailing window → forward head (point + p10/p50/p90 per component) | **Yes — the only path forecasts use** |
| Both directions in time | Training-only masked-day reconstruction (BERT-style): reconstruct a day's bar from context on both sides | Training + diagnostics only |
| Sideways across the market | A Perceiver-style per-day market encoder: learned latent tokens cross-attend to every ticker's day-*t* features; every window position consumes its day's market embedding | Yes — leakage-free by construction (all inputs are day ≤ *t*) |

The reconstruction head doubles as the live **"predict yesterday" health diagnostic**: at each
step it reconstructs the most recent closed day with that position masked, in two modes —
time-context only (`mask_day`) and market-context only (`mask_ticker`). A rolling degradation
beyond 2× the post-fit baseline flags `encoder_health: DEGRADED` in the daily summary.
Telemetry only; the sanctioned response is a recovery retrain, never anything automatic.
One disclosed caveat: the `mask_ticker` mode's market embedding is built from *all* tickers
that day, including the masked one — one input among ~N through a small latent bottleneck,
not an identity channel (tests bound the effect: an uncorrelated ticker cannot recover its own
bar through it). Read it as "reconstructed from the day's market state", not strictly
"from all other tickers".

## Time semantics

- `--start` (default 2000-01-01) is the first **prediction** date. Training is *not* truncated
  there: the initial fit — and every `--resume --retrain` recovery retrain — uses every row a
  ticker has before the cutoff, however far back its archive goes.
- The as-of rule is calendar-day-exclusive (a run "as of" an instant never sees that day's own
  bar), and a row becomes trainable only once its next trading row's date has passed — the
  horizon-1 form of ml-forecaster's walk-forward leakage guard.
- Missing/corrupt/short tickers are **never blockers**: they are logged, counted in the run
  report, and skipped; a short-history ticker joins automatically once it has 60 prior rows.
  Only an archive with zero usable tickers refuses.

## Models compared on identical metrics

- `persistence` — tomorrow = today, with honest empirical residual bands (the yardstick).
- `gbm_quantile` — 16 `HistGradientBoostingRegressor` fits (4 components × point/p10/p50/p90)
  on last-day features + market summary statistics; "online update" = scheduled refit
  (row-capped, bounded trailing window).
- `octopus` — the PyTorch model above (~1–2M parameters). Online update = a few gradient steps
  per day on recent days + a reservoir replay buffer (anti-catastrophic-forgetting).

All bands pass through shared **adaptive conformal calibration** (default on): a per-component
width multiplier nudged daily toward the 80% coverage target. Raw and calibrated bands are both
ledgered so the report shows calibration's contribution honestly.

## Data

Per-ticker CSVs (`Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits`) from
`dca_ticker_paths.configured_ticker_history_dir()` (`$DCA_TICKER_HISTORY_DIR`, default the
`ticker_data/` symlink). The whole system operates in split/dividend-**adjusted** space (O/H/L/C
scaled by each row's `Adj Close / Close`); every record carries the applied ratio so dollar
conversion stays auditable (day-*t*'s ratio is wrong only across an overnight split — rare and
visible, never silent). The dead Polygon stub `historical_stock_data.csv` is not used.

## Running it on your own machine

No model server, no Ollama — `python run_walkforward.py …` is the whole deployment.
`--device auto` prefers CUDA, then Apple-Silicon MPS, then CPU.

```bash
cd wondrous-octopus && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # CPU torch: pip install torch --index-url https://download.pytorch.org/whl/cpu
export DCA_TICKER_HISTORY_DIR=/path/to/ticker_data   # or rely on the repo's ticker_data symlink

python run_walkforward.py --demo --run-dir /tmp/octopus-demo    # self-contained smoke test (seconds)
python run_walkforward.py --preset quick --dry-run              # cost estimate before committing
python run_walkforward.py --preset quick --run-dir ~/octopus/quick
```

Presets bundle the scale knobs (explicit flags always win):

| Preset | Universe | Window | Online update | GBM refit | Intent |
|---|---|---|---|---|---|
| `quick` | 500 longest histories | 64 | every 5 days | quarterly | prove the pipeline in ~an hour |
| `overnight` | 2,000 | 128 | every 2 days | monthly | one night on MPS/CUDA |
| `full` | whole archive | 128 | daily | monthly | several nights via `--resume` |

Runs checkpoint every 20 steps and on SIGINT/SIGTERM (exit 3), so stopping any evening and
`--resume`-ing the next is the normal full-scale workflow. Exit codes: 0 done, 1 REFUSED
(nothing written), 2 runtime failure (checkpoint retained), 3 clean interrupt. A resume
refuses changed flags (only `--end`/`--now` may move) and a changed ticker universe.

### The daily live cycle

After the replay catches up to the present, run once per day after the archive refresh:

```bash
python daily_update.py --run-dir ~/octopus/quick
```

Scores yesterday's forecasts, runs the encoder diagnostic, applies the online update, predicts
the next day, checkpoints, writes `daily/<date>_summary.json` **and
`daily/<date>_next_day_forecasts.csv`** — tomorrow's predicted O/H/L/C with bands, in dollars,
for every eligible ticker — and prints a one-line digest. Idempotent (`NOOP` on rerun);
catches up automatically if days were missed. Scheduling it is the operator's choice.

### Recovery retrains

After major discrepancies or errors (a `DEGRADED` health flag, a runtime failure that leaves
doubt, scores diverging wildly from the baselines):

```bash
python run_walkforward.py --resume <run_dir> --retrain ...same flags...
```

re-runs the full-history fit at the current cursor (pre-2000 rows included), preserves the
ledger and cursor, and continues. Suggested by the daily digest when warranted; never automatic.

## Watching it work

```bash
python inspect_run.py --run-dir <dir> --tomorrow [--csv out.csv]   # the headline: tomorrow's OHLC, all tickers
python inspect_run.py --run-dir <dir> --latest                     # was it right today? predicted vs actual
python inspect_run.py --run-dir <dir> --ticker AAPL --last 30      # one stock over time
python inspect_run.py --run-dir <dir> --summary                    # the short whole-model analysis
python inspect_run.py --run-dir <dir> --html dash.html             # self-contained dashboard
```

The replay logs one line per simulated day (`tail -f` friendly), and `--report-only <run_dir>`
re-renders `report/report.json` + `report.md` from the ledger at any time, even mid-run.

## Metrics and reports

Per model × regime (dot-com bust, GFC, COVID, 2022 rate shock, other, all) × component:
MAE/RMSE (log-return and dollar), pinball at 0.1/0.5/0.9, p10–p90 coverage (calibrated AND
raw) with band width, plus point-inside-realized-range hit-rates, skill vs persistence
(`1 − MAE/MAE_persistence`), and the market-entity view (equal-weight aggregate tracking error;
the fraction of close-error variance that is market-wide vs idiosyncratic). Multi-model
comparisons are restricted to (ticker, origin day) pairs present for all compared models.

## Production-safety safeguards

The five production-safe-loop primitives (spec → circuit breaker → ledger → bounded loop →
review surface), mapped onto this system:

| Primitive | Where it lives here |
|---|---|
| **Spec — define done first, no mid-run revisions** | `HarnessConfig` is the frozen spec: what runs (models/universe/window), what "done" is (`--start`→`--end`/archive end). Its hash is sealed into every checkpoint; a resume with changed non-resume-safe flags — or a silently different ticker universe — is REFUSED. |
| **Circuit breaker — hard ceilings, pre-flight, loud** | `--max-steps` / `--max-hours`: per-invocation ceilings checked **before** each step's work (never after the spend). A trip prints an unmissable `=== CIRCUIT BREAKER CHECKPOINT ===` banner even if the caller ignores the status, checkpoints cleanly, and exits 3; `--resume` continues later. `steps == limit` is allowed; the next step trips — no grace period. |
| **Ledger — append-only audit trail** | The gzip-member JSONL ledger: no updates, no deletes, every record stamped with schema version + seed. SHA-256 shard fingerprints ride in the checkpoint; editing the ledger makes the next resume REFUSE. |
| **A loop that respects its boundaries** | The harness exits only through defined paths — completed, interrupted (signal), budget_tripped, REFUSED, runtime-failure-with-checkpoint — each with its own exit code. There is no silent path. |
| **Review surface — output vs commitment, humans gate downstream** | `report.json`/`analysis.md` are generated only from ledgered measurements (no hand-written claims), raw vs calibrated bands are both scored so calibration can't hide, and the scope boundary guarantees downstream systems get nothing automatically: forecasts never touch `config/` or live sizing — a human must deliberately consume them. |

## Reliability mechanics (what makes 26 years resumable)

- **Ledger**: append-only gzip-member JSONL shards per model/kind/year (~2–3 GB compressed at
  full scale). Every append is one gzip member, so a crash can only damage un-checkpointed
  tail bytes.
- **Checkpoints**: two-phase directory swap; model weights as `metadata.json` + `weights.npz`
  (**no pickle, ever** — torch state crosses disk as named arrays; the GBM refits
  deterministically on load instead of serializing trees).
- **Resume contract**: shard sizes + SHA-256s ride in the checkpoint; a longer-than-recorded
  shard is truncated back (un-checkpointed steps re-run deterministically); a mismatched one
  REFUSES. Per-(day, purpose) seeds derive from the master seed, so resumed and uninterrupted
  runs are byte-identical — enforced by the flagship harness test.
- **Ledger filesystem boundary**: create, append, resume, and checkpoint verification require
  descriptor-relative, no-follow filesystem operations (supported on Linux and macOS).
  They refuse symlinks below the selected run directory, nonregular shard files, hard-linked
  shards, and malformed checkpoint paths. Recovery hashes and truncates the same open file;
  cleanup uses a pinned parent directory. Ordinary interrupted runs still recover their
  uncheckpointed tails. Windows currently REFUSES these operations because Python does not
  provide the required primitives there; read-only report/inspection readers remain available.
  Use a local copy with ordinary directories and single-linked shard files on a supported
  platform. The run directory selected by the operator may itself be a filesystem alias.
  See [the filesystem boundary decision](adr-octopus-ledger-filesystem-boundary.md).
- **Live re-anchoring**: the cursor is stored as a date, so a grown archive (new days, new
  IPOs) re-anchors correctly; new tickers get embedding rows initialized from the
  unknown-ticker embedding at bind time (deterministic vocabulary expansion).
