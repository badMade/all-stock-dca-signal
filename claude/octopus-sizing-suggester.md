# Octopus and the sizing suggester

Written 2026-10-08. Reference for how Wondrous Octopus forecasts relate to the `SizingSuggester`
contract, and what is and is not wired into live buying. Nothing here changes the ladder.

## What is wired to live sizing: nothing from Octopus

- `wondrous_octopus` implements `ForecastProvider.forecast(ticker, asof) -> ForecastResult` only. It
  deliberately does not implement `SizingSuggester`, so it cannot reach live sizing (see the class
  docstring in `dca-sizing-signals/dca_shared_interfaces/forecasting.py` and `docs/wondrous_octopus.md`).
- `SizingSuggester.suggest(ticker, asof) -> SizingSuggestion` (verified 2026-10-08) is the opt-in contract.
  Today only `ml_forecaster` and, proposed, `connector_signals` fit it. It has no write, apply or publish
  method. The suggester is **not** wired into live sizing, and this change does not wire it.
- The 1x/1x/2x/3x ladder and the per-ticker calibration (`config/per_ticker_sizing.json`) are unchanged.

## Field mappings: verified vs. unverified

| Claim | Status |
|---|---|
| Protocol method is `suggest(ticker, asof)` | **Verified** |
| `SizingSuggestion` fields: `ticker, asof, multiplier_adjustment_pct, eval_metric_mae, source, model_version, generated_at` | **Verified** |
| `predicted_price = P50` | **Unverified, and not a suggester field.** `SizingSuggestion` has no price. `ForecastResult.point_estimate` is the provider's point estimate, which is not necessarily the P50. Octopus does emit p10/p50/p90 bands per OHLC component (`docs/wondrous_octopus.md`), so P50 is available only from the Octopus daily forecast CSV, not through either Protocol. |
| `previous_low = prior-day intraday low` | **Unverified.** No such field in either contract. The order-plan code carries `prior_low` (`dca_order_plan.py`); confirm its definition before reusing the name. |

Until both are confirmed against the code that produces them, treat them as assumptions.

## Execution routing (why this note mentions it)

Primary buys run locally. A cloud run is calculation-only and cannot place orders. Perplexity is the backup
buyer and acts only when the local run is unreachable, off, or errored. Details, thresholds and the
`UNKNOWN` stand-down rule are in `docs/adr-plan-first-backup-executor.md` ("Amendment: execution routing") and
`dca_execution_router.py`. Octopus outputs and suggester outputs are inputs to calculation at most; they
never decide who buys.
