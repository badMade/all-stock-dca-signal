# Graded DCA Strategy Backup

Version-controlled, human-readable backup of the graded DCA strategy: its
sizing ladder, ticker universe, budgets, safeguards, scheduled tasks, reporting
workflow, and Notion template. **No credentials or secrets are stored here.**

## Docs

| File | Contents |
|---|---|
| [strategy.md](strategy.md) | Strategy overview, ticker universe, latest sizing ladder, budgets |
| [safeguards.md](safeguards.md) | Broker/account, trading constraints, budget caps, test window |
| [schedule_manifest.md](schedule_manifest.md) | All scheduled tasks (only `ce16ba6b` trades) |
| [reporting_workflow.md](reporting_workflow.md) | Daily/periodic artifact + Notion flow |
| [notion_dashboard_template.md](notion_dashboard_template.md) | Notion page template + conceptual API sync |
| [cost_tracking.md](cost_tracking.md) | Cost-control policy and usage proxies |
| [restore_runbook.md](restore_runbook.md) | Restore procedure + audit checklist |

## Machine-readable backup

- [`../config/`](../config) — tickers, sizing ladder, budgets, scheduled tasks, notifications, reporting.
- [`../schemas/`](../schemas) — JSON schemas for config, run-state, slippage, and cost-tracker artifacts.
- [`../scripts/`](../scripts) — `backup_strategy.py` to re-extract config from a transcript, plus tests.

## Key facts (quick reference)

- **Active universe:** 45 tickers; `BYDDY` excluded; `IRDMI→IRDM`, `GLWR→GLW`.
- **Latest ladder:** 0.1%→8%+, base $1, max **5x** (no upper cutoff). Older
  0.1%–12% ladders are **superseded**.
- **Budgets:** $1 base · $60 daily base · $120 hard daily · $600 weekly · $420 reserve.
- **Only trading task:** `ce16ba6b`, **3:30 PM ET** weekdays, Robinhood account
  `562057042` only. The **3:45 PM ET** task is analytics (slippage + Notion sync).
- **Test window:** through **2026-08-28** inclusive.
