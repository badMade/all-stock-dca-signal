# Graded DCA Strategy

Version-controlled backup of the current graded dollar-cost-averaging (DCA)
strategy. Human-readable and audit-friendly. **No credentials or secrets** are
stored in this repo.

Machine-readable source of truth: [`config/`](../config). This document mirrors
those files in prose.

## Overview

Each trading weekday, near the close, the strategy inspects the drop of each
active ticker versus its previous regular-session close and, if the buy signal
fires, places a single market buy sized by a graded multiplier of a **$3 base
amount**. All buys pass a review gate and respect the selected tiered
daily/weekly budget caps. It never sells.

The strategy identity is the **All Stock graded DCA live test**. Live execution
is on **Robinhood account `562057042` only**; Alpaca is read-only validation /
account context only.

## Signal rules

A buy signal fires for a ticker only when **all three** conditions hold:

1. The decision price is **below the previous regular-session close**.
2. The **60-minute intraday trend is down**.
3. The ladder **multiplier is > 0** (i.e. the drop is at least 0.1%).

The order dollar amount before budget constraints is
`base_amount_usd × multiplier` = `$3 × multiplier`.

## Active ticker universe

42 active symbols (see [`config/tickers.json`](../config/tickers.json)):

```
DFTX TRIN AAAU SPCX EVR LUNR IRDM DXYZ LITE ASML DRAM STX
MSFT AAPL AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR
INTC GOOGL GOOG AMZN GLW MA GS MU NFLX XLG TOPT QTOP SATA RGTI IBM
```

- **Excluded:** `BYDDY`, `VCX`, `TRLV`, `RVI`
- **Symbol corrections applied:** `IRDMI -> IRDM`, `GLWR -> GLW`

## Sizing ladder (latest, active)

Maps the drop vs. previous close to a buy multiplier of the $3 base amount.
Boundaries are min-inclusive, max-exclusive. There is **no upper cutoff**: any
drop of 8% or more is 5x. See [`config/sizing_ladder.json`](../config/sizing_ladder.json).

| Drop vs. prior close | Multiplier | Dollars (base $3) |
|---|---|---|
| < 0.1%          | 0x (no buy) | $0.00 |
| 0.1% to < 1%    | 1x   | $3.00 |
| 1% to < 2%      | 1.5x | $4.50 |
| 2% to < 3%      | 2x   | $6.00 |
| 3% to < 4%      | 2.5x | $7.50 |
| 4% to < 5%      | 3x   | $9.00 |
| 5% to < 6%      | 3.5x | $10.50 |
| 6% to < 7%      | 4x   | $12.00 |
| 7% to < 8%      | 4.5x | $13.50 |
| >= 8%           | 5x   | $15.00 |

### Historical / superseded ladders

Older ladder variants — **including the 0.1%-to-12% sizing ladders** — are
**historical and superseded**. The active ladder above (0.1% to 8%+, max 5x, no
upper cutoff) is the only one in force. If a request references the "0.1% to 12%
ladder," treat it as background context, not the current rule.

## Budget & tiered cap rules

Base amount is exactly **$3.00**. See
[`config/budgets.json`](../config/budgets.json) (`base_amount_policy`). Every
candidate dollar amount is **`$3.00 × multiplier`**. The automation must never
infer, discover, or fall back to **$50.00** or any broker/account default base
amount. If the base-amount config is missing or unreadable, continue using
**$3.00** and record `base_amount_source='hardcoded_user_confirmed_3_usd'`.

The daily and weekly caps are not fixed: each run selects a budget tier from the
day's planned spend and the number of **3x-or-greater signals** (buy candidates
whose ladder multiplier is >= 3.0). Pick the highest tier triggered by **either**
dimension.

| Tier | Select when | Daily cap | Weekly cap |
|---|---|---|---|
| Normal | planned spend <= $129 **and** 3x+ signals <= 5 | $180 | $900 |
| Elevated | planned spend > $129 **or** 3x+ signals 6-10 | $240 | $1,200 |
| High-volatility | planned spend > $213 **or** 3x+ signals 11-17 | $300 | $1,500 |
| Stress | planned spend > $272 **or** 3x+ signals > 17 | $360 | $1,800 |

- **Stress** capacity is usable only to the extent prior unused weekly rollover
  reserve is available.
- The **weekly rollover reserve is capped at $900**.
- Allocations are prioritized by **higher multiplier first, then larger percent
  drop, then ticker** (alphabetical) for a deterministic tie-break. Capped
  tickers are flagged.
- **One strategy order per ticker per day.**

Caps are absolute ceilings: a run never exceeds the selected daily cap, the
selected weekly cap, available buying power, or the broker/order-review limit.

## Same-day prior-run offset / top-up guard

A morning or manual run must **never block** the scheduled afternoon (3:20 PM ET)
run. Instead, the scheduled run treats any earlier same-day allocation as
prior-allocation context and **tops up** only when it is buying into a lower
price:

- **No earlier same-day allocation** for the ticker → normal allocation (buy the
  full afternoon desired amount).
- **Earlier allocation exists** and the afternoon decision price is **strictly
  below** the earlier run's reference price → buy only the **incremental** dollar
  difference (afternoon desired − amount already bought); $0 if that is ≤ 0.
- **Earlier allocation exists** but the afternoon price is **not below** the
  reference price → buy **$0**.
- **Earlier amount known but no comparable price** (missing prior reference or
  current afternoon price) → conservative **$0** with `top_up_manual_review`
  flagged, rather than topping up blind.

Worked example: a morning run bought $3 of a ticker; the afternoon signal wants
$6. If the afternoon price is below the morning reference, the afternoon run buys
the **$3** difference. If the afternoon desired ≤ the morning amount, it buys
**$0**. If the afternoon price is not below the morning reference, it buys **$0**
even though the signal fired.

Only earlier runs generated **before 2:30 PM ET** contribute, and scheduled runs
are never read as prior context — so morning/manual orders before 2:30 PM ET are
read for top-up only, never as afternoon scheduled execution evidence,
slippage, watchdog confirmation, scheduled cost basis, or scheduled fill counts.
When multiple earlier runs touched a ticker the dollar amounts are summed and the
**lowest** available reference price wins. The scheduled run always writes a valid
scheduled `run_state` even when every buy is fully offset to $0.

Per candidate, the scheduled `run_state` records `prior_same_day_amount`,
`prior_same_day_reference_price`, `prior_same_day_run_id`, `prior_same_day_source`,
`afternoon_desired_amount`, `afternoon_decision_price`, `incremental_order_amount`,
`top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review` (see
[`schemas/run_state.schema.json`](../schemas/run_state.schema.json)).

## Execution constraints

See [`docs/safeguards.md`](safeguards.md) for the full list. Summary:

- Only broker/account for live/manual runs: **Robinhood Agentic account `562057042`**.
- Alpaca is read-only validation / account context only (unless explicitly changed later).
- One strategy order per ticker per day.
- Market buy orders with `dollar_amount` notional and `time_in_force = gfd`, only after `review_equity_order` passes.
- No sells, options, crypto, margin expansion, order replacement, account changes, or trading outside regular hours.
- Test window approved through **Fri Aug 28, 2026 inclusive**.
