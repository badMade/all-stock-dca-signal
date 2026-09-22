# Weekly-Rollover Intraday Cap Curve Backtest

Analytics only. This document records a **non-live backtest** of an intraday
cumulative cap curve layered on top of a weekly-rollover funding model. Nothing
here changes live trading. The live afternoon/morning/midday streams keep using
the active [`config/sizing_ladder.json`](../config/sizing_ladder.json) ($3-base
0x-to-5x ladder) and the standing safeguards in [safeguards.md](safeguards.md).
Machine-readable candidate/result data:
[`config/backtest_intraday_cap_curve.json`](../config/backtest_intraday_cap_curve.json).

## Backtest model

- **Base amount:** $1 per candidate for this backtest (not the live $3 base). The
  live base amount policy is unchanged and remains $3.00 (see
  [strategy.md](strategy.md) and [`config/budgets.json`](../config/budgets.json)).
- **Weekly funding:** $300 is added at the start of each week. Any unused weekly
  cash rolls forward.
- **Rollover arithmetic:** the current week's available cash equals $300 plus the
  prior week's unused cash. Worked example: if a week has $360 available and
  spends $210, the following week starts with $450 ($300 + $150 unused).
- **Rollover reset:** any week that fully spends its accumulated cash leaves $0 to
  roll forward, so the next week resets to the $300 base. Example continuing the
  above: a week that starts with $450 and spends all $450 leaves the following
  week starting again at $300.
- **Daily cap:** the remaining weekly cash divided by the number of remaining
  trading days in the week. The intraday cumulative cap curve (below) then applies
  to that calculated daily cap.
- **Historical prices:** unchanged from the prior 60 trading day test.
- **Intraday proxy:** 10:30 uses the daily open, 12:30 uses the average of the
  daily open and daily low, and the final slot uses the daily close.

## Intraday cumulative cap curve

A curve written `A / B / C` is the cumulative percentage of the day's calculated
daily cap that may be deployed by each intraday decision time:

- **A** = cumulative cap at 10:30 (percent of the daily cap).
- **B** = cumulative cap at 12:30 (percent of the daily cap).
- **C** = cumulative cap at the final slot (always 100, the full daily cap by close).

Example: a `35 / 65 / 100` curve allows up to 35% of the daily cap to be deployed
by 10:30, up to 65% by 12:30, and up to 100% by the 3:30 final slot.

## Results (60 trading day window)

| Curve | Total deployed | Fills | Cap skips | Final spend share | Ending rollover | Discount vs prior close |
| --- | --- | --- | --- | --- | --- | --- |
| 65 / 90 / 100 | $3,546.50 | 2,733 | 6 | 10.45% | $353.50 | 3.44% |
| 50 / 85 / 100 | $3,544.50 | 2,721 | 32 | 10.51% | $355.50 | 3.45% |
| 40 / 75 / 100 | $3,541.50 | 2,704 | 64 | 10.70% | $358.50 | 3.46% |
| **35 / 65 / 100** (highlighted) | **$3,533.50** | **2,677** | **104** | **11.04%** | **$366.50** | **3.48%** |
| 33 / 66 / 100 | $3,536.00 | 2,671 | 111 | 10.96% | $364.00 | 3.48% |

Column meanings:

- **Total deployed:** dollars actually bought across the window.
- **Fills:** number of executed buy fills.
- **Cap skips:** candidate buys skipped because the intraday cumulative cap for
  that decision time was already reached.
- **Final spend share:** share of the final (3:30) decision in total deployment.
- **Ending rollover:** unused weekly cash carried past the end of the window.
- **Discount vs prior close:** average buy price discount versus the prior
  completed regular-session close.

## User-highlighted candidate: 35 / 65 / 100

The **35 / 65 / 100** curve is the candidate highlighted by the user. Relative to
the flatter, front-loaded curves it holds more cap back for the midday and final
decisions, which shows up as:

- The **best discount vs prior close** in the set (3.48%, tied with 33 / 66 / 100)
  and the **highest final spend share** (11.04%), consistent with deferring more
  buying power toward later, typically lower, intraday proxy prices.
- A **higher cap-skip count** (104) than the front-loaded curves, the expected
  cost of a tighter early-session cap.
- Slightly **lower total deployed** ($3,533.50) and a **larger ending rollover**
  ($366.50), meaning more unused cash rolls forward rather than being spent early.

This is a backtest candidate for review, not a live configuration change.

## Review cadence

Per [schedule_manifest.md](schedule_manifest.md), the monthly review should
include this weekly-rollover intraday cap curve analysis and re-evaluate the
user-highlighted **35 / 65 / 100** candidate against the alternatives above.
