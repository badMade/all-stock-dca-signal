# Codebase roadmap: how the system works, end to end

Generated 2026-09-30 from a static import graph of every Python file (123 core, `scripts/`,
`all_stock_dca/`, `guardrails/`, `selfheal/`, `viz/` modules), cross-checked against the live
prompt mirror, workflows, `pyproject.toml`, and the docs. It is meant to be read next to your own
mental model: each section ends with **Check against your expectation** bullets, the places where
the code differs from what `CLAUDE.md` or the older docs say.

Companion pages: [repo_map.md](repo_map.md) (what exists), [live_wiring.md](live_wiring.md)
(injection), [dead_code_report.md](dead_code_report.md) (cleanup candidates).

## 1. The one-paragraph version

An external agent session (cron-scheduled, holding Robinhood MCP access) wakes at a slot time and
reads its prompt from `claude/dca-executor-prompts/live-mirror/`. The prompt tells it to run a
Gate 5 candidate script (`scripts/generate_*_candidates.py`), the gate sequence in `dca_runner.py`
or an executor class, place orders through `dca_live_adapters.py` callables that map 1:1 to MCP
tools, write run artifacts under `cron_tracking/`, and finish with `scripts/finalize_run_evidence.py`.
Later jobs (`verify_schedule`, `drift_detection`, `alert_digest`, reports, the local console) only
read those artifacts. This repo holds no credentials and makes no network calls.

## 2. Request flow for one live slot

```
live-mirror prompt (.txt)            claude/dca-executor-prompts/live-mirror/
  |
  |-- Gate 5  scripts/generate_<slot>_candidates.py -> scripts/stream_candidates.py
  |            sizing from near_close_dca (graded ladder) + config/*.json
  |            + config/per_ticker_sizing.json + config/ml_sizing_adjustment.json (bounded)
  |
  |-- top-up  scripts/same_day_top_up_gate.py      prints the authoritative buy_usd
  |           scripts/assemble_order_pages.py      proves order pagination complete
  |           scripts/broker_reconciliation.py     fail closed if not provably complete
  |
  |-- gates   dca_runner.py  (0 market day, 1 time, 3 window, 4 sole executor, 6 budget, 7 place)
  |           OR Morning/MarketOpen/Midday/IntradayDCAExecutor.run()
  |           OR run_dca.run_slot() (1530ET path over near_close_dca primitives)
  |            \-> dca_live_adapters.RobinhoodBroker / RobinhoodDataProvider (injected callables)
  |
  |-- state   near_close_dca StrategyState, dca_cumulative, dca_whole_share_accumulator,
  |           dca_inverse_martingale (opt-in), scripts/file_lock, scripts/idempotency
  |
  `-- Gate 9  scripts/finalize_run_evidence.py -> scripts/same_day_pointer.py, scheduled_artifacts.py
                \-> cron_tracking/<stream>/<run>/ + latest_<stream>_run.json
```

After the slot: `scripts/verify_schedule.py` (did each slot leave evidence), `scripts/drift_detection.py`
(signals vs orders), `scripts/alert_digest.py`, `dca_reporting.py`, `dca_weekly_dashboard.py`,
`dca_risk.py`, and the read-only console (`from.py` -> `dca_console*.py`).

## 3. Layers and who imports whom

Import counts are in-repo importers, excluding tests.

| Layer | Modules | Notes from the graph |
|---|---|---|
| Strategy core | `near_close_dca` (42.5k lines, 27 importers) | The hub. Everything money-related leans on it |
| Executors | `morning_dca` (10.7k, 9 importers) and three thin subclasses | `conftest`, console, `run_dca`, scripts import it |
| Gate runner | `dca_runner` (9.4k, 10 importers) | Imported by `near_close_dca` (one deferred import), 4 scripts, and 5 console modules |
| Broker/data | `dca_live_adapters` (8.7k, 6 importers) | |
| Orchestration | `run_dca` (5.0k) | Imported only by `dca_console_schedule`; its `__main__` is the dry-run CLI |
| Money/risk | `dca_risk`, `dca_cumulative`, `dca_inverse_martingale`, `dca_whole_share_accumulator` | Last two are imported only by `near_close_dca` |
| Infrastructure | `dca_config` (19 importers), `dca_fileio` (32), `dca_paths`, `dca_ticker_paths`, `dca_common`, `scripts/file_lock` | Safe to change only with the full suite |
| Reporting | `dca_reporting`, `dca_weekly_dashboard`, `dca_cost_tracker`, `dca_notion_sync`, `dca_average_cost_snapshot` | Library only, see section 5 |
| Console | `dca_console` + 14 `dca_console_*` modules | Each page reader has exactly one importer, the route table |
| Reliability engines | `scripts/{broker_reconciliation,idempotency,reason_codes,correlation,same_day_pointer,file_lock,run_state_machine,run_ledger,scheduler_lease}` | See section 4 |
| Sibling projects | `wondrous-octopus`, `ml-forecaster`, `ticker-advisor`, `dca-sizing-signals`, `ticker-market-breadth`, `llm-providers`, `yfinance-prefetch`, `production-safe-agent-loop`, `trading-strategies`, `analysis` | Not scanned here; own deps and tests |

## 4. Reliability engines: wired or not

| Engine | In-repo importers (non-test) | Status |
|---|---|---|
| `broker_reconciliation` | `near_close_dca`, `same_day_top_up_gate`, `assemble_order_pages`, `run_classification` | Wired |
| `idempotency` | `near_close_dca`, `morning_dca`, `dca_alerts` | Wired |
| `same_day_pointer` | `near_close_dca`, `morning_dca`, `finalize_run_evidence`, others | Wired |
| `reason_codes` | `broker_reconciliation`, `same_day_top_up_gate` | Wired |
| `correlation` | `run_dca` | Wired into the 1530ET path only |
| `run_state_machine` | `dca_console_alerts` only | Engine only (the doc says so) |
| `run_ledger` | none | Engine only, tests only |
| `scheduler_lease` | none | Engine only, tests only |

Check against your expectation: `CLAUDE.md` says the sole-executor guarantee is file leases
(`repo_map.md` row for `scheduler_lease`). The code shows Gate 4 enforces sole-executor from broker
order history, and `scheduler_lease` has no caller. Confirm that is what you intended.

## 5. Entry points, corrected against the graph

`CLAUDE.md` says `dca_runner.py`, `dca_cost_tracker.py`, `dca_weekly_dashboard.py` have "zero
in-repo callers by design". The graph disagrees in a narrow way:

| Module | Real in-repo callers |
|---|---|
| `dca_runner` | `near_close_dca` (`market_day_status`), `scripts/stream_candidates`, `generate_buy_candidates`, `verify_schedule`, `finalize_run_evidence`, console readers. It has no caller that *runs a slot*; the session does |
| `dca_cost_tracker` | `dca_console_data`, `dca_console_reports` |
| `dca_weekly_dashboard` | `dca_console_reports` only |
| `MorningDCAExecutor` family | No caller constructs and runs them. Only `run_dca` and scripts import helpers from `morning_dca` |

So the "driven from outside" claim holds for running slots, but these modules are not dead and
not untouched by in-repo code. Deleting any of them breaks the console as well as live automation.

## 6. Slot ownership (verified against `run_dca.SLOT_MAP` / `DISABLED_SLOTS`)

`run_dca.run_slot()` supports 1030ET, 1230ET and 1530ET. 0935ET, 1130ET, 1330ET and 1430ET raise
`UnsupportedSlotError` there and run only through executor classes or `dca_runner`. This matches
`repo_map.md` section 1.2.

## 7. Config and data contracts

`config/*.json` validated by `schemas/*.json`, gate in `tests/test_config_invariants.py`.
Paths come from `dca_paths.stream_root()` / `cron_tracking_root()`; nothing hard-coded. The
account number exists only as `${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}`.

## 8. Things that look wrong and need your call

See [dead_code_report.md](dead_code_report.md) for evidence. Short list:

1. Root copies of `snap.py`, `build_raw_data.py`, `run_market_open_signals.py`, and two JSON files
   duplicate `claude/_agent_scratch/`. `repo_map.md` says they were moved out of the root on
   2026-09-07.
2. `all_stock_dca/` is a package, but `pyproject.toml` lists `packages = ["scripts", "selfheal"]`,
   so it would be missing from an installed wheel.
3. Four scripts have no reference anywhere in the repo: `all_stock_dca/merge_predictions.py`,
   `all_stock_dca/train_forecaster.py`, `scripts/evaluate_deferral_params.py`,
   `scripts/shard_heartbeat.py`.
4. Ten helpers (nine private) are defined and never called.

## 9. How to regenerate this

The graph came from an AST scan of imports (absolute, relative, `from x import y`) plus a search
of prompts, workflows, `pyproject.toml`, config and console assets for each module name. It cannot
see string dispatch, so treat "no caller" as a lead, not a verdict.
