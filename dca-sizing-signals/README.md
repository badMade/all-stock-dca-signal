# dca-sizing-signals

Independent sizing-suggestion producers for the graded DCA strategy, each optionally
publishing a bounded per-ticker multiplier adjustment that `near_close_dca.graded_buy_multiplier()`
may read, and none of them ever able to write to live sizing directly. A sibling of
`ml-forecaster/`, `wondrous-octopus/`, and `ticker-advisor/`: a self-contained subproject
with its own `pyproject.toml` and `tests/`, following the same conventions.

## What this is

Four packages that only depend on each other in one direction, enforced by both the
type system and a CI test, not by convention alone:

| Package | Role |
| --- | --- |
| `dca_shared_interfaces/` | The neutral contracts (`ForecastProvider`, `SizingSuggester`, `SizingSuggestion`) and the reusable fail-closed guarded-publish base class. Depends on nothing else in this repo. |
| `connector_signals/` | A concrete `SizingSuggester` sourced from Robinhood-connector-obtained market data. Implements `dca_shared_interfaces.forecasting.SizingSuggester`. |
| `comovement_signals/` | Research-only co-movement signals 1 (cluster concentration cap) and 2 (correlated selloff detector) from `docs/adr-comovement-signals.md`. Pure stdlib functions over caller-supplied close rows; two `SizingSuggester` wrappers; publishes only its own two files. Signals 3 to 5 are shadow/log-only functions (not suggesters) that append to their own JSON-lines files. Not wired into any live path. |
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
  yet. Their tests (`tests/pending/test_edgar_fundamentals.py`,
  `tests/pending/test_dolthub_options.py`) are written and currently fail on import, per this
  repo's TDD-first policy. `tests/pending/conftest.py` keeps pytest from collecting them, so
  the CI job stays green; move each back to `tests/` once its module passes. Built from
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

## Co-movement signals (research only)

`comovement_signals/` implements signals 1 and 2 of `docs/adr-comovement-signals.md`
(operator decision 2026-11-01: pairs and co-movement as signals, not a pairs strategy).

| Step | Function | Default |
|---|---|---|
| Returns | `returns_matrix(close_rows, as_of)`: daily log returns. Rows after `as_of`, rows on `as_of` not marked `final: True` (a forming intraday bar), and `interpolated` rows are ignored; a date counts as a session only when half the listed tickers have a close on it | 252-session window; fewer than 120 returns = singleton |
| Clusters | `build_clusters(matrix)`: average linkage on 1 - correlation, ties broken by ticker | cut 0.5; pairs need 120 shared sessions |
| Signal 1 | `cluster_cap_suggestions(...)`: scale an over-cap cluster (2+ members) to the cap | 35% of the day's budget; multiplier floor 0.25 |
| Signal 2 | `correlated_selloff(...)`: mark clusters with 60% of 3+ scored members down 2% or more; their escalations share one budget equal to the largest member's; plus a universe-wide flag | 60%, -2%, 3 members |

`ClusterCapSuggester` and `CorrelatedSelloffSuggester` take a `load_inputs(asof)` callable
and fail closed: any missing, stale (newest close more than 4 days old, day returns not
for `asof`) or malformed input, or a failed clustering check, gives `ok=False` with a
reason and publishes nothing. `publish_outcome()` writes only
`comovement_cluster_cap_suggestions.json` or `comovement_correlated_selloff_suggestions.json`
to a directory the caller picks; the publisher refuses any other file name. Multipliers
are bounded to [0.25, 1.0]. These rules are not yet scored by the Phase 0 ledger, so each
`ComovementSuggestion` has `scored=False` and an `eval_metric_mae` of `+inf` (it can never
win an MAE comparison, and the shared MAE-gated publisher refuses it), and the files record
`"mae": null, "scored": false`. `tests/comovement/test_real_fixture.py` checks the
pipeline against a small real fixture of split-adjusted Robinhood daily closes.

```bash
python -m pytest tests/comovement -q
```

### Signals 3 to 5 (shadow and log only)

These are not `SizingSuggester`s and cannot influence sizing. Each returns a `ShadowResult`
(records plus a reason for every pair or day it refused), and `ShadowWriter(directory)`
appends records to that signal's own file. The writer refuses any other file name
(including `ml_sizing_adjustment.json` and the two `*_suggestions.json` files), any
directory with a `config` path component, and any record whose `inputs_known_at` is after
its `as_of`. Each line has `as_of`, `inputs_known_at`, `signal`, `payload`, `live_effect: "none"`
and a SHA-256 `content_hash`; an identical line is not appended twice.

| Signal | Function | Default | Shadow file |
|---|---|---|---|
| 3 Pair breakdown | `pair_breakdown_shadow(close_rows, as_of)`: pairs with 2-year correlation >= 0.70; flag when the 60-session rolling correlation is strictly below the pair's 2-year 10th percentile (of the earlier rolling values) for 5 sessions; suggests `hold_1x` for both | 504-session lookback, 252 shared returns, 100 reference values | `comovement_pair_breakdown_shadow.jsonl` |
| 4 Cheaper-leg tilt (speculative) | `cheaper_leg_tilt_shadow(close_rows, as_of, pairs=None, pair_dollars=None)`: pairs with formation correlation >= 0.75; OLS of log A on log B over the 252 closes before the evaluation close only; weight A = clip(0.5 - 0.15 z, 0.2, 0.8) | leg A = alphabetically first | `comovement_tilt_shadow.jsonl` |
| 5 Rotation log | `rotation_log_shadow(close_rows, as_of, staples, semis)`: mean log return of the caller's staples group minus the semis group, plus its 20-session rolling mean | every group member must have a return | `comovement_rotation_log.jsonl` |

```python
from comovement_signals import ShadowWriter, pair_breakdown_shadow, write_shadow_result
result = pair_breakdown_shadow(close_rows, as_of)
write_shadow_result(result, ShadowWriter(Path("research/shadow")))  # a failed result writes nothing
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

Dependencies: `requests==2.34.2` and `duckdb==2.0.0`. Nothing else, and nothing in the
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

### Cash-secured put shadow log (research only, never trades)

`market_data_ingest/ledger/csp_shadow.py` implements the **proposed** ADR
`docs/adr-csp-shadow-log.md` (PR #2103). It records "would sell this put at the bid" rows and
settles them later. It cannot trade: it names no broker order tool, makes no network call, and
`tests/ledger/test_csp_shadow_no_orders.py` fails the build if that changes. It keeps its own
append-only tables (`csp_shadow_entries`, `csp_shadow_settlements`) via
`ensure_csp_schema(con)`, under the same rules as the ledger.

| Function | Does |
|---|---|
| `select_candidates(...)` | At most one put per active ticker per session: 25-45 days, delta -0.30 to -0.15, spread <= 10% of mid, strike <= ladder buy price, collateral <= 10% of the account per contract and <= 25% in total, none in a sector above 30%, none in a tax-loss pause. Stale, future or non-finite data drops the row with a reason |
| `record_shadow_entries(con, ...)` | Entries at the **bid** (`entry_price = bid` is also a schema check) |
| `settle(...)` | OTM expires (premium kept); ITM assigned at strike (cost = strike - premium); early close at the **ask**; unknown price = `unscored`; ex-dividend and deep-ITM flags |
| `scorecard(...)` | Return on locked collateral vs the 3.60% sweep and plain DCA, worst loss vs 12 months of premium, early-assignment risk counts, pass bar (never before 6 months and 20 settled trades, never with an expired trade still unscored; only trades settled by `as_of` count) |
| `open_short_puts(con, as_of)`, `tax_loss_blocked_tickers` | Open shadow puts, for the tax-loss eligibility check |

A scored settlement is never superseded by a later unscored one. Sector exposure counts
holdings, open shadow puts and earlier picks in the same run; a sector missing from the
holdings map, or `paused_tickers=None` (pause list unknown), yields no candidates. The
thresholds are proposed defaults in `CspShadowConfig`, not operator-approved.

## Status

Architecture and boundary tests are real and passing. `edgar_fundamentals.py` and
`dolthub_options.py` are test-first stubs awaiting implementation approval -- see "What
this does NOT do." Built across a chat session with no repo access; packaged for handoff
to Claude Code for actual integration into `all-stock-dca` and any git operations, since
that environment has the credentials and repo access this one didn't.
