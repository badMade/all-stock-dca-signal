# Safeguards & Budget Rules

The binding safety rules for the graded DCA strategy. These override
convenience: if a rule and an automation conflict, the rule wins and the run
should skip rather than trade. Machine-readable copies live in
[`config/budgets.json`](../config/budgets.json) and
[`config/notifications.json`](../config/notifications.json).

## Broker / account

- **Robinhood Agentic account `562057042` is the only execution broker/account**
  for live and manual runs.
- **Alpaca, Finance, and Yahoo are read-only** — validation, market-data, and
  account context only; never execution — unless the user explicitly changes this
  later.

## Market-data source policy

- The **Finance connector is the PRIMARY market-data source** for quotes, prior
  closes, spreads, and trend inputs.
- **Alpaca and Robinhood provide validation / context** on top of the primary
  source.
- **Yahoo Finance via `yfinance` is a FALLBACK ONLY**, used when the Finance
  connector data is unavailable, stale, incomplete, or unresolved. Fallback data
  **must be labeled as fallback** and must **not** weaken confidence, spread,
  trend, top-up, budget, whitelist, buy-only, regular-hours-only, or any other
  safety rule.
- **Robinhood remains the only execution broker** regardless of which source
  produced the market data.
- **GFUZ exception:** GFUZ may use **Robinhood as its PRIMARY** source when the
  Finance connector cannot resolve it (see
  `config/tickers.json:special_handling.GFUZ` and
  [morning_dca.md](morning_dca.md#gfuz-special-handling)).

See the `market_data_source_policy` block in
[`config/budgets.json`](../config/budgets.json) and
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

## Trading constraints

- **No sells.** Buys only.
- **No options, no crypto, no margin changes.**
- **No order replacement, no order cancellations, no account modifications.**
- **No trading outside regular market hours.**
- **One strategy order per ticker per stream per day.**
- Orders are **market buys** with a `dollar_amount` notional and
  `time_in_force = gfd`.
- **Submission path (approved Fri Jul 10 2026 11:20 AM ET):**
  - **Scheduled live tasks are pre-authorized.** They **must not** request
    per-order confirmation and **must not** call Robinhood `review_equity_order`
    (which forces a preview + confirmation). They call `place_equity_order`
    directly **only after every internal validation, signal, budget, spread,
    confidence/trend, and top-up check passes**.
  - **Manual / ad hoc runs still use the review + confirmation path** —
    `review_equity_order` must pass before any placement.
- None of the above may change unless the user explicitly requests it later.

## Circuit breakers

Hard limits that skip an order rather than trade when any is hit:

- **Max per order: $10** (5x × $2 base) — the absolute per-order ceiling across
  all live streams.
- Never exceed the selected **daily cap**, **weekly cap**, or **available buying
  power**.
- **Whitelisted (active-universe) tickers only.**
- **Skip** when data confidence fails, the bid/ask spread is too wide, the
  intraday trend cannot be validated down (flag `manual_review_candidate` instead
  of buying), or a same-day top-up's current price is not strictly below the prior
  same-day buy reference price.
- **No more than one strategy order per ticker per stream.**

See the `execution_authorization_policy` and `circuit_breakers` blocks in
[`config/budgets.json`](../config/budgets.json).

## Budget caps

Base amount is **$2**. Daily and weekly caps are selected per run by tier, based
on the day's planned spend and the count of 3x-or-greater signals (multiplier
>= 3.0). Pick the highest tier triggered by **either** dimension.

| Tier | Select when | Daily cap | Weekly cap |
|---|---|---|---|
| Normal | planned spend <= $129 **and** 3x+ signals <= 5 | $180 | $900 |
| Elevated | planned spend > $129 **or** 3x+ signals 6-10 | $240 | $1,200 |
| High-volatility | planned spend > $213 **or** 3x+ signals 11-17 | $300 | $1,500 |
| Stress | planned spend > $272 **or** 3x+ signals > 17 | $360 | $1,800 |

- The selected daily and weekly caps are absolute ceilings.
- **Stress** tier capacity is usable only to the extent prior unused weekly
  rollover reserve is available. The **weekly rollover reserve is capped at $900.**
- Allocations are prioritized by higher multiplier first, then larger percent
  drop, then ticker (alphabetical) for a deterministic tie-break; capped tickers
  are flagged.
- A run never exceeds the selected daily cap, the selected weekly cap, available
  buying power, or the broker/order-review limit.
- **Dynamic risk kill-switch:** when capital deployed by the scheduled-live
  afternoon stream in any trailing 7-day window exceeds **40% of the monthly
  cash allocation** (`monthly_cash_allocation_usd`, default $3,600), the graded
  ladder is **capped at 1x** — the strategy keeps buying dips at the $2 base but
  stops scaling up until the window rolls back under the threshold. A rolling
  30-day cash-burn rate is tracked for visibility. Manual and other streams are
  never counted toward this guard. See [risk_management.md](risk_management.md)
  and the `risk_guard` block in [`config/budgets.json`](../config/budgets.json).

## Test window

- Live testing is approved **through Fri Oct 30, 2026, inclusive**, per
  `config/trading_window.json` (extended 2026-07-26 from the original Aug 28,
  2026 end date to avoid a silent hard stop while Phase 2's real market
  calendar is pending — see `PRODUCTION_READINESS.md`). The window
  deliberately stops before the **2026-11-01 EDT→EST transition**: the three
  live tasks' cron schedules (and the external trigger gate below) are
  fixed-UTC/EDT-only, so trading through that DST change would fire live
  trades at the wrong ET time (or have them rejected outright) until the
  external scheduler's cron is made DST-aware — see
  `config/trading_window.json`'s `description` field.
- `near_close_dca.py`'s date gate independently blocks full NYSE closures
  (e.g. Labor Day, Thanksgiving) via a computed holiday calendar
  (`nyse_holidays()`), regardless of what `config/trading_window.json`
  approves — see [schedule_manifest.md](schedule_manifest.md).
- This is the **code-level gate** enforced by `near_close_dca.py`'s date check.
  The live task's separate trigger gate (`programmatic_trigger_check.sh`) lives
  outside this repo (scheduler-side) and was last documented as enforcing
  weekdays **2026-07-02 → 2026-08-28**, **3:30–3:35 PM ET** (retimed from
  3:20 PM to align with the canonical 3:30 PM ET final live task); confirm/extend
  it independently, since extending `config/trading_window.json` alone does not
  change that external script.
- A one-time reminder (`f7411889`) fires 2026-08-15 to decide on continuation —
  reschedule it too now that the window runs through Oct 30.

## Operational rules

- The live task **must write standardized same-day state files** and **must not
  fall back to interactive confirmation** — it runs unattended.
- **3:30 PM state-file guarantee:** the live task **always** writes the scheduled
  `run_state.json` under `scheduled_runs/YYYYMMDD_1530ET/` and updates
  `latest_scheduled_run.json` on every fire, **even when no order is placed**. If
  Robinhood review passes but submission is blocked by required post-review user
  confirmation, the run_state stays valid and carries
  `order_submit_status='not_submitted_confirmation_required'`, `reviewed_order`
  details where available, candidate details, and **no `order_id`**. Such a run
  is valid scheduled-run evidence that the task ran but **not** evidence of an
  order/fill: slippage reports no scheduled fills/order IDs (and does not
  invalidate the run), and the watchdog alerts on no order placed without marking
  the lineage invalid. See [run_classification.md](run_classification.md).
- **Manual runs do not count as scheduled-run evidence.** Every run_state
  artifact must carry a `run_classification` block distinguishing scheduled from
  manual runs. Scheduled runs write under
  `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/` and update
  `latest_scheduled_run.json`; manual runs write under `manual_runs/<timestamp>/`
  and **never** update that pointer. Downstream reporting excludes all manual
  order IDs and any `run_origin='manual'` / `manual_run=true` state, and never
  infers scheduled status from broker orders alone. See
  [run_classification.md](run_classification.md) and
  [`config/run_classification.json`](../config/run_classification.json).
- **Automatic failure recovery (live trading tasks):** the scheduled live tasks
  (morning `42173154`, midday `a6239dae`, afternoon `10ce362c`) treat a mid-run
  failure caused by **internal persistence, parsing, wrapper-normalization,
  transient connector throttling, or raw-file write** problems **after data was
  fetched** as a **recovery trigger, not a terminal stop**. The task reruns the
  required read-only inputs **once**, **persists all raw responses first**,
  **recomputes only from the persisted files**, and continues through the same
  pre-authorized guarded path **only if every safeguard above still passes** —
  no rule is loosened for recovery. The recovery reuses the same scheduled run
  directory with `recovery_rerun=true` and is **never** reclassified as manual.
  If the recovery attempt **also fails**, or a **safety / account / order
  rejection** occurs, the task writes `run_state.json` with the blocking reason
  and **escalates** rather than retrying further or dropping to an interactive
  path. See the `failure_recovery_policy` block in
  [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and
  [run_classification.md](run_classification.md#recovery-reruns).
- **Manual missed-morning recovery (added Jul 20 2026):** if the **10:30 AM
  scheduled live morning task did not run at all**, a human may place the missed
  morning buys by hand under
  `.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*` (example
  `20260720_1048ET`). These artifacts are **manual** (`run_origin='manual'`,
  `manual_run=true`, `same_day_top_up_context_only=true`,
  `valid_scheduled_evidence=false`): prior-allocation / same-day top-up context
  **only**, never proof the 10:30 scheduled task ran and never scheduled
  evidence. The **12:30 midday** and **3:30 final** runs must scan this path and
  fold any same-day recovery fills into the top-up guard as prior allocation. See
  [run_classification.md](run_classification.md#manual-missed-morning-recovery-added-2026-07-20).
- **Afternoon completion rule:** all afternoon **trading and artifact-writing**
  tasks complete **no later than 3:40 PM ET**, when the read-only watchdog
  (`a46bf007`) begins its final verification. The watchdog's own post-3:40 PM
  verification and the Friday weekly Notion performance refresh at **5:00 PM ET**
  are the only approved post-3:40 PM exceptions.
- **Cost-control policy:** daily tasks are compact / local-first; heavy analysis
  runs Fridays and monthly; avoid manual full runs unless automation fails.

## Notifications

- **No Slack.**
- Notifications go to the **in-app / message center**, and **email only where a
  task explicitly says so** (no routine dashboard emails).
