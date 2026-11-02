# dca-sizing-signals

Independent sizing-suggestion producers for the graded DCA strategy, each optionally
publishing a bounded per-ticker multiplier adjustment that `near_close_dca.graded_buy_multiplier()`
may read, and none of them ever able to write to live sizing directly. A sibling of
`ml-forecaster/`, `wondrous-octopus/`, and `ticker-advisor/`: a self-contained subproject
with its own `pyproject.toml` and `tests/`, following the same conventions.

## What this is

Three packages that only depend on each other in one direction, enforced by both the
type system and a CI test, not by convention alone:

| Package | Role |
| --- | --- |
| `dca_shared_interfaces/` | The neutral contracts (`ForecastProvider`, `SizingSuggester`, `SizingSuggestion`) and the reusable fail-closed guarded-publish base class. Depends on nothing else in this repo. |
| `connector_signals/` | A concrete `SizingSuggester` sourced from Robinhood-connector-obtained market data. Implements `dca_shared_interfaces.forecasting.SizingSuggester`. |
| `market_data_ingest/` | The one place in this repo allowed to make network calls: fetches fundamentals, options/IV, index, and earnings data from external sources and writes versioned, point-in-time-stamped local files. `ml_forecaster/` and `wondrous_octopus/` never import this -- only `connector_signals/` does. |

## What this does NOT do

- Does not write to live DCA sizing. `SizingSuggester.suggest()` returns an immutable
  `SizingSuggestion`; nothing in this package holds a reference to `near_close_dca` or any
  live engine state. Enforced by `tests/test_architecture_boundaries.py`, which fails if
  any package here imports `near_close_dca`, `ml_forecaster`, or `wondrous_octopus`.
- Does not import `ml_forecaster/` or `wondrous_octopus/`, and is not imported by them.
  `wondrous_octopus/` may optionally implement `ForecastProvider` from
  `dca_shared_interfaces` for future ensembling; it deliberately does not implement
  `SizingSuggester`, matching its own README's "does not influence live DCA sizing."
- `edgar_fundamentals.py` and `dolthub_options.py` under `market_data_ingest/` do not exist
  yet. Their tests (`tests/test_edgar_fundamentals.py`, `tests/test_dolthub_options.py`) are
  written and currently fail on import, per this repo's TDD-first policy -- built from
  live-verified fixtures (SEC EDGAR CIK0000320193 companyfacts, DoltHub
  post-no-preference/options), not fabricated. Implementation is pending explicit
  approval; do not write it without that.
- Does not integrate SimFin or Sharadar. Both require a registered API key this build
  environment did not have; SEC EDGAR alone already covers free, unregistered,
  point-in-time fundamentals history, so they're deferred, not abandoned.

## Where sizing_adjustment_reader.py goes

Shipped alongside this package as `core-repo-additions/live_dca_side/sizing_adjustment_reader.py`,
but deliberately NOT part of it. It belongs next to `near_close_dca.py` in the core repo,
since it's the one module allowed to know more than one `SizingSuggester` exists --
combining their independent output files is its job, not either suggester's.

## Install

```bash
cd dca-sizing-signals
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/
```

## Price source (Massive + Alpaca)

`market_data_ingest/prices/` replaces Yahoo Finance as the ongoing source of daily prices.
Massive (ex-Polygon) Stocks Basic is the primary nightly source. Alpaca Basic is the
end-of-day failover and the intraday last-trade source. The Yahoo history already in
`ticker_data/` stays, imported as `source='yahoo_legacy'`. Decision record:
`docs/adr-price-source-massive-alpaca.md`. It is suggestion-only: nothing in the live DCA
paths imports it, and the ladder and sizing logic are unchanged.

### Install

```bash
cd dca-sizing-signals
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # requests, duckdb, pytest (pinned); or: pip install -e ".[prices,dev]"
python -m pytest tests/prices            # offline; the live PoC under tests/prices/contract skips itself
```

Dependencies: `requests==2.34.2` and `duckdb==1.5.6`. Nothing else, and nothing in the
stdlib-only core.

### Environment variables

| Variable | Needed for | Notes |
|---|---|---|
| `MASSIVE_API_KEY` | daily job, PoC | Sent as an `Authorization: Bearer` header, never in a URL |
| `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | daily job (calendar, failover), intraday, PoC | Sent as `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers. The calendar uses the paper trading API |
| `DCA_PRICE_DB` | optional | DuckDB path. Default `<repo>/data/backfill.duckdb` (gitignored, as is `data/staging/`) |
| `DCA_TICKER_HISTORY_DIR` | `yahoo_legacy` import, PoC | The existing Yahoo CSV archive |
| `PRICE_CONTRACT=1` | PoC only | Opts in to the live contract test |

Keys are read from the environment only. They are never logged and never written to the
database or the staging files; `tests/prices/test_no_secrets.py` checks this.

### Usage

```bash
# Nightly, after 20:00 ET. Exit code 0 means ok or market closed; non-zero means the run failed.
python -m market_data_ingest.prices.daily_job
python -m market_data_ingest.prices.daily_job --session 2026-10-16
# First runs: fewer than 20 stored sessions means no row-count baseline yet.
python -m market_data_ingest.prices.daily_job --session 2024-10-21 --bootstrap        # floor 8000 rows
```

```python
from datetime import date, datetime, timezone
from pathlib import Path
from market_data_ingest.prices.store import PriceStore
from market_data_ingest.prices.legacy_yahoo import import_dir
from market_data_ingest.prices.reconcile import reconcile_symbol

now = datetime.now(timezone.utc)
with PriceStore("data/backfill.duckdb") as store:
    store.adjusted_bars("NVDA", as_of_date=date(2024, 6, 1))  # no lookahead: the 2024-06-10 split is not applied
    report = import_dir(store, Path("ticker_data"), run_id="yahoo-import", now=now)  # report.failed lists bad files
    reconcile_symbol(store, "NVDA", run_id="recon", now=now).status                  # pass / flag / block / insufficient
# Intraday: market_data_ingest.prices.decision.price_for_suggestion(symbol, slot="10:30", ...)
# returns PriceOK (already recorded in intraday_quotes) or NoSuggestion(reason, error_class).
```

The adjusted views are DuckDB table macros stored in the database file, so the DuckDB CLI
works too: `SELECT * FROM adjusted_bars(DATE '2024-06-01', TIMESTAMP '9999-12-31') WHERE symbol = 'NVDA'`.

### Troubleshooting

| Symptom (run record `error_class` / `code`) | Meaning and fix |
|---|---|
| `IncompleteBatchError` / `no_baseline` | Fewer than 20 stored Massive sessions. Run oldest-first with `--bootstrap` until 20 exist |
| `IncompleteBatchError` / `below_median` | Massive returned under 90% of the usual row count. The fallback was tried; rerun later. The job re-pulls T-1..T-5 every night |
| `IncompleteBatchError` / `empty_open_session` | No bars on a day the calendar says was open. Usually the job ran too early; rerun after 20:00 ET |
| `ProviderError` / `not_authorized` | Massive free history is 2 years. Older sessions are refused by design; use `yahoo_legacy` for them |
| `ProviderError` / `rate_limited` | Two 429s in a row. The client already spaces Massive calls 12 s apart (5/minute); check for another process sharing the key |
| `ValidationError` | One row failed the OHLC or price checks, so the whole batch was rejected (strict by design). The message names the rows |
| `SessionNotClosed` | The session is not final before 20:00 ET |
| `RouterExhaustedError` | Massive and Alpaca both failed. Both causes are in `ingestion_runs.error_detail` |
| `SpliceNotValidatedError` | Yahoo and vendor history are joined only after a passing reconciliation that ends at the splice date |
| DuckDB "Could not set lock" | Another process has the database open. DuckDB allows one writer |

## Outcome ledger and universe tables (Phase 0, research only)

`market_data_ingest/ledger/` implements the **proposed** ADR
`docs/adr-outcome-ledger-pit-harness.md` (PR #2096) on the same DuckDB file as the price
store. It is not wired into live sizing, makes no network or broker calls, and nothing in the
live DCA paths imports it. Callers pass already-fetched plain dicts to `LedgerStore.ingest_*`.

| Table | Holds |
|---|---|
| `market_sessions` | The trading calendar that horizons count in |
| `fills`, `baseline_fills` | Live fills (`run_kind` = `scheduled` / `manual`) and the 1x counterfactual per slot decision |
| `outcomes` | Price at t+N sessions per fill and baseline row, `scored` or `unscored` (never zero) |
| `accounts`, `tax_lots` | Accounts by salted alias only; lot snapshots with a hashed `lot_key` |
| `trials` | Every evaluated configuration with its metrics and the recorded promotion threshold |
| `universe_scores`, `universe_proposals`, `universe_membership` | Factor scores, membership proposals, dated `[effective_from, effective_to)` membership |

Same rules as the price store: append-only, `content_hash` keys, `ON CONFLICT DO NOTHING`,
`source, as_of, ingested_at, run_id` on every row, latest version wins at `known_at`. Table
macros (`outcome_candidates`, `outcomes_known_at`, `scheduled_outcomes`, `trial_counts`,
`universe_members_as_of`, ...) live in the database file. Outcomes use an `ASOF LEFT JOIN` on
`quote_date <= horizon_session`, so no later quote can leak in, and the quote must be the
horizon session's own bar (`OUTCOME_QUOTE_TOLERANCE_SESSIONS = 0`; an older one is
`stale_quote`). A fill whose session is missing from `market_sessions` is
`session_not_in_calendar`. `record_outcomes` refuses `as_of` earlier than `known_at`, and the
reader prefers the row built from the newest `inputs_known_at`. `market_sessions` is
append-only with no retraction row yet, so a session wrongly ingested as open stays counted.
`scheduled_outcomes` drops manual runs and every manual order id (`docs/run_classification.md`).

The answers to the ADR's open questions are **proposed defaults, not operator-approved**, kept
as constants in `market_data_ingest/ledger/defaults.py`: location here; alias =
HMAC-SHA256 of the raw identifier with the salt from `$DCA_LEDGER_ACCOUNT_SALT` (required, never
stored); baseline = flat 1x at the same slot timestamp and price; horizons t+5 and t+20
sessions; promotion threshold 0.95 (field only, no promotion logic).

```python
from market_data_ingest.ledger.accounts import account_alias
from market_data_ingest.ledger.store import LedgerStore

with LedgerStore("data/backfill.duckdb") as ledger:
    ledger.ingest_fills(rows, source="run_artifact", run_id="ingest-1", as_of=now)
    ledger.record_outcomes(known_at=now, run_id="score-1", as_of=now)
    ledger.scheduled_outcome_summary(known_at=now)
    ledger.ingest_accounts([{"account_alias": account_alias(raw), "account_kind": "agentic"}],
                           source="broker_accounts", run_id="ingest-1", as_of=now)
    ledger.members_as_of(date(2026, 4, 1))
```

Tests: `python -m pytest tests/ledger` (offline, synthetic hand-computed fixtures).

## Status

Architecture and boundary tests are real and passing. `edgar_fundamentals.py` and
`dolthub_options.py` are test-first stubs awaiting implementation approval -- see "What
this does NOT do." Built across a chat session with no repo access; packaged for handoff
to Claude Code for actual integration into `all-stock-dca` and any git operations, since
that environment has the credentials and repo access this one didn't.
