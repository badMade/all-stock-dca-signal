# ml-forecaster

A pooled, regularized-linear ML forecaster that publishes a bounded per-ticker sizing
*adjustment* for the graded DCA strategy's buy multiplier -- the concrete implementation of the
Phase 2 idea sketched (but never built) in `docs/ticker_advisory.md`'s "Phase 2 (not built yet):
influencing sizing, not just universe membership" section. It never places an order, never calls
Robinhood, and never touches `near_close_dca.py`'s sizing math directly -- see "What this does
NOT do" below.

This is a sibling of `ticker-advisor/`, `production-safe-agent-loop/`, and `trading-strategies/`:
a self-contained, heavy-dependency subproject with its own `pyproject.toml`/`requirements.txt`/
tests, excluded from the core repo's stdlib-only ruff and coverage gates (see the root
`CLAUDE.md`).

## What this does

Reads each active ticker's local OHLCV archive
(`dca_ticker_paths.configured_ticker_history_dir()`), engineers a small set of no-lookahead daily
features, and trains ONE pooled Ridge-regularized linear regression across the whole ticker
universe to predict the forward 5-trading-day realized return on a dip day (a day where price
fell versus the prior close). The prediction is clipped to a sane band and linearly rescaled into
a bounded (default ±20%, schema-capped at ±50%) per-ticker multiplier, then -- only if it clears a
fail-closed quality gate -- published to ml-forecaster's own shadow incumbent file
(`<cron root>/sizing_suggestions/ml_forecaster/incumbent.json`) and appended to the shared
suggestion history. It does NOT write `config/ml_sizing_adjustment.json` by default: that file
feeds `near_close_dca.graded_buy_multiplier()`, and sizing is still being determined by comparing
independent suggesters, so promoting any suggester into it is a deliberate human step (see
`docs/sizing_suggestion_history.md`). Pass `--output` to publish to the live file on purpose.

## Why pooled, why linear, why Ridge

Per-ticker archives in this repo are small -- a few hundred to ~1500 rows once rolling-window
warmup and the forward-return horizon are trimmed -- nowhere near enough to fit a separate model
per symbol without overfitting. Pooling every ticker's rows into one shared fit multiplies the
effective sample size and keeps the model's coefficients directly inspectable, which matters
because this model's output perturbs a REAL per-trade dollar amount. `model.py`'s own docstring
has the full reasoning, including why a pooled gradient-boosted-tree model (a reasonable v2
upgrade) is deliberately not what v1 does.

## Install

```bash
cd ml-forecaster
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python train.py                                    # train + evaluate + guarded-publish
python train.py --tickers AAPL MSFT GOOGL --n-splits 4 --alpha 2.0
python train.py --dry-run                           # train + evaluate + write artifacts, never publish
python train.py --history-dir /path/to/ticker_data --output /path/to/config/ml_sizing_adjustment.json
python -m pytest tests/
```

`train.py` is meant to be invoked by an external scheduled agent session on a weekly cadence
(e.g. after Friday close) -- per the root `CLAUDE.md`'s "This is a library driven from outside,
not a self-contained app" framing, this repo makes no network calls and has no in-repo scheduler,
so `ml-forecaster/` contains no cron/scheduling code of its own. `train.py` is only the CLI such
an external cadence calls, exactly like `scripts/daily_per_ticker_calibration.py` and
`scripts/generate_per_ticker_sizing.py` already are for the (non-ML) per-ticker sizing
calibration.

Each run writes `<run-id>/model.json` (the fitted model, plain JSON -- see "No pickle, ever"
below) and `<run-id>/eval_report.{json,md}` (the walk-forward evaluation, the proposed
adjustments, and the publish decision) under `--models-dir`, which defaults to
`dca_paths.stream_root("ml_forecaster")/models` -- the same `$DCA_CRON_TRACKING_ROOT`-honouring
artifact-root convention every other scheduled stream in this repo uses, not a directory inside
`ml-forecaster/` itself (which could be read-only, e.g. under an installed wheel's
site-packages) -- and is always overridable via `--models-dir` (Codex review: "Route model
artifacts through the shared artifact root"). The now-vestigial `models/.gitkeep` (and
`models/.gitignore`) checked into this directory remain only as a convenience for an explicit
`--models-dir ml-forecaster/models` override; run artifacts under the DEFAULT location are
gitignored the same way the root `CLAUDE.md` gives `cron_tracking/`/`artifacts/`.

## The pipeline

| Module | Role |
| --- | --- |
| `features.py` | OHLCV archive CSV -> per-day, per-ticker feature rows. Own small loader -- does NOT cross-import `ticker-advisor/pricing.py`, keeping the two sibling tools independently deployable. Reads the archive location via `dca_ticker_paths.configured_ticker_history_dir()` through a `sys.path` splice to the repo root, the same pattern `ticker-advisor/pricing.py`/`advise_tickers.py` already use for that import. |
| `model.py` | The pooled Ridge model: `fit()` (needs scikit-learn), `TrainedModel.predict_return_pct()`/`predict_multipliers()` (plain numpy, no scikit-learn needed), JSON `to_dict()`/`from_dict()`/`save()`/`load()`, and `rescale_return_to_multiplier()`. |
| `evaluate.py` | Chronological expanding-window walk-forward backtest: MAE/RMSE/directional hit-rate for the model and two naive baselines (predict-zero, predict-trailing-mean), per fold and averaged overall. |
| `train.py` | CLI entrypoint: discovers tickers, builds the pooled dataset, walk-forward-evaluates, fits the final model, computes proposed per-ticker adjustments, writes run artifacts, and calls `publish.py`'s guarded gate. |
| `publish.py` | The only module that writes `config/ml_sizing_adjustment.json`. Fail-closed guarded publish -- see below. |

### Features (per ticker, per trading day -- no lookahead)

- Same-day drop-vs-prior-close percentage (`drop_vs_prior_close_pct`, matching
  `near_close_dca.py`'s own `drop_vs_previous_close` sign convention: positive means a drop).
- 20-day realized volatility (`realized_vol_20d`) -- stdev of daily returns.
- 14-day RSI-style momentum oscillator (`rsi_14`).
- Drawdown from the trailing 52-week high (`drawdown_from_52w_high_pct`).
- 5-day and 20-day trailing return (`trailing_return_5d_pct`, `trailing_return_20d_pct`).
- Volume relative to its own 20-day rolling average (`volume_rel_20d_avg`) -- degrades to a
  neutral `1.0` for any archive that carries no usable `Volume` column, rather than dropping the
  feature and destabilizing the pooled schema across tickers that do vs. don't have it.
- Ticker identity, one-hot encoded over the training-time vocabulary (`model.py`'s
  `_design_matrix()`); an unseen ticker at prediction time gets an all-zero row (no ticker-
  specific offset), not an error.
- Day-of-week, one-hot encoded.

Every one of these is computed strictly from data at-or-before its own day.
`tests/test_features.py::test_compute_features_no_lookahead` asserts this directly: a feature row
for day T is bit-for-bit identical whether computed from the full history or from history
truncated at T, and unaffected by perturbing every row strictly after T.

### Target and training population

The label is the forward 5-trading-day realized return (`features.TARGET_COLUMN`), computed from
T+1..T+5 -- deliberately forward-looking, since it's the training label, not a feature; it is
never one of the columns fed into the model's design matrix. Training and walk-forward evaluation
are both restricted to DIP DAYS (`drop_vs_prior_close_pct > 0`), matching the target's own
conditioning. Published adjustments, however, are read off each ticker's most recent available
row in the FULL feature set (not restricted to dip days) -- representing the model's current read
of that ticker's momentum/volatility/drawdown state, since the published multiplier is looked up
whenever a dip *next* occurs, not only on days that were themselves dips. This is a deliberate,
documented v1 simplification (`train.py`'s own comment on the point) -- a future iteration could
instead publish only for tickers currently mid-dip.

`--horizon` (default 5 trading days) is recorded in both `model.json` (`TrainedModel.horizon_trading_days`)
and `eval_report.json` (`horizon_trading_days`), not only in the live published config (which already
carries it via `guarded_publish()`'s own `horizon_trading_days` field) -- otherwise a non-default-horizon
run's own artifacts carry no record of which forward-return horizon their coefficients and metrics
describe, once a later publish replaces the live config or for a `--dry-run` that never reaches
`publish.py` at all.

### Walk-forward evaluation, never k-fold

`evaluate.py` uses chronological, EXPANDING-window splits only. This is time-series data: a
shuffled/k-fold split would let future rows leak into a fold's training set. Each fold trains a
fresh model on data strictly before that fold's test window and scores MAE, RMSE, and directional
hit-rate for the model plus two naive baselines (predict-zero, predict-trailing-mean) computed
from that same fold's own train/test split.

### Guarded publish -- fail closed, always

`publish.py` mirrors `scripts/daily_per_ticker_calibration.py`'s refuse-and-say-why pattern. It
publishes a new `config/ml_sizing_adjustment.json` only when the new model's out-of-sample
walk-forward MAE is STRICTLY better than both:

1. The currently-published model's own recorded `eval_metric.mae` (read back from the live config
   file itself -- an inactive or metric-less prior file imposes no floor).
2. The better of this run's own two naive walk-forward baselines.

A tie on either check is treated as "not an improvement" and refused. On any refusal the existing
`config/ml_sizing_adjustment.json` is left completely untouched (never even opened for writing),
`eval_report.json`'s `status` field records why (`blocked_regression`,
`blocked_invalid_bounds`, `blocked_invalid_adjustments`, or `blocked_missing_eval_metric`), and
`train.py`'s CLI exits nonzero.

The read (for the regression comparison), the comparison, and the write are all held under one
cross-process lock (`<config>.lock`, next to the config file) -- otherwise two overlapping
retrains could each read the same currently-published MAE, each independently pass the regression
gate, and the second to finish could overwrite a model that had already published in between. A
NaN/infinite `model_mae` or naive-baseline MAE is refused the same way a missing one already is
(every `>=`/`<=` regression comparison against a NaN silently evaluates false, so an unchecked
non-finite metric would sail through the gate rather than being blocked by it).

`train.py` also refuses to score a ticker's feature snapshot for publishing if that ticker's
latest archived row is older than `--max-staleness-hours` (same budget the published config's own
`generated_at` is held to) relative to `--now` -- a stalled per-ticker archive refresh would
otherwise let a months-old snapshot get stamped with a fresh `generated_at` and pass the live
loader's own (`generated_at`-only) freshness check.

### No pickle, ever

`model.json` is plain JSON. `model.fit()` uses scikit-learn's `Ridge` only to fit; once fit, its
`coef_`/`intercept_` are extracted into a small dataclass, and PREDICTION
(`TrainedModel.predict_return_pct`) is a plain numpy dot product against those stored numbers --
no scikit-learn `Estimator` object, and therefore no pickle bytestream, ever crosses the boundary
between this untrusted-heavy-dependency sibling tree and anything that later reads a saved
`model.json`. This avoids the arbitrary-code-execution trust boundary an unpickle would otherwise
open between `ml-forecaster/` and the trusted, stdlib-only core.

## What this does NOT do

- Does not touch `near_close_dca.py`, `graded_buy_multiplier()`, or any other live sizing/budget
  math directly -- it only writes `config/ml_sizing_adjustment.json`, which the ALREADY-EXISTING
  consumption side (`dca_config.load_ml_sizing_adjustment`, wired into
  `near_close_dca.graded_buy_multiplier()`) reads on its own schedule, fail-closed.
- Does not run on a schedule itself. No cron, no in-repo scheduler -- see "Usage" above.
- Does not call Robinhood, yfinance, or any network service. It reads only the local ticker
  history archive already on disk.
- Does not verify broker tradability or ticker-universe membership -- that's `ticker-advisor/`'s
  job, a separate sibling tool this one deliberately does not cross-import from.
