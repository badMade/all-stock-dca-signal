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

- **Max per order: $15** (5x × $3 base) — the absolute per-order ceiling across
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

Base amount is **$3**. Daily and weekly caps are selected per run by tier, based
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
  ladder is **capped at 1x** — the strategy keeps buying dips at the $3 base but
  stops scaling up until the window rolls back under the threshold. A rolling
  30-day cash-burn rate is tracked for visibility. Manual and other streams are
  never counted toward this guard. See [risk_management.md](risk_management.md)
  and the `risk_guard` block in [`config/budgets.json`](../config/budgets.json).

## Test window

- Live testing is approved **through Fri Aug 28, 2026, inclusive.**
- The live task's trigger gate (`programmatic_trigger_check.sh`) enforces
  weekdays **2026-07-02 → 2026-08-28**, **3:20–3:35 PM ET** (aligned to the
  15:15–15:35 ET scheduled classification window and the 3:40 PM completion rule).
- A one-time reminder (`f7411889`) fires 2026-08-15 to decide on continuation.

## Operational rules

- The live task **must write standardized same-day state files** and **must not
  fall back to interactive confirmation** — it runs unattended.
- **3:20 PM state-file guarantee:** the live task **always** writes the scheduled
  `run_state.json` under `scheduled_runs/YYYYMMDD_1520ET/` and updates
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
  `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` and update
  `latest_scheduled_run.json`; manual runs write under `manual_runs/<timestamp>/`
  and **never** update that pointer. Downstream reporting excludes all manual
  order IDs and any `run_origin='manual'` / `manual_run=true` state, and never
  infers scheduled status from broker orders alone. See
  [run_classification.md](run_classification.md) and
  [`config/run_classification.json`](../config/run_classification.json).
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
