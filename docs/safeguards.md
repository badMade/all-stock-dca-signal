# Safeguards & Budget Rules

The binding safety rules for the graded DCA strategy. These override
convenience: if a rule and an automation conflict, the rule wins and the run
should skip rather than trade. Machine-readable copies live in
[`config/budgets.json`](../config/budgets.json) and
[`config/notifications.json`](../config/notifications.json).

## Broker / account

- **Robinhood Agentic account `562057042` is the only execution broker/account**
  for live and manual runs.
- **Alpaca is read-only** — validation and account context only — unless the
  user explicitly changes this later.

## Trading constraints

- **No sells.** Buys only.
- **No options, no crypto, no margin expansion.**
- **No order replacement, no account changes.**
- **No trading outside regular market hours.**
- **One strategy order per ticker per day.**
- Orders are **market buys** with a `dollar_amount` notional and
  `time_in_force = gfd`, submitted **only after `review_equity_order` passes**.

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
- Allocations are prioritized by higher multiplier first, then larger percent drop.
- A run never exceeds the selected daily cap, the selected weekly cap, available
  buying power, or the broker/order-review limit.

## Test window

- Live testing is approved **through Fri Aug 28, 2026, inclusive.**
- The live task's trigger gate (`programmatic_trigger_check.sh`) enforces
  weekdays **2026-07-02 → 2026-08-28**, **3:20–3:55 PM ET**.
- A one-time reminder (`f7411889`) fires 2026-08-15 to decide on continuation.

## Operational rules

- The live task **must write standardized same-day state files** and **must not
  fall back to interactive confirmation** — it runs unattended.
- **Cost-control policy:** daily tasks are compact / local-first; heavy analysis
  runs Fridays and monthly; avoid manual full runs unless automation fails.

## Notifications

- **No Slack.**
- Notifications go to the **in-app / message center**, and **email only where a
  task explicitly says so** (no routine dashboard emails).
