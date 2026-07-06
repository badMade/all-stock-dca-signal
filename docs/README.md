# Graded DCA Strategy Backup

Version-controlled, human-readable backup of the graded DCA strategy: its
sizing ladder, ticker universe, budgets, safeguards, scheduled tasks, reporting
workflow, and Notion template. **No credentials or secrets are stored here.**

## Docs

| File | Contents |
|---|---|
| [strategy.md](strategy.md) | Strategy overview, ticker universe, latest sizing ladder, budgets |
| [safeguards.md](safeguards.md) | Broker/account, trading constraints, budget caps, test window |
| [schedule_manifest.md](schedule_manifest.md) | All 11 scheduled tasks (only `10ce362c`, which supersedes `ce16ba6b`, trades) |
| [run_classification.md](run_classification.md) | Scheduled-vs-manual run contract (paths, required fields, exclusion rules) |
| [reporting_workflow.md](reporting_workflow.md) | Daily/periodic artifact + Notion flow |
| [notion_dashboard_template.md](notion_dashboard_template.md) | Notion page template + conceptual API sync |
| [cost_tracking.md](cost_tracking.md) | Cost-control policy and usage proxies |
| [restore_runbook.md](restore_runbook.md) | Restore procedure + audit checklist |

## Machine-readable backup

- [`../config/`](../config) — tickers, sizing ladder, budgets, scheduled tasks, notifications, reporting.
- [`../schemas/`](../schemas) — JSON schemas for config, run-state, slippage, and cost-tracker artifacts.
- [`../scripts/`](../scripts) — `backup_strategy.py` to re-extract config from a transcript, `run_classification.py` to enforce the scheduled-vs-manual contract, plus tests.

## Key facts (quick reference)

- **Active universe:** 45 tickers; `BYDDY` excluded; `IRDMI→IRDM`, `GLWR→GLW`.
- **Latest ladder:** 0.1%→8%+, base $3, max **5x** (no upper cutoff). Older
  0.1%–12% ladders are **superseded**.
- **Budgets:** $3 base · tiered caps selected per run by planned spend and 3x+
  signal count — Normal $180/$900 · Elevated $240/$1,200 · High-vol $300/$1,500 ·
  Stress $360/$1,800 (daily/weekly); weekly rollover reserve capped at $900.
- **Only trading task:** `10ce362c` ("Graded DCA automatic live test - $3
  allocation"; the repaired task that supersedes the deleted `ce16ba6b`, with
  artifact root kept under `.../ce16ba6b` for continuity), **3:20 PM ET**
  weekdays, Robinhood account `562057042` only;
  Alpaca read-only. The **3:35 PM ET** task is analytics (slippage + Notion sync).
- **Run classification:** scheduled runs write under
  `scheduled_runs/YYYYMMDD_1520ET/` and update `latest_scheduled_run.json`;
  manual runs write under `manual_runs/<timestamp>/` and never do. Reporting
  excludes manual runs/order IDs from scheduled-live metrics. See
  [run_classification.md](run_classification.md).
- **Afternoon completion:** all afternoon tasks finish by **3:40 PM ET**, except
  the Friday weekly Notion refresh at **5:00 PM ET**.
- **Test window:** through **2026-08-28** inclusive.
