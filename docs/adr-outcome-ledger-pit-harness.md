# ADR: Outcome ledger, tax-lot ledger and point-in-time harness on DuckDB (Phase 0)

- **Status:** Proposed (2026-11-01). Open questions below must be answered before any code.
- **Scope:** research and reporting only. The stdlib-only core, the ladder, `config/`, and all
  three entry points (`run_dca.run_slot()`, the `MorningDCAExecutor` hierarchy,
  `dca_runner.py`) are untouched. Nothing here writes live sizing or places orders.
- **Plan of record:** Project doc `claude/strategy-roadmap.md`, Phase 0 (items: outcome ledger,
  #17 tax-lot ledger, #18 point-in-time harness, #12 trial registry, #8 weekly counterfactual
  review). Source research: "Self improving DCA strategy options" report, 2026-11-01.

## Context

Every later refinement (budget pacing, inverse-vol suggester, quality gate, meta-labeling)
needs a score, and the system has none today:

- Notion records spend (about 3,590 buy orders, 2026-06-30 to 2026-10-30) but no P&L, and the
  dashboard's graded-vs-fixed, cost-basis and sector tables still read "TBD".
- Filled shares appear to move from the agentic account into the main individual account, so
  any metric that reads only agentic positions sees the latest fills, not true cost basis.
- No forecast accuracy, realized P&L or cost-basis-vs-later-price results are committed.
- Backtests on the current, hand-picked universe are survivor-biased.

`dca-sizing-signals/market_data_ingest/prices/store.py` already holds an append-only,
point-in-time DuckDB store (`daily_bars_raw`, `corporate_actions`, `bars_known_at`,
`actions_known_at`, `adjusted_bars`; see `docs/adr-price-source-massive-alpaca.md`).
`duckdb` is already pinned in `dca-sizing-signals/requirements.txt`. This ADR extends that
store; it does not add a dependency or a second engine.

## Decision (proposed)

1. **Engine: DuckDB, in the research tree only.** Chosen over Polars (no persistent store),
   SQLite (row store, slow for analytics), PostgreSQL/TimescaleDB, ClickHouse and QuestDB
   (server or second engine with no gain at this data size). Never imported by a core module.
2. **Same rules as the price store.** Append-only, no `UPDATE` or `DELETE`. Every row carries
   `source, as_of, ingested_at, run_id`. Re-ingesting identical data is a no-op via a
   `content_hash` key.
3. **New tables.**
   - `fills`: every scheduled live fill, read from run artifacts and broker order history.
     Manual runs and manual order IDs are tagged and excluded, per
     `docs/run_classification.md`.
   - `baseline_fills`: for each slot decision, the counterfactual baseline buy at the same
     timestamp and price (definition is open question 4).
   - `outcomes`: per fill and per baseline row, the price at fixed horizons (open question 5),
     joined with `ASOF JOIN` on `quote_date` so no later data leaks in.
   - `accounts`: one row per Robinhood account, keyed by an operator-chosen alias or a salted
     hash. No account-shaped literal is ever stored or committed (open question 3).
   - `tax_lots`: read-only snapshots of lots across all Robinhood accounts, so lots that move
     from the agentic account to the main account stay traceable. Flags the 31-day buy pause
     any future loss sale would need (wash-sale rule, IRS Publication 550).
   - `trials`: every configuration ever evaluated, with its metrics, so promotion can apply a
     deflated Sharpe bar and the trial count is never hidden.
4. **Read-only broker access.** Ingest uses only read tools (`get_accounts`,
   `get_equity_positions`, `get_equity_tax_lots`, `get_equity_orders`), injected as plain
   callables in the same pattern as `dca_live_adapters.py`. No order, cancel, review or
   watchlist tool is ever wired in.
5. **Point-in-time harness.** Backtests run as table macros over `adjusted_bars(as_of_date,
   known_at)` and EDGAR facts filtered on `filed_date`, never period end. Universe membership
   comes from a dated membership table (open question 1); until that exists every backtest
   result is labelled survivor-biased.
6. **Outputs are suggestions only.** The weekly counterfactual review (#8) reads DuckDB and
   publishes a short summary plus proposed config diffs to the Notion dashboard and/or a draft
   PR (open question 6). DuckDB is the source of truth; Notion is a read-only copy. Nothing
   in this ADR changes `config/` automatically.
7. **Fail closed.** A missing or incomplete ingest marks that day's scores `unscored`, never
   zero, and never blocks or alters a live slot.

## Open questions

1. Historical index-membership source, with delisted price data, for survivorship-free
   backtests.
2. Location: extend `dca-sizing-signals/` (shares the price store and its boundary test), or a
   new sibling subproject with its own `requirements.txt`.
3. Account alias scheme: operator-chosen labels, or a salted hash with the salt kept outside
   the repo.
4. Baseline definition: a flat 1x buy at the same slot, or the plain no-signal schedule.
5. Outcome horizons (proposed t+5 and t+20 sessions) and the promotion threshold.
6. Where the Friday review publishes: Notion page, draft PR, or both.

## Consequences

- Phase 1 to 4 items become measurable against a common baseline instead of anecdote.
- Tax-lot reads cover every Robinhood account, so the ingest sees more account data than the
  live executor does. It stays read-only and alias-keyed.
- Adds tables to a store that is itself still Proposed; if the price-source ADR changes
  location or schema, this ADR follows it.
- CI: `dca-sizing-signals` has no CI job, and agents may not edit `.github/workflows/`. Tests
  run through `scripts/local_ci.sh` until the operator adds a job.

## Verification

- A test proves `outcomes` never reads a quote dated after the fill's horizon (lookahead).
- A test proves no account-shaped literal can reach a table or a committed file.
- A test proves manual-run fills never enter scheduled metrics.
- Timing check: 91 tickers x 10 years of daily bars joined to EDGAR facts by `filed_date`
  completes in under a second on the Mac Studio.

## Rollback

Revert the PR. Delete the new tables or the DuckDB file they live in. Nothing in the live path
reads them.
