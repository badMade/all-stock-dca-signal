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
| --- | --- | --- |
| Backward in time | Causal attention over each ticker's trailing window → forward head (point + p10/p50/p90 per component) | **Yes — the only path forecasts use** |
| Both directions in time | Training-only masked-day reconstruction (BERT-style): reconstruct a day's bar from context on both sides | Training + diagnostics only |
| Sideways across the market | A Perceiver-style per-day market encoder: learned latent tokens cross-attend to every ticker's day-*t* features; every window position consumes its day's market embedding | Yes — leakage-free by construction (all inputs are day ≤ *t*) |
| Sideways across a peer group | Correlation-based style clustering: the universe is re-clustered periodically from a trailing window of returns, giving each ticker a peer group without any sector or fundamental input. The group's median return and the ticker's rank inside it become features; the cluster label itself is embedded by the octopus and split on by the GBM | Yes — each refit reads only bars at or before its own refit date, and labels are aligned across refits so a persisting group keeps its id |

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
  horizon-1 form (generalized to an h-row embargo under "Multi-horizon forecasting") of ml-forecaster's walk-forward leakage guard.
- Missing/corrupt/short tickers are **never blockers**: they are logged, counted in the run
  report, and skipped; a short-history ticker joins automatically once it has 60 prior rows.
  Only an archive with zero usable tickers refuses.

## Multi-horizon forecasting (t+1 .. t+90)

`--max-horizon N` (default 1 = the single-step system, byte-identical; `--eval-horizons H,H,...`
default `5,10,15,20` clipped to N) makes every model that supports it emit a forecast for **every**
trading row t+1..t+N each day: the OHLC point plus p10/p50/p90 per component. Only the *evaluation
horizons* are matured, scored, ledgered (`horizon_score` records) and calibrated; the full 1..N
forecast of the newest origin day is exported to `<run_dir>/horizons/<model>.csv` at every checkpoint
(calibrated bands, log-returns vs the origin close, one row per ticker x horizon). Widen the
evaluation set as the model earns it by starting a new run -- both flags are frozen run properties
and a resume that changes either (or turns horizons on/off) is REFUSED, like every other flag.
gbm_quantile stays single-horizon (a run with only gbm and `--max-horizon > 1` is refused).

**"t+h" is the ticker's own h-th next trading row**, the same convention horizon 1 always used.

### Design choice: direct heads with a horizon embedding, not recursive rollouts

Recursive rollout (predict t+1, feed it back, repeat 90 times) needs the model to also predict the
inputs of the next step -- every feature, including the cross-sectional and market blocks -- and
compounds its own error 90 times; its t+90 band is a product of 90 unvalidated steps and its
calibration can only be measured at the end of the chain. **Direct** forecasting trains each horizon
against the realized h-day outcome, so the error at h=20 is exactly the error being optimized and
scored, never an accumulation of simulated ones. The octopus keeps its horizon-1 head untouched and
adds one extension head for horizons 2..N: a shared trunk summary `z`, then
`hidden_h = GELU(W z + E[h])`, `out_h = V hidden_h` with a learned horizon embedding `E` (so
neighbouring horizons share statistical strength instead of 90 independent heads each seeing few
labels). Outputs are in per-sqrt(h) units and multiplied back by sqrt(h) so a t+90 residual does not
swamp the short horizons' loss. A single-horizon model owns none of these parameters and initializes
exactly as before. Persistence's yardstick is "the bar stays today's bar", its horizon-1 residual
band widened by sqrt(h) -- the random-walk assumption, stated, then corrected by calibration.

### The horizon-h embargo (the leakage guard, generalized)

Horizon 1's rule is "trainable once the next row's date has passed". Horizon h is: **the target of
origin row i is usable only once row i+h has been revealed** -- an embargo of h trading rows.
`horizons.py` is the single place it lives: `usable_horizon_count(series, row, asof, H)` turns an
explicit revealed-through date into "horizons 1..K are knowable" (always a prefix), and
`horizon_targets()` returns NaN beyond it. It is never "is the target finite?" -- the replay panel
holds the whole archive, so a future bar always exists in memory. Applied everywhere a target is read:

- **fit / retrain**: `asof` = the fit cutoff (`window_end`); a row near the cutoff trains only the
  horizons its revealed future covers;
- **online update**: `asof` advances only to a *revealed* target date, so replay-buffer rows and
  recent days are re-used each day with more of their horizons unlocked;
- **scoring**: a horizon-h forecast is scored on the cursor day with `asof` = that day, and not
  looked at at all before `calendar[origin_pos + h]`.

Tests corrupt every bar after `asof` and assert the usable targets (and the trained extension-head
weights) do not move, with a negative control showing a leaky `asof` fails them. An external loop
that never declares an `asof` (train_foundation.py) gets *no* extension-head targets, not unbounded ones.

### Per-horizon calibration, metrics, ledger

- `model_interface.HorizonConformalCalibrator`: one `ConformalCalibrator` (own per-component scales,
  own once-per-target-day watermark) per *evaluated* horizon. A t+20 band is never widened because
  t+5 missed. Emitted-but-unevaluated horizons use scales interpolated geometrically in log h between
  the nearest evaluated horizons (clamped beyond them): an **assumption, not a measurement** --
  widening `--eval-horizons` is how it becomes one.
- `metrics.build_horizon_report` / `report.md`: error, pinball, calibrated vs raw coverage and band
  width **by model x horizon x component**, skill vs persistence *at the same horizon*, and a
  model x horizon x year table (close) so error by horizon is comparable over time. Horizon 1 comes
  from the native `score` records, h>=2 from `horizon_score`.
- Ledger kind `horizon_score` (sharded by origin year like the rest; SHA-256 fingerprinted and
  truncated-on-resume identically). Pending forecasts checkpoint as `horizon_pending.npz`
  (`allow_pickle=False`; SHA-256 in the harness state, mismatch REFUSES) -- no pickle anywhere.
- Cost: score records grow by the number of evaluation horizons; memory by (tickers x max eval
  horizon days x models) small arrays. Keep the evaluation set small until it is wanted.

### Daily updating and tracking against real data

Nothing here costs money or needs an API: truth is the same per-ticker CSV archive (`ticker_data`,
refreshed by the existing `tickers-daily-update` / yfinance prefetch). Each day's harness step (the
replay, and `daily_update.py` which drives the same step) does, in order: reveal the new day, score
every matured evaluation-horizon forecast against it, nudge that horizon's calibrator, run the
online update (gradient steps on recent + replay days, now including the horizon head with the
embargo-unlocked horizons), then forecast t+1..t+N again. Out-of-sample error per horizon is
therefore measured against real archived closes continuously, with no hand-written claims.
Limits to know: the model is *adjusted* online, not re-optimized to an "optimal match" -- it is a
small online learner scored honestly, and a recovery retrain stays an operator decision.
`daily_update.py`'s own `*_next_day_forecasts.csv` is still horizon-1 only; the 1..N forecast is
`horizons/<model>.csv`. Research artifacts only; nothing reads or writes `config/` or live sizing.

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

### Optional feature blocks

The default feature layout is 36 columns (23 per-ticker, 6 cross-sectional, 7 market) and is
what every existing checkpoint, frozen normalization vector and octopus `feat_proj` weight
matrix was built against. `features.build_panel(..., extra_features=("calculus",))` appends an
opt-in **calculus block** of 5 more per-ticker columns:

| Column | What it adds | Warm-up |
| --- | --- | --- |
| `ret_accel` | The literal discrete second derivative of log price (`ret_close.diff()`). The base set carries level and velocity but no curvature | 2 rows |
| `vol20_roc` | Which way `realized_vol_20` is *moving*, not just where it sits: a log-ratio against its own reading 5 rows back, neutral-and-finite when either endpoint is zero, clipped at ±3.0 | 25 rows |
| `sg_slope`, `sg_curvature` | Savitzky-Golay local derivatives: a degree-2 polynomial fitted to the trailing 11 log prices, differentiated at the window's right edge. Noise-robust without a moving average's lag | 10 rows |
| `frac_diff_close` | Fractionally differenced log close (López de Prado, *AFML* ch. 5) at `d = 0.4`: sheds the trend while keeping the long memory integer differencing destroys | 54 rows |

Three properties are load-bearing and enforced by `tests/test_features.py`:

- **Off by default, appended at the tail.** Indices 0..35 keep meaning what they meant, because
  `harness._BAR_FEATURE_SLICE` and `baseline_model`'s `FEATURE_NAMES.index(...)` constants are
  positional and resolved at import against the default tuple.
- **Causal, hand-rolled Savitzky-Golay.** `scipy.signal.savgol_filter` is *centered*: it reads
  future rows and would break the no-lookahead invariant (and scipy is only a transitive
  dependency here). The kernels are fixed FIR taps derived from a Vandermonde pseudo-inverse.
- **The fractional-differencing kernel is anchored and width-capped.** Truncated binomial
  weights do not sum to zero (`sum(w) = +0.136` at `d = 0.4`), so a raw application leaks the
  price *level*: a ticker priced 100x higher with an identical return shape lands +0.63 away on
  a column whose own signal std is 0.033, and the panel fits one global mean/std per column.
  Each window is anchored on its own oldest observation, which removes that exactly. `tau` is
  `1e-3` so the kernel truncates at 55 taps rather than the 282 a `1e-4` threshold would need:
  the harness only forecasts rows where every feature is finite, so a kernel wider than
  `min_history` would delay every ticker's first forecast and drop short-history tickers.

No CLI reaches this yet, by design. Wiring `--features` through `HarnessConfig` (which puts the
layout into the config hash, so a resume against a differently-shaped checkpoint refuses cleanly
instead of failing inside `load_state_dict`) is a separate change.

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
| --- | --- | --- | --- | --- | --- |
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
`daily/<date>_next_day_forecasts.csv`** -- tomorrow's predicted O/H/L/C with bands, in dollars,
for every eligible ticker, with an expected calendar date (`expected_target_date`,
`expected_target_date_basis` = `calendar` | `weekday_estimate`; the resolved `target_date` stays
unset until the ticker's next data row exists) -- and prints a one-line digest. Idempotent (`NOOP` on rerun);
catches up automatically if days were missed. Scheduling it is the operator's choice.

### Scheduling it (launchd)

```bash
sed -e "s|REPLACE_WITH_REPO_PATH|$PWD|g" -e "s|REPLACE_WITH_OCTOPUS_RUN_DIR|$HOME/octopus/quick|g" \
  deploy/launchd/com.all-stock-dca.octopus-daily-update.plist.template \
  > ~/Library/LaunchAgents/com.all-stock-dca.octopus-daily-update.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.octopus-daily-update.plist
launchctl kickstart gui/$(id -u)/com.all-stock-dca.octopus-daily-update    # run once now to test
```

Runs 18:30 Mon-Fri local time (after the close and the archive refresh); log at
`/tmp/dca-octopus-daily-update.log`. Needs an existing run directory with a checkpoint.

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

## The ensemble (`stacking.py`)

A regime-weighted blend of the models a run already scored. It needs nothing new: a ledgered
`score` record carries the forecast, the realized truth *and* the per-quantile pinball loss, so
the ensemble is a second pass over records already on disk. No re-run, no new training pipeline.

```python
from stacking import replay
for record in replay(run_dir, ("persistence", "gbm_quantile", "octopus")):
    ...   # a score record under model name "ensemble"
```

- **Weights** are an exponentially-weighted inverse mean-pinball per *(model, component)*, so a
  model can win the `close` blend and lose the `high` one. Non-negative by construction, with a
  floor under every model: today's worst is the next regime's best, and a model at weight zero
  never generates the evidence that would win its weight back. Default half-life 60 revealed
  days, floor 0.02.
- **Blending is vincentized** (the quantile *functions* are averaged level by level). A
  component's p10/p50/p90 all share that component's weights, and a convex combination of sorted
  vectors stays sorted, so the ensemble can never emit a **crossed band**.
  **Bar validity is not guaranteed**, and this is the one place the convexity argument does not
  reach: `max` being convex would preserve `high >= max(open, close)` only under *one* weight
  vector shared by every component, and weights here are deliberately per-component. So `open`
  can follow model A while `high` follows model B, and two individually valid bars can blend into
  an impossible one. Measured: models at *(open 0.00, high 0.001)* and *(open 0.10, high 0.101)*,
  with `open` following the second and `high` the first, blend to a high of 0.001 below an open of
  0.100. `finalize_forecast()` repairs it, so what leaves is always a possible bar, but the repair
  is real work rather than defense in depth: its counts ride out on every record `replay()` emits,
  per the repo's repairs-are-counted-never-silent contract.
- **Causality** is structural: `replay()` reads the weights once per target day *before* blending
  any of that day's records, and folds that day's losses in only afterwards. The regression test
  perturbs every score after a cutoff and asserts nothing at or before it moves, the same shape
  as the feature layer's no-lookahead test.
- **Decay is once per revealed day**, not once per record. A full-universe day matures thousands
  of scores; per-record decay would collapse a 60-day half-life into about a day of replay and
  make the weights depend on universe size. Same rule `ConformalCalibrator.observe_batch()`
  follows. A model absent from a day is left untouched rather than decayed, since decaying it
  would read as improvement in an inverse-loss scheme.
- **Honest sample**: only *(ticker, origin day)* pairs every requested model forecast are
  emitted, matching the intersect-to-shared-pairs rule the multi-model reports already use.
- **Memory is bounded by one year, not by the run.** The ledger already shards by the origin
  date's year, so `replay()` walks those shards in order (`Ledger.available_years`) and holds
  only the year being processed. A forecast matures on its ticker's next trading row, so one
  originating in year *Y* matures in *Y* or, across the new-year boundary, in *Y+1* and no
  later; every pair maturing in *Y* is therefore complete once shard *Y* is loaded, and the
  handful that cross carry forward. Records are projected down to the fields the blend actually
  reads as they are loaded. A full-universe multi-decade run is millions of records.

Records come out under the model name `ensemble` in the same shape `harness._score_one()` writes,
so `metrics.py`'s regime × model × component tables aggregate it beside its components unchanged.
Library-only for now: no CLI reaches it, and it is not wired into the harness's live predict loop.

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
| --- | --- |
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
  shards on every path that adopts a shard the process did not create, and malformed
  checkpoint paths. Appends prove ownership by inode instead: the first append creates the
  shard `O_EXCL` and pins it, later appends require the same inode, so a run survives the
  cloud-sync and backup agents that hard-link freshly written files (Google Drive for Desktop
  holds such a link for 7-11 seconds after every append) while still refusing a shard swapped
  for any other file. Resume, unrecorded-shard cleanup and read-only verification keep the
  unaliased requirement and wait up to 30 s for a sync agent's link to clear before refusing,
  re-checking each time that the shard name still resolves to the opened inode.
  Recovery hashes and truncates the same open file; cleanup uses a pinned parent directory. Ordinary interrupted runs still recover their
  uncheckpointed tails. Windows currently REFUSES these operations because Python does not
  provide the required primitives there; read-only report/inspection readers remain available.
  Use a local copy with ordinary directories and single-linked shard files on a supported
  platform. The run directory selected by the operator may itself be a filesystem alias.
  See [the filesystem boundary decision](adr-octopus-ledger-filesystem-boundary.md).
- **Live re-anchoring**: the cursor is stored as a date, so a grown archive (new days, new
  IPOs) re-anchors correctly; new tickers get embedding rows initialized from the
  unknown-ticker embedding at bind time (deterministic vocabulary expansion).
