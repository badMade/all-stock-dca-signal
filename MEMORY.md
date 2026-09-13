# Project memory: runners, reports, and prediction tools

Recorded from the local-console implementation session on 2026-09-06.
This is a navigation aid, not a replacement for current code, configuration,
or the user's current request. Recheck availability and configuration before
execution. Do not interpret stored schedules or examples as live authorization.
See [HANDOFF.md](HANDOFF.md) for the implementation checkpoint and continuation status.

## Start here

The user's preferred interface is one simple Python launcher, `from.py`, with
daily summaries, timing updates, Octopus predictions and easy testing.

```bash
python3 from.py                       # browser UI + isolated synthetic demos
python3 from.py --check               # runtime, dependency and action readiness
python3 from.py --once                # demo, saved summaries, JSON output, exit
python3 from.py --run demo-dca         # core DCA smoke run
python3 from.py --run demo-neural      # short actual neural Octopus smoke run
python3 from.py --run test-signals     # existing tests for all seven signal slots
python3 from.py --local               # observe existing local run artifacts
python3 from.py --local --root <cron-root> --octopus-run <research-run>
```

Use [docs/local_console.md](docs/local_console.md) for setup, all flags and the
local API. The default port is 8765; `--port 0` chooses an available port.
Do not assume a server from an earlier session is still running.

Before writing a new runner or reconstructing strategy logic, consult the
canonical entry-point index:

```bash
python3 scripts/agent_entry_points.py
python3 scripts/agent_entry_points.py --search <keyword>
```

The core is Python 3.11+ and standard-library-only. Jobs use the repository
`.venv` when available; research can use a separate `--research-python`.

## Interface wiring

| Component | Responsibility |
| --- | --- |
| `from.py` → `dca_console.py` | Loopback UI, fixed actions, snapshots, daily JSON/Markdown summaries |
| `dca_console_jobs.py` | One active job, streamed/redacted logs, deadlines, persisted job history |
| `dca_console_data.py` | Observed daily runs, confirmed fills, workload proxies, configured timetable |
| `dca_console_octopus.py` | Bounded stdlib reads of forecast CSV, daily JSON, report JSON and checkpoint metadata |
| `console/index.html`, `app.js`, `styles.css` | Browser dashboard, filtering, charts, downloads and documentation |

The UI refreshes about every 15 seconds. The running launcher also updates
today's summary in local mode, or the selected demo date in demo mode, in the
background. Summaries are evolving views of available
artifacts, saved below `<console-output>/summaries/<demo-or-local>/<date>.*`.
Demo output stays under `<console-output>/demo` and is excluded from the local
view. Stopping the launcher stops updates; it installs no external scheduler.

## Existing execution runners

`run_dca.py` is the CLI/orchestrator. Its CLI supports dry-run/demo execution;
live runs require injected broker/data providers. `dca_runner.py` implements the
live gate/plan/execute APIs. Create the plan with `plan_orders(candidates, snapshot,
tier)`, then pass that returned plan and the same snapshot/tier objects to
`execute_plan()`. An `assess()` JSON report is not a live plan. See
[docs/live_wiring.md](docs/live_wiring.md).

These are the seven nominal ET signal slots; consult current configuration for
active scheduling. Candidate commands perform Gate 5, not order placement.

| ET slot | Candidate module (`python3 -m ...`) | Strategy/executor |
| --- | --- | --- |
| 09:35 | `scripts.generate_market_open_candidates` | `market_open_dca.MarketOpenDCAExecutor` |
| 10:30 | `scripts.generate_morning_candidates` | `morning_dca.MorningDCAExecutor`, morning live mode |
| 11:30 | `scripts.generate_intraday_candidates --slot 1130` | `intraday_dca.IntradayDCAExecutor` |
| 12:30 | `scripts.generate_midday_candidates` | `midday_dca.MiddayDCAExecutor` |
| 13:30 | `scripts.generate_intraday_candidates --slot 1330` | Same intraday executor, different anchor |
| 14:30 | `scripts.generate_intraday_candidates --slot 1430` | Same intraday executor, different anchor |
| 15:30 | `scripts.generate_buy_candidates --slot 1530ET` | `near_close_dca` signal path |

All require `--trading-day` and `--market-data`. Midday and final-buy also
require verified `--deployed-last-7d-usd` from the risk subsystem; missing
history must not be treated as zero deployment. The 09:35 input uses Robinhood
only. Trend-sensitive slots require the appropriate intraday bar window.
The 10:30 preflight reader degrades to the full universe with a warning when
preflight evidence is absent/invalid; it is deliberately non-blocking.
The three intraday slots share a pooled budget, enforced outside Gate 5.
`scripts/stream_candidates.py` is the shared implementation for the six
executor-class slots; the 15:30 signal engine is separate.

## Reports, timing, and maintenance

| Need | Existing module or entry point |
| --- | --- |
| Full run reports | `run_dca.py --report`; `dca_reporting.py` |
| Weekly review | `dca_weekly_dashboard.py` |
| Confirmed purchase ledger | `python3 -m scripts.purchase_history`; supports `--additional-root` |
| Automation workload | `dca_cost_tracker.py`; proxy units, not billed dollars or tokens |
| Notion report/sync helpers | `dca_notion_sync.py`; external delivery needs its connector |
| Risk and deployment evidence | `python3 -m dca_risk`; inspect its evidence block before using totals |
| Run drift | `scripts/drift_detection.py --run-dir <run-dir>` |
| Alerts and failed gates | `python3 -m scripts.alert_digest`; local artifacts and supplied mail exports |
| Scheduled/manual provenance | `scripts/run_classification.py` and `scripts/scheduled_artifacts.py` |
| Ticker archive refresh | `python3 -m scripts.run_ticker_maintenance update`; requires configured updater |
| Recalculate calibration | `python3 -m scripts.run_ticker_maintenance calibrate` |
| Event-gated calibration | `python3 -m scripts.run_ticker_maintenance event-calibrate` |
| Market visualization | `python3 -m scripts.generate_market_graph` |

`config/scheduled_tasks.json` describes intended task timing; it is not proof
that an external scheduler is registered or ran. `config/market_calendar.json`
and the existing calendar helpers govern holiday/early-close handling.
Use Eastern Time, including DST. Simulated orders, candidates, submitted orders,
and confirmed priced fills are distinct. Missing evidence remains unavailable.

## Wondrous Octopus and adjacent prediction tools

Wondrous Octopus is the whole-market, walk-forward OHLC forecasting project.
Its models are persistence, quantile GBM, and neural PyTorch Octopus. It predicts
the next trading row using prior data, scores matured forecasts, supports
online updates, and reports uncertainty and encoder-health diagnostics.

| Purpose | Command (prefix with the research Python) |
| --- | --- |
| Synthetic baseline replay | `wondrous-octopus/run_walkforward.py --demo --run-dir <fresh-dir>` |
| Estimate real archive replay | `wondrous-octopus/run_walkforward.py --preset quick --dry-run` |
| Start real archive replay | `wondrous-octopus/run_walkforward.py --preset quick --run-dir <fresh-dir>` |
| Daily scoring/update/forecast | `wondrous-octopus/daily_update.py --run-dir <run-dir>` |
| Inspect forecasts | `wondrous-octopus/inspect_run.py --run-dir <run-dir> --tomorrow` |
| Inspect model performance | `wondrous-octopus/inspect_run.py --run-dir <run-dir> --summary` |
| Regenerate regime metrics | `wondrous-octopus/run_walkforward.py --report-only <run-dir>` |

Read [docs/wondrous_octopus.md](docs/wondrous_octopus.md) before changing replay,
resume, or recovery behavior. The standalone `--demo` defaults to persistence
and GBM; the launcher's `demo-neural` action explicitly tests PyTorch on ten
replay days. NumPy/pandas/scikit-learn are optional research dependencies;
PyTorch is additionally required for the neural model.

Important artifacts within an Octopus run:

- `daily/<date>_summary.json`: scoring, health and daily-update digest.
- `daily/<date>_next_day_forecasts.csv`: adjusted-dollar OHLC and quantile bands.
- `report/report.json`: measured errors/coverage by model, component and regime.
- `checkpoint/harness_state.json`: cursor and outstanding forecasts; `run_meta.json`
  records the archive/universe selection needed for continuation.

Historical regimes include dot-com bust, GFC, COVID, 2022 rate shock and other
periods, but only measured periods have results. The baseline synthetic demo
covers 2019–2020. Unresolved target dates must remain unresolved; checkpoint
scoring dates are not automatically forecast target dates. The console's latest
research view is independent of its selected DCA history date. Watch for stale
report/CSV artifacts relative to the checkpoint.

`ml-forecaster/` is a separate Ridge sizing-research pipeline, not Octopus.
`ml-forecaster/train.py --dry-run` trains/evaluates without publishing sizing
changes. `ticker-advisor/` proposes universe changes; `trading-strategies/`
contains independent backtests/sentiment tools; `ticker-market-breadth/` collects
breadth data; `dca-sizing-signals/` holds research interfaces. Each keeps its own
dependencies and documentation. `production-safe-agent-loop/` supplies library
primitives and is not an automatically wired live executor.

## Roots, boundaries, and verification

Resolve run roots through `dca_paths.cron_tracking_root()` / `stream_root()` and
`DCA_CRON_TRACKING_ROOT`. Resolve ticker history through
`dca_ticker_paths.configured_ticker_history_dir()` / `DCA_TICKER_HISTORY_DIR`.
Never record account values, credentials, tokens or `.env` contents here.
Octopus never changes trading configuration or live DCA sizing. The console
does not submit live orders or create external schedules.

At the implementation checkpoint, lint and 62 focused console/index tests
passed, along with core, baseline-forecast, neural-forecast and seven-slot smoke
checks. Six inverse-Martingale corporate-action tests failed identically on
pristine pre-console HEAD; see the dated verification record in
[docs/local_console.md](docs/local_console.md). These results are historical,
not a claim about future repository health. Re-run checks relevant to new work.
