# Cost Tracking

How automation cost is monitored and controlled. Machine-readable artifact
shape: [`schemas/cost_tracker.schema.json`](../schemas/cost_tracker.schema.json).

## Policy

- **Daily tasks**: compact / local-first. Avoid large narratives on quiet days
  (e.g. the volatility scan alerts only on >5% outliers).
- **Heavy analysis**: reserved for **Fridays and monthly** refreshes.
- **Manual full runs**: avoid unless automation fails.

## Cost-saving operating mode (requested Thu Jul 16 2026)

Machine-readable source of truth:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json) → `operating_mode`
(mode `cost_saving_daily_compact`). Weekday tasks stay **compact**; all heavy
analysis is deferred to the **Friday 5:00 PM ET** weekly refresh. This mode
changes **cost/reporting behavior only** — it does **not** relax any trading
safety rule (see [safeguards.md](safeguards.md)).

### Daily compact weekday chain

| ET | Task | Role |
|---|---|---|
| 9:45 AM | `morning-dca-0945` | pre-flight validation (read-only) |
| 10:30 AM | `42173154` | morning live execution |
| 12:30 PM | `a6239dae` | midday live execution |
| 3:20 PM | `10ce362c` | final live execution (final buy of the day) |
| 3:35 PM | `67aa802b` | post-trade slippage + compact Notion **Latest Run** sync |
| 3:40 PM | `a46bf007` | live-run watchdog |

### Daily mode rules

- Write **compact artifacts only** — machine-readable JSON/CSV, no long narrative
  summaries.
- **Suppress no-op / heartbeat notifications**; notify **only for actionable
  events**: placed orders, rejected orders, candidates capped by budget,
  missing/invalid artifacts, broker/log mismatch, data-confidence failures,
  drift-detector failures, recovery attempts/failures, and meaningful exceptions
  (see [`config/notifications.json`](../config/notifications.json) →
  `daily_compact_mode`).
- **Use drift detection instead of manual checking.** The post-artifact drift
  detector (PR #80, merge commit `3641b90`) reconciles `signals.csv` against
  `submitted_orders.csv` after every live run and flags any auto-buy/actionable
  row with no submitted order — **including rows that carry an explicit
  per-row block/rejection reason**. Blocked rows are still reported (tagged
  `per_row_guard_or_block`) rather than silently excluded, so an operator can
  confirm the block was intended. See [drift_detection.md](drift_detection.md).
- **Batch market-data pulls** across the active universe where possible; persist
  each raw connector response **once** and have downstream steps read the saved
  files rather than re-pulling (see `raw_data_persistence` on task `10ce362c`).
- **Limit Notion writes**: the compact **Latest Run** update daily; refresh the
  **full** Notion page only in the Friday weekly refresh and the monthly
  dashboard refresh (`0656657a`, first weekday of the month).
- **Cap automated retries at one targeted recovery rerun.**
- **Model tiering**: cost-conscious / default models for routine repo, script,
  and summary maintenance; premium models only for major architecture/debugging.

### Friday heavy analysis (`041d9d90`, 5:00 PM ET)

Full Notion refresh, performance tables, slippage trend analysis, missing-fill
analysis, cost-basis / control comparisons, cost tracker update, and the full
weekly dashboard refresh. This is the only approved post-3:40 PM DCA-related
exception.

### Recovery policy

Avoid manual full reruns unless a detector, the watchdog, or a failed scheduled
run signals a real issue. Prefer **targeted recovery** of the specific failed
tickers or failed steps, capped at **one** recovery rerun.

## Monthly cost review (`bec4a6ee`)

- Runs the first weekday of the month at **9:55 AM ET**.
- Reports **usage proxies** (task-run counts, heavy-analysis counts, manual
  runs, approximate tokens). **No exact credit/billing figures** — exact
  Perplexity credit consumption is not exposed, so do not claim exact credits.

## Proxy metrics

| Proxy | Meaning |
|---|---|
| `task_runs` | Total scheduled task executions in the period |
| `heavy_analysis_runs` | Weekly/monthly full refreshes |
| `manual_runs` | Ad-hoc manual runs (should trend to zero) |
| `approx_tokens` | Rough token usage estimate, if available |

## Levers

- Prefer compact Notion "Latest Run" updates daily; full dashboard only weekly/monthly.
- Keep no-outlier days silent.
- Reuse cached/local data instead of re-fetching whole universes.
