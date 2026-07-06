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

| Rule | Value |
|---|---|
| Base amount | $1 |
| Daily base cap | $60 |
| Carry-forward reserve (max) | $420 |
| Hard daily max | $120 |
| Weekly hard cap | $600 |

The hard daily max and weekly cap are absolute ceilings. Reserve carry-forward
never pushes a day above the hard daily max or the week above the weekly cap.

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
