# ADR: Massive + Alpaca as the ongoing price source (Yahoo stays as `yahoo_legacy`)

- **Status:** Proposed (2026-10-18). Not wired into any live path.
- **Scope:** `dca-sizing-signals/market_data_ingest/prices/` only. The stdlib-only core, the
  ladder, `config/`, and all three entry points (`run_dca.run_slot()`, the
  `MorningDCAExecutor` hierarchy, `dca_runner.py`) are untouched.
- **Plan of record:** `PLAN-price-source.md`, reviewed with Codex in
  `SAME-PAGE-LOG-price-source.md` (SAME PAGE after two rounds).

## Context

Yahoo Finance is unreliable and costly. Its history is already pulled into `ticker_data/*.csv`
and stays, labelled `yahoo_legacy`. Robinhood MCP gives current quotes only, so it cannot
supply nightly bars or corporate actions. The brief referenced a backfill ADR at
`claude/architecture.md` and a database at `data/backfill.duckdb`; neither exists in the
repo, its history, or on the operator's Mac, so this ADR does not build on them.
`dca-sizing-signals/market_data_ingest/` is the package its README names as "the one place in
this repo allowed to make network calls", so the new code goes there.

## Decision

1. **Providers.** A `PriceProvider` Protocol (`daily_bars(session)`,
   `corporate_actions(since)`, `last_trade(symbols)`) with two implementations:
   - `MassiveProvider` (Massive Stocks Basic, free): one grouped-daily call per session,
     `adjusted=false&include_otc=false`; splits `/stocks/v1/splits`; dividends
     `/stocks/v1/dividends`. Basic has no real-time trades, so `last_trade` always raises.
   - `AlpacaProvider` (Alpaca Basic, free): latest trades on `iex` or `delayed_sip`, raw daily
     bars (`adjustment=raw`) as end-of-day failover, corporate actions.
2. **Exceptions.** `ProviderError` (with a `code`), and its subclasses `StaleDataError`,
   `IncompleteBatchError`, `RouterExhaustedError`. Nothing is swallowed. The one
   catch-and-convert is `decision.price_for_suggestion`, whose documented output on failure
   is `NoSuggestion(reason, error_class)`, never a stale price.
3. **Router.** Primary, then fallback, then `RouterExhaustedError` carrying both causes. A
   non-`ProviderError` (a bug) propagates and never triggers failover.
4. **Calendar.** Alpaca `/v2/calendar` behind a `SessionCalendar` shape (`is_session`,
   `previous_sessions`). `near_close_dca.nyse_holidays()` is off limits here (the package
   boundary test forbids importing `near_close_dca`), and being computed it would miss
   one-off closures such as 2025-01-09. A calendar outage is an error, never "closed".
5. **Storage: DuckDB, append-only, point in time.** Tables `daily_bars_raw`,
   `corporate_actions`, `ingestion_runs` (an event log), `intraday_quotes`, plus
   `yahoo_legacy_bars` and `reconciliation_results` (operator answer Q5). Every table carries
   `source, as_of, ingested_at, run_id`. Primary keys include a `content_hash` of the
   values, so re-ingesting identical data is a no-op (`ON CONFLICT DO NOTHING`) and a vendor
   correction lands as a new row with a later `as_of`. No `UPDATE` or `DELETE` exists.
   Each batch is staged to Parquet and inserted from that file in one transaction.
6. **Raw only on disk; adjusted values in table macros.** `bars_known_at(known_at)`,
   `actions_known_at(known_at)` and `adjusted_bars(as_of_date, known_at)` are DuckDB table
   macros stored in the database file. A split or dividend applies to a bar only when
   `bar.session < ex_date <= as_of_date`, which is the no-lookahead rule. `known_at` hides
   rows we learned about later. Massive's `historical_adjustment_factor` is stored as
   delivered but never used, because it is relative to "now", not to an as-of date.
7. **Yahoo legacy is its own table.** Yahoo `Close` is split-adjusted as of download, so in
   `daily_bars_raw` the adjusted macro would adjust it twice.
8. **Reconciliation and splice.** Massive raw closes, split-adjusted to the Yahoo file's last
   session, against Yahoo `Close` over the last min(250, overlap) sessions (under 60 is
   `insufficient`). Per session `<= 0.1%` pass, `0.1-1%` flag, `> 1%` block; the symbol takes
   its worst session. Volume off by more than 10% sets `volume_flag` and never blocks alone.
   `spliced_adjusted_series` refuses unless the latest reconciliation passed and ended
   exactly at the splice date.

## Dependencies

`requests==2.34.2` and `duckdb==1.5.6` (released 2026-09-28), declared in
`dca-sizing-signals/requirements.txt` and as the package's `prices` extra. `duckdb` is new
to this repository (`all_stock_dca/backtest.py` writes a `.duckdb`-named file with
`sqlite3`). Neither touches the stdlib-only core. Parquet is written by DuckDB itself, so
there is no `pyarrow`. Timestamps are stored as naive-UTC `TIMESTAMP`, because reading
`TIMESTAMPTZ` back into Python requires `pytz`; the store converts at its boundary.

## Consequences

- Nightly ingest is about 8 Massive calls; the free tier's 5 calls/minute makes that roughly
  two minutes. A two-year backfill is about 500 calls, roughly 100 minutes.
- Re-ingesting a value that flips A, B, A conflicts with the first A row and is dropped, so
  "latest" shows B. This is a known limitation; the run log shows each fetch.
- Two cash dividends on the same symbol and ex-date from one vendor with no vendor id
  collapse to the latest. Massive supplies ids, so this matters only for id-less rows.
- The Yahoo daily updater keeps running and no consumer of `ticker_data/` is repointed.
  Cutover is a separate decision once reconciliation has run on the real archive.
- CI: `dca-sizing-signals` has no job and AGENTS.md bars agents from editing
  `.github/workflows/`. The job and the pip-audit manifest entry are proposed, not made.

## Rollback

Revert the PR. Delete `data/backfill.duckdb` and `data/staging/`. Nothing else reads them.
