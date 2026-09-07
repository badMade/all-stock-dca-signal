# Repo map: runners, reports, and prediction tools

A structural index of everything in this repo that *runs* a trading slot, *emits* a report, or
*predicts* something. Written to be read instead of grepping the large modules.

**Read this before opening a core module.** The four biggest files cost more to read than the
rest of the repo combined:

| File | Lines | Read it only when |
|---|---:|---|
| `near_close_dca.py` | ~42,000 | You are changing signal, ladder, budget, or safeguard logic itself |
| `morning_dca.py` | 9,996 | You are changing executor-class behavior shared by all four streams |
| `dca_runner.py` | 9,302 | You are changing a gate in the live gate sequence |
| `dca_live_adapters.py` | 8,104 | You are changing the injected-callable broker/data boundary |
| `dca_weekly_dashboard.py` | 6,442 | You are changing the weekly dashboard sections |
| `run_dca.py` | 4,722 | You are changing the 3:30 PM function pipeline or the dry-run CLI |

Prefer, in order: this map, then the one `docs/` page it points at, then
`python scripts/agent_entry_points.py --search <word>` for the exact command, then the module.

**How this differs from `scripts/agent_entry_points.py`.** That script is the *task*-oriented
index ("I need to do X, what command?") and is test-enforced against reality by
`scripts/tests/test_agent_entry_points.py`. This page is the *structural* index ("what exists,
who owns which slot, what is wired into live money and what is not"). They are complements: the
script answers what to run, this answers what is there.

---

## 1. Runners

### 1.1 Three entry points, one strategy

`docs/live_wiring.md` is the full treatment. The short version, because getting this wrong is the
most expensive mistake available here: **a fix in one does not propagate to the other two.**

| Entry point | Shape | Who drives it | Owns which slots |
|---|---|---|---|
| `run_dca.run_slot()` / `run_pipeline()` | Function pipeline over `near_close_dca` primitives | `run_dca.py` itself, the only real `__main__` | `1030ET`, `1230ET`, `1530ET` (see `SLOT_MAP`); `0935ET`/`1130ET`/`1330ET`/`1430ET` are in `DISABLED_SLOTS` and raise `UnsupportedSlotError` |
| `MorningDCAExecutor` + 3 subclasses | Class hierarchy, one per stream | An external agent calling `.run(now, data, broker=...)`. No in-repo caller, no CLI. | all four executor streams |
| `dca_runner.py` | Gate sequence over injected callables | An external agent session holding Robinhood MCP access | all seven slots via `SLOT_SPECS` |

### 1.2 Slot ownership table

Cron IDs are the live scheduler's task IDs (`docs/schedule_manifest.md` is canonical).

| ET | Slot | Cron ID | Executor class | `dca_runner` SlotSpec | `run_dca.run_slot` | Artifact root + pointer |
|---|---|---|---|---|---|---|
| 9:35 AM | `0935ET` | `2ba19526` | `MarketOpenDCAExecutor` (`market_open_dca.py`) | `"0935"` / market-open | disabled | `market_open_dca/` + `latest_market_open_run.json` |
| 9:45 AM | preflight | `bb4f6ef1` | `MorningDCAExecutor` watchlist + preflight modes | n/a (no trades) | n/a | `morning_dca/` |
| 10:30 AM | `1030ET` | `42173154` | `MorningDCAExecutor` (`morning_dca.py`) | `"1030"` / morning | `("morning", "1030ET")` | `morning_dca/` + `latest_morning_run.json` |
| 11:30 AM | `1130ET` | `7be82b97` | `IntradayDCAExecutor` (`intraday_dca.py`) | `"1130"` / intraday | disabled | `intraday_dca/` + `latest_intraday_run.json` |
| 12:30 PM | `1230ET` | `a6239dae` | `MiddayDCAExecutor` (`midday_dca.py`) | `"1230"` / midday | `("midday", "1230ET")` | `midday_dca/` + `latest_midday_run.json` |
| 1:30 PM | `1330ET` | `7be82b97` | `IntradayDCAExecutor` | `"1330"` / intraday | disabled | `intraday_dca/` + `latest_intraday_run.json` |
| 2:30 PM | `1430ET` | `7be82b97` | `IntradayDCAExecutor` | `"1430"` / intraday | disabled | `intraday_dca/` + `latest_intraday_run.json` |
| 3:30 PM | `1530ET` | `10ce362c` | none (this is the `run_dca`/`near_close_dca` path) | `"1530"` / final | `("afternoon", "1530ET")` | `scheduled_runs/YYYYMMDD_1530ET/` + `latest_scheduled_run.json` (legacy `ce16ba6b` root) |

Two consequences worth internalizing:

- The 3:30 PM slot is the **only** one the function pipeline still owns, and the only one where
  `dca_whole_share_accumulator` is wired in (`near_close_dca._run_once_impl`, see
  `docs/whole_share_accumulation.md`).
- The three intraday times share one cron ID and one executor class. A change to
  `IntradayDCAExecutor` lands on all three at once.

Manual runs land in each stream's own `manual_runs/<timestamp>/` and never touch a pointer.
Route every scheduled-vs-manual decision through `scripts/run_classification.py`
(`docs/run_classification.md`).

### 1.3 The executor class hierarchy

`morning_dca.py` holds `DCAExecutor` and `MorningDCAExecutor`; the other three modules are thin
subclasses (297 to 528 lines each) that override identity constants and a few flags only:

```
DCAExecutor                     morning_dca.py     (base)
└── MorningDCAExecutor          morning_dca.py     10:30 live + 9:45 watchlist/preflight
    ├── MarketOpenDCAExecutor   market_open_dca.py 9:35
    ├── MiddayDCAExecutor       midday_dca.py      12:30
    └── IntradayDCAExecutor     intraday_dca.py    11:30 / 1:30 / 2:30
```

Each subclass module exports a `<Stream>Config` dataclass, a `load_<stream>_config()`, a
`*_PROFILES` mapping (mode -> `cron_id`, `run_type`, window bounds, `state_root`,
`folder_suffix`, `scheduled_run_id_prefix`, schema version), and its `*_LIVE_CRON_ID`. That
profile dict is the fastest way to learn a stream's identity without reading the base class.

### 1.4 The gate sequence (`dca_runner.py`)

Gate 0 market day -> Gate 1 time sanity -> Gate 3 window -> Gate 4 sole executor -> Gate 6 budget
-> Gate 7 placement -> Gate 9 evidence. Public surface, in call order:

`market_day_status` / `MarketDayStatus` -> `gate_time_sanity` -> `gate_trading_window` ->
`gate_sole_executor` (over `BrokerOrder`) -> `budget_snapshot` / `BudgetSnapshot` ->
`plan_orders` (`Candidate` -> `PlannedOrder` -> `Plan`) -> `build_runner_broker` ->
`execute_plan` (`ExecutionResult`, `PlacementFailure`) -> `assess`, with
`load_strategy_state` / `save_strategy_state` / `state_transaction` around it.

Gate 5 (signal generation) lives outside this module, in the candidate generators below.

### 1.5 Gate 5 candidate generators

One CLI per slot, all sharing `scripts/stream_candidates.py`. These are what an executor session
runs to get its buy list; never reimplement the sizing they print.

| Script | Slot |
|---|---|
| `scripts/generate_market_open_candidates.py` | 0935ET |
| `scripts/generate_morning_candidates.py` | 1030ET |
| `scripts/generate_intraday_candidates.py` | 1130 / 1330 / 1430ET |
| `scripts/generate_midday_candidates.py` | 1230ET |
| `scripts/generate_buy_candidates.py` | 1530ET only |
| `scripts/stream_candidates.py` | shared machinery; extend this to add a slot |

### 1.6 Reliability engines behind the runners

Library modules, no CLI, imported by the runners. `docs/reliability_spec.md` carries the honest
implementation-status matrix.

| Module | Role |
|---|---|
| `scripts/broker_reconciliation.py` | Fail-closed same-day reconciliation + effective prior allocation. If it cannot prove completeness, the run must not submit. |
| `scripts/run_state_machine.py` | Run-state lifecycle for one scheduled/recovery window |
| `scripts/run_ledger.py` | Chronological run ledger across the daily streams |
| `scripts/idempotency.py` | Order idempotency keys |
| `scripts/scheduler_lease.py` | File-based leases (the sole-executor guarantee) |
| `scripts/same_day_pointer.py` | Latest-pointer validation and refresh |
| `scripts/same_day_top_up_gate.py` | Broker-sourced enforcement of `same_day_top_up_policy`. **CLI**; its printed `buy_usd` is authoritative. |
| `scripts/reason_codes.py` | Unified structured decision/reason codes |
| `scripts/correlation.py` | Incident correlation IDs across scheduler/runner/broker/recovery |
| `scripts/file_lock.py` | Cross-process advisory lock (`flock` fast path) |
| `scripts/scheduled_artifacts.py` | The scheduled-run artifact contract |
| `scripts/foreground_primary.py` | Foreground-primary surrogate-context detection for the close-of-day validators |
| `dca_alerts.py` | Operator alerting, dead-man heartbeats, `guarded_run`, failed-start markers |

### 1.7 Ops and maintenance runners (all have a CLI)

| Script | What it does |
|---|---|
| `scripts/healthcheck.py` | Deterministic codebase-health gate |
| `scripts/self_heal.py` | Repair pipeline for the self-heal workflow (`.github/workflows/self-heal.yml`) |
| `scripts/compute_schedule.py` | Computes the self-heal schedule from repo telemetry |
| `scripts/workflow_policy.py` | Static policy checks over `.github/workflows/*.yml` |
| `scripts/backup_strategy.py` | Extracts live strategy state from a pasted transcript |
| `scripts/run_ticker_maintenance.py` | Ticker-history maintenance + per-ticker recalculation |
| `scripts/refresh_active_ticker_histories.py` | Restores missing daily-price histories for the universe |
| `scripts/run_tickers_daily_update.py` | Drives the resumable ticker-history updater |
| `scripts/backfill_intraday_archive.py` | Seeds the intraday archive from artifacts already on disk |

---

## 2. Reports

**Almost none of the reporting modules have a `__main__`.** They are libraries called by an
external agent session (or by `run_dca.py --report`). If you are looking for a CLI that is not in
the table below, it does not exist; call the function.

| Producer | Emits | Invocation | Doc |
|---|---|---|---|
| `dca_reporting.py` | Post-trade slippage rows, latest-run summary, coverage report, foreground-primary report | library: `generate_reports`, `generate_scheduled_reports`, `build_latest_run_summary`, `write_foreground_primary_report`; also `run_dca.py --report` | `docs/reporting_workflow.md` |
| `dca_weekly_dashboard.py` | Weekly performance dashboard (perf snapshot, graded-vs-fixed, sector matrix, budget-reserve effectiveness, execution quality, coverage/missed, review notes) | library: `build_weekly_dashboard`, `write_weekly_dashboard` | `docs/reporting_workflow.md` |
| `dca_notion_sync.py` | Compact Notion sync markdown + payload | library: `build_notion_sync_payload`, `write_compact_sync` | `docs/notion_dashboard_template.md` |
| `dca_cost_tracker.py` | Monthly automation usage/cost markdown + payload | library: `generate_cost_tracker` | `docs/cost_tracking.md` |
| `dca_average_cost_snapshot.py` | Weekly average-cost-basis snapshot across every account | library: `build_average_cost_snapshot`, `write_average_cost_snapshot` | `docs/average_cost_snapshot.md` |
| `dca_risk.py` | Rolling cash-burn / kill-switch report | **CLI** (`python dca_risk.py`) | `docs/risk_management.md` |
| `dca_intraday_archive.py` | Durable intraday observation archive per poll | library: `append_observations`, `archive_observations_safely`, `write_run_snapshot` | `docs/intraday_archive.md` |
| `dca_market_graph.py` + `viz/` | The 3D market graph ("The Ocean"), one self-contained HTML file, no network | **CLI**: `python -m scripts.generate_market_graph` | `docs/market_graph.md` |
| `scripts/purchase_history.py` | Consolidated ledger of every confirmed fill, ever | **CLI** | `docs/purchase_history.md` |
| `scripts/drift_detection.py` | Daily signals-vs-orders drift detector (`--slack`) | **CLI** | `docs/drift_detection.md` |
| `scripts/alert_digest.py` | Digest of executor gate alerts stuck as unsent Gmail drafts, by gate/slot/streak | **CLI** | `docs/alert_digest.md` |
| `scripts/close_window_signal.py` | Read-only `what_would_have_fired` close-window summary (multiplier capped at 3x **for reporting only**) | library, consumed by `dca_reporting.py` | inline docstring |
| `scripts/model_usage_log.py` | Model-usage audit log writer | **CLI** | `schemas/model_usage_log.schema.json` |
| `scripts/generate_credit_usage_proxy_report.py` | The one-time 2026-07-27 credit-usage proxy report | **CLI** | `docs/credit_usage_proxy_report.md` |
| `scripts/run_classification.py` | Not a report, but the gate every report uses to exclude manual runs | library | `docs/run_classification.md` |

Non-trading scheduled report tasks (from `docs/schedule_manifest.md`): 5:00 PM `67aa802b`
post-trade slippage + Notion sync; 5:30 PM Friday `041d9d90` weekly dashboard; 8:45 AM monthly
`0656657a` dashboard refresh; 9:55 AM monthly `bec4a6ee` cost review; 5:15 PM month-end
`c9fb1fb8` (EDT) / `17a3b2a9` (EST) volatility risk refresh.

---

## 3. Prediction and signal tools

### 3.1 The one thing to know first

Only two config files can move live money through sizing, and both are written by a guarded,
fail-closed publisher, never by hand:

```
config/per_ticker_sizing.json      <- scripts/generate_per_ticker_sizing.py (stdlib, in-repo)
config/ml_sizing_adjustment.json   <- ml-forecaster/publish.py (sibling subproject)
        |                                 read by dca_config.load_ml_sizing_adjustment
        |                                 -> StrategyConfig.ml_sizing_adjustment
        v                                 -> near_close_dca.graded_buy_multiplier (bounded)
   live buy multiplier
```

`sizing_adjustment_reader.py` is the boundary module that combines multiple suggesters. It has no
in-repo caller yet by design: it exists so that when a second suggester lands, the combination
decision has one home instead of leaking into either suggester. Dependency flows one way,
suggester -> its own config file -> reader -> `graded_buy_multiplier()`, never back.

Everything else in this section is research output. Nothing else here is wired into live sizing.

### 3.2 In-repo (stdlib only)

| Module | Predicts / produces | Wired into live sizing? |
|---|---|---|
| `scripts/generate_per_ticker_sizing.py` | Per-stock DCA calibration from daily OHLCV history, into `config/per_ticker_sizing.json` | **yes**, read by `dca_runner.py` |
| `scripts/daily_per_ticker_calibration.py` | Rebuilds next-session per-ticker triggers after a ticker-update event (wraps the above) | **yes**, same file |
| `near_close_dca.py` signal layer | The graded ladder itself: dip tiers -> multiplier | this *is* the live path |
| `dca_inverse_martingale.py` | Opt-in deferred accumulation: tracks dip-tier amounts, fires once on recovery | opt-in, **off by default** (`docs/inverse_martingale.md`) |
| `dca_whole_share_accumulator.py` | Whole-share accumulation for non-fractional tickers (built for BYDDY) | **yes, 3:30 PM stream only** (`docs/whole_share_accumulation.md`) |
| `dca_cumulative.py` | Cumulative sub-dollar credit ledger | yes |
| `claude/_agent_scratch/run_market_open_signals.py`, `build_raw_data.py`, `snap.py` (moved out of the repo root 2026-09-07) | One-off 9:35 signal reproduction from static data | no, harness/scratch |

### 3.3 Sibling subprojects (own `requirements.txt`, own `tests/`, own CI job)

Each is excluded from the core stdlib-only ruff and coverage gates. Run and lint them separately
(the commands are in `CLAUDE.md`).

| Subproject | Predicts | Output | Reaches live sizing? |
|---|---|---|---|
| `ml-forecaster/` | Pooled ridge regression over no-lookahead daily features -> forward 5-day return on dip days -> bounded per-ticker multiplier adjustment | `config/ml_sizing_adjustment.json`, only through `publish.py`'s fail-closed quality gate | **yes, bounded** (default +-20%, schema cap +-50%) |
| `wondrous-octopus/` | Next-day OHLC per ticker (point estimate + p10/p50/p90) via a causal Transformer per ticker plus a Perceiver-style cross-ticker market encoder, walk-forward with online updates | Run ledgers, `report.json`, `inspect_run.py` views | **no**, research artifacts only |
| `ticker-advisor/` | Scores *candidate* tickers on strategy health, backtest, and sentiment | Ranked report + a **proposed** diff for `config/tickers.json` and friends; edits nothing itself | no, universe proposals for manual review |
| `dca-sizing-signals/` | `connector_signals/` produces a `SizingSuggestion` from connector market data; `market_data_ingest/` is the **only** place in the repo allowed to make network calls | Versioned point-in-time local files; an immutable `SizingSuggestion` | no (a boundary test fails if it imports `near_close_dca`, `ml_forecaster`, or `wondrous_octopus`) |
| `ticker-market-breadth/` | Nothing. Fetches `^TICK` NYSE breadth history | `<history_dir>/_breadth/TICK.csv` | **no, data only**, read by nothing |
| `production-safe-agent-loop/` | Nothing. Turn/token circuit breaker + forced pre-flight spec | raises `CircuitBreakerError` | no, an operator wires it into an agent session |
| `trading-strategies/` | Monday sentiment monitor + backtest research | markdown/CSV research output | no, out of scope |
| `analysis/` | Dated one-off analyses (ladder revision, execution audits, reliability updates) | markdown | no, historical record |

`ml-forecaster/cluster_tickers.py` is the odd one out: it lives in the forecaster but exports a
behavioral ticker grouping for the 3D market graph, and has no path to the guarded publish.

---

## 4. Quick lookup

| I want to... | Do this |
|---|---|
| Dry-run the whole pipeline | `python run_dca.py --demo --report --now <weekday>T15:45 --artifacts-dir <dir>` |
| Find the command for a task | `python scripts/agent_entry_points.py --search <word>` |
| Know which entry point owns a slot | Section 1.2 above |
| Change gate behavior | Decide *which* entry point first (section 1.1). The fix does not propagate. |
| Add a Gate 5 slot | Extend `scripts/stream_candidates.py`, not a new generator from scratch |
| Add a new suggester that influences sizing | Publish your own config file, then teach `sizing_adjustment_reader.py` to combine it. Do not touch `graded_buy_multiplier` directly. |
| Add a top-level module or config file | Update `pyproject.toml` (`py-modules`, `data-files`) or it breaks only in an installed wheel |
| Find the tests for something | `tests/test_<module>.py` for core, `scripts/tests/` for `scripts/`, each subproject's own `tests/` |

## 5. Keeping this honest

This page is hand-maintained and will drift. When you add a runner, a report producer, or a
prediction tool, add the row here in the same commit. `scripts/agent_entry_points.py` is the
test-enforced index; if a command belongs in both, put it in both, and let the script's test be
the thing that catches a stale path.
