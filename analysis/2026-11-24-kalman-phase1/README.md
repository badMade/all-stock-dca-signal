# Kalman residual-dip signal: Phase 1 inputs and trial rows (2026-11-24)

One-off research scripts behind the second round of Phase 1 runs in
[`docs/research/2026-11-23-kalman-phase1-results.md`](../../docs/research/2026-11-23-kalman-phase1-results.md).
Nothing here is read by the strategy or by `kalman_signals`; outputs are kept outside the repo
(price data and generated reports are not committed). Run from `dca-sizing-signals/` with
`PYTHONPATH=.` so the `comovement_signals`, `kalman_signals` and `market_data_ingest` imports resolve.

| Script | What it does | Network |
| --- | --- | --- |
| `build_clusters.py <history-dir> <out.json>` | Point-in-time co-movement clusters (`comovement_signals.build_clusters`) as of 2023-12-29, the session before the backtest window, so membership uses no scored-period data. Output `members` is the `--clusters` input. | No |
| `fetch_earnings.py <tickers.json> <out.json>` | Past earnings announcement dates per ticker from yfinance (`get_earnings_dates(limit=24)`), the `--earnings` input. | Yes (Yahoo) |
| `record_trials.py rows <out.json> [<reports-dir>]` | One `LedgerStore.ingest_trials` row per walk-forward run report. | No |
| `record_trials.py ingest <rows.json> <ledger.duckdb>` | Writes those rows. Idempotent (content-hashed). Run against the Phase 0 ledger 2026-11-24 (4 rows). | No |

SPY daily history came from the `stock-history-downloader` skill into a directory outside the
managed `ticker_data` archive and was passed with `--market-file`.
