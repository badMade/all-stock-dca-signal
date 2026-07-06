# Graded DCA Strategy

Version-controlled backup of the current graded dollar-cost-averaging (DCA)
strategy. Human-readable and audit-friendly. **No credentials or secrets** are
stored in this repo.

Machine-readable source of truth: [`config/`](../config). This document mirrors
those files in prose.

## Overview

Each trading weekday, near the close, the strategy inspects the drop of each
active ticker versus its previous regular-session close and, if the drop clears
the ladder's minimum, places a single market buy sized by a graded multiplier of
a $1 base amount. All buys pass a review gate and respect daily/weekly budget
caps. It never sells.

## Active ticker universe

45 active symbols (see [`config/tickers.json`](../config/tickers.json)):

```
DFTX TRIN AAAU SPCX TRLV RVI VCX EVR LUNR IRDM DXYZ LITE ASML DRAM STX
MSFT AAPL AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR
INTC GOOGL GOOG AMZN GLW MA GS MU NFLX XLG TOPT QTOP SATA RGTI IBM
```

- **Excluded:** `BYDDY`
- **Symbol corrections applied:** `IRDMI -> IRDM`, `GLWR -> GLW`

## Sizing ladder (latest, active)

Maps the drop vs. previous close to a buy multiplier of the $1 base amount.
Boundaries are min-inclusive, max-exclusive. There is **no upper cutoff**: any
drop of 8% or more is 5x. See [`config/sizing_ladder.json`](../config/sizing_ladder.json).

| Drop vs. prior close | Multiplier | Dollars (base $1) |
|---|---|---|
| < 0.1%          | 0x (no buy) | $0.00 |
| 0.1% to < 1%    | 1x   | $1.00 |
| 1% to < 2%      | 1.5x | $1.50 |
| 2% to < 3%      | 2x   | $2.00 |
| 3% to < 4%      | 2.5x | $2.50 |
| 4% to < 5%      | 3x   | $3.00 |
| 5% to < 6%      | 3.5x | $3.50 |
| 6% to < 7%      | 4x   | $4.00 |
| 7% to < 8%      | 4.5x | $4.50 |
| >= 8%           | 5x   | $5.00 |

### Historical / superseded ladders

Older ladder variants — **including the 0.1%-to-12% sizing ladders** — are
**historical and superseded**. The active ladder above (0.1% to 8%+, max 5x, no
upper cutoff) is the only one in force. If a request references the "0.1% to 12%
ladder," treat it as background context, not the current rule.

## Budget & reserve rules

See [`config/budgets.json`](../config/budgets.json).

| Rule | Value |
|---|---|
| Base amount | $1 |
| Daily base cap | $60 |
| Carry-forward reserve (max) | $420 |
| Hard daily max | $120 |
| Weekly hard cap | $600 |

Reserve logic (applied once per trading day):

- `unused_base = max(0, daily_base_cap - actual_spend)` — calm days build reserve.
- `reserve_draw = min(current_reserve, max(0, actual_spend - daily_base_cap))` — red days draw reserve.
- `reserve_next = min(reserve_cap, max(0, reserve_start - reserve_draw + unused_base))`.
- `allowed_today = max(0, min(hard_daily_max, daily_base_cap + current_reserve, weekly_cap - week_spend))`.

## Execution constraints

See [`docs/safeguards.md`](safeguards.md) for the full list. Summary:

- Only broker/account for live/manual runs: **Robinhood Agentic account `562057042`**.
- Alpaca is read-only validation / account context only (unless explicitly changed later).
- One strategy order per ticker per day.
- Market buy orders with `dollar_amount` notional and `time_in_force = gfd`, only after `review_equity_order` passes.
- No sells, options, crypto, margin expansion, order replacement, account changes, or trading outside regular hours.
- Test window approved through **Fri Aug 28, 2026 inclusive**.
