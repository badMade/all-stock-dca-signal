# Graded DCA Strategy Backup

Version-controlled, human-readable backup of the graded DCA strategy: its
sizing ladder, ticker universe, budgets, safeguards, scheduled tasks, reporting
workflow, and Notion template. **No credentials or secrets are stored here.**

## Docs

| File | Contents |
|---|---|
| [reliability_spec.md](reliability_spec.md) | Formal run-state, reconciliation, and artifact-contract spec + honest implementation-status matrix |
| [strategy.md](strategy.md) | Strategy overview, ticker universe, latest sizing ladder, budgets |
| [live_wiring.md](live_wiring.md) | Live providers/broker adapters, orchestrator/CLI, and the two placement safety gates |
| [safeguards.md](safeguards.md) | Broker/account, trading constraints, budget caps, test window |
| [schedule_manifest.md](schedule_manifest.md) | All 11 scheduled tasks (only `10ce362c`, which supersedes `ce16ba6b`, trades) |
| [run_classification.md](run_classification.md) | Scheduled-vs-manual run contract (paths, required fields, exclusion rules) |
| [drift_detection.md](drift_detection.md) | Post-artifact signals-vs-submitted-orders drift detector + Slack alerting |
| [reporting_workflow.md](reporting_workflow.md) | Daily/periodic artifact + Notion flow |
| [notion_dashboard_template.md](notion_dashboard_template.md) | Notion page template + conceptual API sync |
| [cost_tracking.md](cost_tracking.md) | Cost-control policy and usage proxies |
| [restore_runbook.md](restore_runbook.md) | Restore procedure + audit checklist |
| [backtest_curve.md](backtest_curve.md) | Analytics-only weekly-rollover intraday cap curve backtest (35 / 65 / 100 candidate) |

## Machine-readable backup

- [`../config/`](../config) — tickers, sizing ladder, budgets, scheduled tasks, notifications, reporting.
- [`../schemas/`](../schemas) — JSON schemas for config, run-state, slippage, cost-tracker, and model-usage-log artifacts.
- [`../scripts/`](../scripts) — `backup_strategy.py` to re-extract config from a transcript, `run_classification.py` to enforce the scheduled-vs-manual contract, plus tests.
- **Reliability engines** (see [reliability_spec.md](reliability_spec.md)): `broker_reconciliation.py` (fail-closed same-day reconciliation + effective prior), `reason_codes.py` (unified decision codes), `run_state_machine.py`, `run_ledger.py`, `correlation.py`, `scheduler_lease.py`, `idempotency.py`. Implemented and unit-tested; wiring into the live decision path is a tracked follow-up.

## Key facts (quick reference)

- **Active universe:** 44 tickers (includes `GFUZ`, `TXN`); `BYDDY`, `VCX`, `TRLV`, `RVI` excluded; `IRDMI→IRDM`, `GLWR→GLW`; `GFUZ` uses Robinhood primary data (finance `NOT_FOUND`).
- **Latest ladder:** 0.1%→8%+, base $2, max **5x** (no upper cutoff). Older
  0.1%–12% ladders are **superseded**.
- **Budgets:** $2 base · tiered caps selected per run by planned spend and 3x+
  signal count — Normal $180/$900 · Elevated $240/$1,200 · High-vol $300/$1,500 ·
  Stress $360/$1,800 (daily/weekly); weekly rollover reserve capped at $900.
- **Only trading task:** `10ce362c` ("Graded DCA automatic live test - $2
  allocation"; the repaired task that supersedes the deleted `ce16ba6b`, with
  artifact root kept under `.../ce16ba6b` for continuity), **3:20 PM ET**
  weekdays, Robinhood account `562057042` only;
  Alpaca read-only. The **3:35 PM ET** task is analytics (slippage + Notion sync).
- **Run classification:** scheduled runs write under
  `scheduled_runs/YYYYMMDD_1520ET/` and update `latest_scheduled_run.json`;
  manual runs write under `manual_runs/<timestamp>/` and never do. Reporting
  excludes manual runs/order IDs from scheduled-live metrics. See
  [run_classification.md](run_classification.md).
- **Afternoon completion:** all afternoon trading/artifact-writing tasks finish
  by **3:40 PM ET**, when the read-only watchdog starts its final verification;
  the watchdog's own check and the Friday weekly Notion refresh at **5:00 PM ET**
  are the only exceptions.
- **Test window:** through **2026-10-30** inclusive per `config/trading_window.json`
  (extended 2026-07-26 from the original 2026-08-28 end date; stops the Friday
  before the 2026-11-01 DST change since the live tasks' cron schedules are
  fixed-UTC/EDT-only — see the window file's `description` for why. This is the
  in-repo code gate — verify the external scheduler's own
  `programmatic_trigger_check.sh` trigger window separately, since it lives
  outside this repo).
