# Restore & Audit Runbook

How to reconstruct the graded DCA setup from this backup, and how to audit it.

## What's backed up here

| Area | Location |
|---|---|
| Strategy prose | [`docs/`](.) |
| Machine-readable config | [`config/`](../config) |
| Artifact/config schemas | [`schemas/`](../schemas) |
| Extraction utility + tests | [`scripts/`](../scripts) |

**Not backed up (by design):** broker credentials, Notion/Drive tokens, private
share links, and any live connector secrets. These must be re-provisioned from a
secret manager.

## Restore procedure

1. Clone the repo and read [`docs/strategy.md`](strategy.md), then
   [`docs/safeguards.md`](safeguards.md).
2. Confirm the config files match the docs:
   ```bash
   python -c "import json,sys; [json.load(open(f)) for f in ['config/tickers.json','config/sizing_ladder.json','config/budgets.json','config/scheduled_tasks.json','config/notifications.json','config/reporting.json']]; print('config JSON valid')"
   ```
3. Recreate the scheduled tasks from
   [`config/scheduled_tasks.json`](../config/scheduled_tasks.json), preserving
   each cron (UTC) and the trading/analytics/monitoring/reminder type.
   - **Only `ce16ba6b` trades.** All others must remain read-only/analytics.
   - Re-attach the trigger gate `programmatic_trigger_check.sh` to `ce16ba6b`.
4. Re-provision secrets **outside** the repo:
   - Robinhood account `562057042` execution credentials.
   - `NOTION_TOKEN` for the dashboard sync (see
     [`notion_dashboard_template.md`](notion_dashboard_template.md)).
5. Verify safeguards are enforced: $3 base with tiered caps (Normal $180/$900,
   Elevated $240/$1,200, High-vol $300/$1,500, Stress $360/$1,800 daily/weekly;
   weekly rollover reserve capped at $900), one order per ticker per day, market +
   `gfd` + `review_equity_order`, no sells/options/crypto/margin/cancellations,
   regular hours only.
6. Confirm the test window (through **2026-08-28**) is still active before
   enabling live trading.

## Refreshing config from a new thread

When the strategy changes, re-extract from a transcript rather than hand-editing:

```bash
python scripts/backup_strategy.py --transcript <thread.md> --write --out-dir config
```

Then diff and review before committing. See [`scripts/README.md`](../scripts/README.md).

## Audit checklist

- [ ] Active universe = 45 symbols; `BYDDY` excluded; `IRDMI→IRDM`, `GLWR→GLW` applied.
- [ ] Ladder is the latest (0.1%–8%+, max 5x, no upper cutoff). No 12x-era tiers.
- [ ] Base amount $3; tiered daily/weekly caps and $900 weekly rollover reserve cap intact.
- [ ] Only `ce16ba6b` can trade; it uses only account `562057042`; Alpaca read-only.
- [ ] Live task at 3:20 PM ET; slippage/Notion sync at 3:35 PM ET (not merged).
- [ ] All 11 tasks present, including `b7961407` (9:00 AM buying-power) and `d72aa525` (3:25 PM briefing).
- [ ] Afternoon tasks complete by 3:40 PM ET, except Fri 5:00 PM Notion refresh.
- [ ] Signal rules recorded: below prior close + 60-min trend down + multiplier > 0.
- [ ] No secrets committed (`git grep` for tokens/keys before pushing).
- [ ] Notifications: no Slack; in-app + email-where-specified only.
