# Recovery Changelog — 2026-07-23

Same-day activity for the DCA streams on 2026-07-23. Entries are grouped into
**scheduled/monitor** results (valid scheduled evidence where noted) and
**manual / recovery** results (`valid_scheduled_evidence=false`,
`same_day_top_up_context_only=true`, never scheduled evidence).

Per the AGENTS.md secrets policy, operational identifiers — the live account
number, broker order ids, and fill timestamps/prices — are not duplicated into
this version-controlled report; they live only in the local gitignored
`cron_tracking/` artifacts referenced by each entry's artifact root.

## Scheduled and monitor results

### 12:30 midday live (`a6239dae`) — TXN filled

| Field | Value |
| --- | --- |
| Symbol | TXN |
| Amount | $1.00 |
| Average fill price | recorded in the local gitignored artifact |
| Status | filled |
| Order ID | recorded in the local gitignored artifact |
| Created at (UTC) | recorded in the local gitignored artifact |
| Broker / account | Robinhood / the configured live account (`config/scheduled_tasks.json` → `execution_authorization_policy`) |
| Run ID | `midday_live_20260723_1230ET` |
| Cron id | `a6239dae` |
| Artifact root | `cron_tracking/midday_dca/20260723_1230ET` |
| Classification | `run_origin=scheduled`, `stream=midday`, `trading_day=2026-07-23`, `valid_scheduled_evidence=true` |

Artifact repair: the local artifact initially stored `run_classification` as a
**string** with a **null** `trading_day`. It was repaired in place to a non-null
`run_classification` **object** with `trading_day=2026-07-23` (ISO) and
`valid_scheduled_evidence=true` (`artifact_repair_applied_at_utc` recorded in
`run_summary.json`).

### 3:05 monitor (`e3e6f09e`) — failed start, backfilled, no trading

The monitor-only task failed with
`error_class=internal_start_error_before_artifacts` before writing output.
Compact monitor-failure artifacts were backfilled under
`cron_tracking/dca_monitor_1505/20260723_1505ET/`: `monitor_state.json`
(`order_submit_status=failed_internal_start_error`,
`valid_scheduled_evidence=false`), `run_summary.json`, and header-only
`signals.csv` / `submitted_orders.csv`. Monitor-only — no orders were placed.

### 3:20 final live (`10ce362c`) — corrected, no orders needed

The final task first escalated because the duplicate guard saw same-day
Robinhood orders while `order_plan.csv` carried artifact `prior_usd=0` and
ignored the populated `prior_usd_rh`. After applying the effective-prior-
allocation patch (fix #6), the corrected plan required no orders
(`run_id=scheduled_20260723_1520ET`):
`order_submit_status=no_orders_after_corrected_same_day_topup`,
`orders_submitted_count=0`, and `corrected_topup_summary.json` shows
`corrected_submit_count=0`, `corrected_total_usd=0`, `blocked_count=28`, all
`already_met_or_overfilled_by_same_day_orders`. Repaired artifacts written under
`cron_tracking/ce16ba6b/scheduled_runs/20260723_1520ET/`: `run_state.json`,
`run_summary.json`, header-only `submitted_orders.csv`, `corrected_topup_plan.csv`,
`corrected_topup_summary.json`, and `latest_scheduled_run.json`. Valid scheduled
evidence that the task fired; not evidence of a fill.

### 3:35 slippage / 3:40 watchdog — afternoon valid, 0 scheduled fills

The 3:35 slippage check and the 3:40 watchdog confirmed the afternoon scheduled
run was valid with **0 scheduled fills**. The 12:30 TXN fill is midday scheduled
activity / context, not afternoon scheduled evidence.

The 3:40 watchdog also flagged a **Robinhood same-day snapshot gap**: raw
snapshots returned 0 orders for the default individual account and missed the
12:30 TXN fill even though `submitted_orders.csv` recorded it. The 10:30, 12:30,
3:20, 3:35, and 3:40 task text was patched so every same-day Robinhood snapshot
queries the configured live `account_number` (`config/scheduled_tasks.json` →
`execution_authorization_policy`), pages through all same-day equity orders
without `placed_agent` filtering first, persists the raw paginated responses, and
runs one bounded account-specific `order_id` fallback for `submitted_orders.csv`
order IDs when the snapshot returns 0 or omits an artifact order. See fix #7 in
[live_task_reliability_update_2026-07-23.md](./live_task_reliability_update_2026-07-23.md).

## Manual / recovery results

All entries below are **manual / recovery context only**
(`valid_scheduled_evidence=false`, `same_day_top_up_context_only=true`) and must
not count as scheduled evidence.

### Completed recovery fill

| Field | Value |
| --- | --- |
| Symbol | BTX |
| Amount | $1.00 |
| Order type | market (dollar notional) |
| Time in force | gfd |
| Average fill price | recorded in the local gitignored artifact |
| Status | filled |
| Order ID | recorded in the local gitignored artifact |
| Broker / account | Robinhood / the configured live account (`config/scheduled_tasks.json` → `execution_authorization_policy`) |
| Run ID | `manual_recovery_rerun_20260723_1208ET` |
| Artifact root | `cron_tracking/morning_dca/manual_recovery/20260723_1208ET_rerun` |
| Classification | `run_origin=manual_recovery`, `manual_run=true`, `valid_scheduled_evidence=false`, `same_day_top_up_context_only=true` |

Artifact contract for this run was reported `complete`: `signals.csv`,
`planned_orders.csv`, `same_day_orders_context.csv`, `submitted_orders.csv`,
`run_state.json`, and `run_summary.json` were all written, and
`run_classification` is non-null in both `run_state.json` and `run_summary.json`.

### Duplicate recovery batches detected

Two earlier recovery batches were detected and flagged so they are not
double-counted or re-submitted. Both are manual / top-up context only:

- `cron_tracking/morning_dca/manual_recovery/20260723_1155ET` — background
  recovery batch that wrote planned orders and later submitted them.
- `cron_tracking/morning_dca/manual_recovery/20260723_1201ET_direct_foreground` —
  foreground direct recovery batch that started after initially observing a
  missing `submitted_orders.csv`.

The 1208ET rerun recorded the warning
`duplicate_recovery_batches_detected_from_1155_and_1201_artifacts` and applied
the duplicate-submission guard (dedupe by symbol, amount, and order ID against
same-day `submitted_orders.csv` and Robinhood order history) so no duplicate BTX
order was placed. See fix #3 in
[live_task_reliability_update_2026-07-23.md](./live_task_reliability_update_2026-07-23.md).
