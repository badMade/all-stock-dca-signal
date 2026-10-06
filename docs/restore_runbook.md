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

**Also not in this repo, but must survive a restore anyway:** the whole-share
accumulation ledger (`stream_root("whole_share_accumulation")/<TICKER>/ledger.json`,
gitignored under `cron_tracking/` like the rest of run output — see
[`docs/whole_share_accumulation.md`](whole_share_accumulation.md)). It carries the
durable, cross-day accumulated-contribution balance for **every** whole-share
ticker — read `config/tickers.json`'s `whole_share` list rather than trusting a
name written here (`BYDDY` and `RVII` as of 2026-08-24), so a later addition is
not silently left behind. Restoring the repo from this backup does **not** restore that
balance — copy the ledger tree itself from the live `cron_tracking` root (or the
box being replaced) before resuming scheduled runs, or the accumulator silently
restarts from $0 and undercounts what has already been contributed toward the next
whole share.

## Restore procedure

1. Clone the repo and read [`docs/strategy.md`](strategy.md), then
   [`docs/safeguards.md`](safeguards.md).
2. Confirm the config files match the docs:
   ```bash
   python -c "import json,sys; [json.load(open(f)) for f in ['config/tickers.json','config/sizing_ladder.json','config/budgets.json','config/scheduled_tasks.json','config/notifications.json','config/reporting.json','config/run_classification.json']]; print('config JSON valid')"
   ```
3. Recreate the scheduled tasks from
   [`config/scheduled_tasks.json`](../config/scheduled_tasks.json), preserving
   each cron (UTC) and the trading/analytics/monitoring/reminder type.
   - **Only the live task `10ce362c` (which supersedes the deleted `ce16ba6b`) trades.** All others must remain read-only/analytics.
   - Re-attach the trigger gate `programmatic_trigger_check.sh` to `10ce362c`.
   - Wire the **run-classification contract** ([run_classification.md](run_classification.md),
     [`config/run_classification.json`](../config/run_classification.json)):
     `10ce362c` writes scheduled vs manual artifacts to the correct paths (the
     artifact root stays under `.../ce16ba6b` for continuity) and
     updates `latest_scheduled_run.json` only for scheduled runs; the slippage,
     watchdog, weekly, and monthly tasks exclude manual runs/order IDs from
     scheduled-live metrics. `scripts/run_classification.py` is the reference
     implementation.
4. Re-provision secrets **outside** the repo:
   - Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>` execution credentials.
   - `NOTION_TOKEN` for the dashboard sync (see
     [`notion_dashboard_template.md`](notion_dashboard_template.md)).
   - Per-stream heartbeat URLs (`DCA_HEARTBEAT_URL_MORNING` / `_MIDDAY` /
     `_AFTERNOON`, healthchecks.io-compatible — one check per stream, alerting
     on missed pings) and `DCA_ALERT_SLACK_WEBHOOK` (failure alerts; falls back
     to `DCA_DRIFT_SLACK_WEBHOOK` if unset) in the scheduler environment. These
     are read by `dca_alerts.guarded_run`; until set, the alerting paths are
     silent no-ops by design (see `PRODUCTION_READINESS.md` Phase 1).
5. Verify safeguards are enforced: $2 base with tiered caps (Normal $180/$900,
   Elevated $240/$1,200, High-vol $300/$1,500, Stress $360/$1,800 daily/weekly;
   weekly rollover reserve capped at $900), one order per ticker per day, market +
   `gfd` + `review_equity_order`, no sells/options/crypto/margin/cancellations,
   regular hours only.
6. Confirm the test window (through **2026-10-30**, per
   `config/trading_window.json`) is still active before enabling live trading,
   and separately confirm the scheduler-side `programmatic_trigger_check.sh`
   trigger gate (outside this repo) matches — extending the config here does
   not extend that external script. The window deliberately ends before the
   2026-11-01 DST change (fixed-UTC live-task cron); do not extend it past
   2026-10-30 until the external scheduler's cron is confirmed DST-aware.

## Refreshing config from a new thread

When the strategy changes, re-extract from a transcript rather than hand-editing:

```bash
python scripts/backup_strategy.py --transcript <thread.md> --write --out-dir config
```

Then diff and review before committing. See [`scripts/README.md`](../scripts/README.md).

## Audit checklist

- [ ] Active universe = 57 symbols (includes `GFUZ`, `TXN`, `RKLB`, the 2026-08-02 additions `GE`, `VRT`, `CAT`, `QQQ`, `QCOM`, `ABBV`, `JPM`, the 2026-08-04 addition `VT`, and the 2026-08-16 additions `QBUT`, `RVI`, `VCX`, `TRLV`); `BYDDY` and `RVII` are whole-share tickers, not excluded (fractional-tradability; `RVII` moved out of the active list 2026-08-24 — see docs/whole_share_accumulation.md); `IRDMI→IRDM`, `GLWR→GLW` applied; `GFUZ` included (Robinhood primary data, finance `NOT_FOUND`).
- [ ] Ladder is the latest (0.1%–16%+ log-scaled, max 8x, no upper cutoff). No 12x-era tiers.
- [ ] Base amount $2; tiered daily/weekly caps and $900 weekly rollover reserve cap intact.
- [ ] Only the live trading tasks `2ba19526`, `42173154`, `7be82b97`, `a6239dae`, and `10ce362c` (supersedes deleted `ce16ba6b`) can trade; they use only Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>`; Alpaca read-only.
- [ ] Final live task at 3:30 PM ET; slippage/Notion sync at 5:00 PM ET daily (retimed 2026-08-02 from 3:35 PM ET).
- [ ] Run-classification contract wired: every run_state has a `run_classification` block; scheduled runs update `latest_scheduled_run.json`, manual runs don't; reporting excludes manual runs/order IDs (`python scripts/tests/test_run_classification.py`).
- [ ] The 14 canonical active tasks are present and match `config/scheduled_tasks.json` -> `canonical_schedule_reconciliation_2026_07_27.canonical_schedule_et`: `2ba19526`, `bb4f6ef1`, `42173154`, `7be82b97`, `a6239dae`, `10ce362c`, `67aa802b`, `041d9d90`, `0656657a`, `bec4a6ee`, `c9fb1fb8`, `17a3b2a9`. Plus the one-time `d2b6c23e` (fires 2026-10-16 11:45 AM ET; recorded in `one_time_additions_after_canonical_verification`, not the canonical table). `e3e6f09e` (3:05 PM monitor-only check) and `a46bf007` (3:40 PM live-run watchdog) were REMOVED from the schedule entirely on 2026-08-02 -- they must NOT be restored; see `removed_on_2026_08_02` in `config/scheduled_tasks.json`.
- [ ] The retired entries `b7961407`, `cd45e95b`, `morning-dca-0945`, `d72aa525`, `d1f7a2c0`, and `f7411889` (kept as historical stubs in `retired_not_in_canonical_schedule`, each with a `former_et`) are **absent from every active schedule table** and carry no `cron_utc`. `f7411889` is the one-time Aug 28 continuation reminder, retired 2026-09-13 and superseded by `d2b6c23e`; see `retired_on_2026_09_13`. `e3e6f09e` and `a46bf007` are absent too, but differently: their task definitions were deleted entirely on 2026-08-02, not kept as retired stubs -- do not expect to find them in `retired_not_in_canonical_schedule`.
- [ ] Afternoon trading completes by 3:30 PM ET (`10ce362c`'s final execution -- the 3:40 PM watchdog that used to anchor a separate completion boundary was removed 2026-08-02); the 5:00 PM ET slippage/Notion sync (`67aa802b`), Fri 5:30 PM ET weekly Notion refresh (`041d9d90`, retimed 2026-08-02 from 5:00 PM ET), and the month-end volatility risk refresh at 5:15 PM ET (`c9fb1fb8`/`17a3b2a9`, retimed 2026-08-02 from its own prior 5:00 PM ET to clear the collision with `67aa802b`'s new 5:00 PM slot) are the only approved post-3:30 PM DCA-related tasks.
- [ ] Signal rules recorded: below prior close + 60-min trend down + multiplier > 0.
- [ ] No secrets committed (`git grep` for tokens/keys before pushing).
- [ ] Whole-share ledger tree (`stream_root("whole_share_accumulation")/<TICKER>/ledger.json`)
      copied over from the box/root being replaced -- not just re-derived from this repo -- so no
      whole-share ticker's accumulated cross-day balance is silently reset to $0 on restore. Check
      one subdirectory per entry in `config/tickers.json`'s `whole_share` list, not just `BYDDY`.
- [ ] Notifications: no *routine* Slack (in-app + email-where-specified only for normal runs); Slack is permitted, env-gated, and off-by-default for the drift (`DCA_DRIFT_SLACK_WEBHOOK`) and failure-alert/heartbeat (`DCA_ALERT_SLACK_WEBHOOK`) paths only — never for routine success notifications.
