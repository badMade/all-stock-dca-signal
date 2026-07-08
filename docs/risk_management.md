# Dynamic Risk Management

The graded DCA strategy participates in dips, but must not empty the month's
funding in a single volatile stretch. Two version-controlled mechanisms bound
that risk. Both are computed from the same `run_state*.json` artifacts the
automation already writes, so nothing new needs to be plumbed through the broker.

Machine-readable config lives in the `risk_guard` block of
[`config/budgets.json`](../config/budgets.json); the implementation is
[`dca_risk.py`](../dca_risk.py) plus the sizing cap in
`near_close_dca.graded_buy_multiplier`.

## 1. Rolling 30-day cash burn rate

`dca_risk.rolling_cash_burn` sums capital *actually deployed* (submitted buy
notional) over the trailing 30 calendar days and reports:

- `total_deployed_usd` — capital deployed in the window.
- `daily_burn_rate_usd` — `total_deployed_usd / 30`, a steady runway figure
  that does not spike just because trades cluster on a few days.
- `active_deployment_days` — how many days in the window actually deployed.

This is analytics/visibility only; it does not by itself change sizing.

## 2. The 7-day deployment kill-switch

When capital deployed in **any trailing 7-day window** exceeds
`kill_switch_7d_deploy_fraction` (default **40%**) of
`monthly_cash_allocation_usd` (default **$3,600**), the kill-switch engages and
the graded ladder is **capped at 1x**:

- Every positive multiplier (1.5x, 2x, … up to 5x) collapses to **1x**.
- A **0x** (no-buy) stays **0x** — the guard never forces a buy.
- The strategy therefore keeps **participating in dips at the $3 base amount**
  but stops scaling up until the rolling 7-day deployment falls back under the
  threshold.

The cap is applied inside `near_close_dca.graded_buy_multiplier` via
`kill_switch_engaged(config)`. The trailing-7-day figure
(`config.deployed_last_7d_usd`) is a **runtime** value injected per run by
`dca_risk.apply_risk_guard`; it is never read from config JSON, so loading config
can never by itself alter sizing.

### Worked example — a 20% market-wide correction

During a sharp market-wide drawdown, many tickers show large drops on the same
day, so the un-capped ladder would fire 3x–5x across the universe and the daily
caps would ramp to the elevated/high-volatility/stress tiers. Left alone, a
week of that could deploy well over 40% of the month's cash in the first few
sessions and leave nothing for the continued slide.

With the guard in place, a hypothetical 20% correction plays out like this:

1. **Days 1–2 (dip appears):** trailing-7-day deployment is still under 40% of
   the monthly allocation, so the full 0x–5x ladder runs — the strategy leans
   into the early dip exactly as designed.
2. **Threshold crossed:** once the trailing-7-day deployment passes
   `0.40 × $3,600 = $1,440`, the kill-switch engages. Every subsequent buy in
   that window is sized at **1x ($3)**, not 2x–5x.
3. **Still participating:** because the cap is 1x (not 0x), the strategy keeps
   buying the ongoing dip every day it qualifies — it just does so at the base
   amount, preserving cash for the rest of the correction instead of front-loading it.
4. **Automatic release:** as the earliest days age out of the 7-day window,
   trailing deployment falls back under $1,440 and the full ladder resumes with
   no manual intervention.

Net effect: aggressive early participation, then a self-imposed "base-rate only"
mode that stretches the remaining monthly cash across a prolonged decline, then
automatic recovery — no trading is ever halted outright.

## Stream hygiene (no conflation)

Deployment is accounted **per stream** and never mixed
(see [run_classification.md](run_classification.md)):

- Only the **scheduled-live afternoon** stream funds the kill-switch: a
  `run_origin='scheduled'` run whose timestamp is **at or after 2:30 PM ET**.
- **Manual** runs (`run_origin='manual'` / `manual_run=true`) are tracked
  separately and **never** counted toward scheduled-live deployment.
- Monitor/analytics/morning runs place no trades and are classified as `other`.
- A scheduled artifact whose timestamp is **before 2:30 PM ET** is **rejected**
  as afternoon evidence, so a stray pre-2:30 PM or morning artifact can never
  inflate scheduled-live deployment.

`dca_risk.build_risk_report` reports 7-day deployment for every stream so an
operator can confirm the manual and other streams are excluded from the guard.

## Execution constraints (unchanged)

The guard changes sizing only. It does **not** relax any standing constraint:

- **Robinhood** account `562057042` remains the **only** execution broker.
- **Alpaca** stays **read-only** validation/account context.
- No sells, options, crypto, margin, cancellations; buys only, regular hours,
  one order per ticker per day.

## Live vs. backtest ladders

- **Live / manual execution** always uses the active
  [`config/sizing_ladder.json`](../config/sizing_ladder.json) — the **$3-base
  0x-to-5x** ladder — unless the kill-switch above caps it to 1x.
- **Analytics only**, the 12-month backtest uses the coarser
  [`config/backtest_sizing_ladder.json`](../config/backtest_sizing_ladder.json):

  | Drop vs. prior close | Multiplier |
  |---|---|
  | 0.1% – <2% | 1x |
  | 2% – <5% | 1.5x |
  | 5% – <8% | 2x |
  | 8%+ | 3x |

  This ladder is **never** loaded into the live `StrategyConfig`
  (`dca_config.load_backtest_sizing_tiers` returns it in isolation); it exists
  purely so analytics can be run against a simpler curve without touching what
  the live task does.

## CLI

```
run-dca-risk --state-root <cron_tracking/ce16ba6b> --as-of 2026-07-08
# optional overrides:
run-dca-risk --state-root <dir> --monthly-allocation-usd 3600 --kill-switch-fraction 0.40
```

Prints the JSON risk report (30-day burn, 7-day deployment, kill-switch status,
per-stream breakdown).
