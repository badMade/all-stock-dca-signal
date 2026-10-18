# PLAN: Massive + Alpaca price source (replaces Yahoo going forward)

Status: DRAFT, awaiting operator approval (gate A). Companion: `VTO-price-source.md`.
Process: plan approval (A) -> tests only (B, approval) -> implement -> PoC -> commit/PR approval (C).

## 0. Ground truth found in the repo (read before reviewing)

| Brief says | Repo reality | Consequence |
|---|---|---|
| Backfill ADR at `claude/architecture.md` | Does not exist in the repo, its history, or anywhere on this Mac | Write a new ADR; cannot align with an ADR nobody can read |
| `data/backfill.duckdb`, Parquet staging | No `data/` dir, no `.duckdb` from an ingest, `duckdb` not installed; only DuckDB mention is `all_stock_dca/backtest.py`, which writes a `.duckdb`-named file with **sqlite3** | DuckDB is a **new** dependency to this repo, not an existing one |
| "existing data/ingestion layer" | `dca-sizing-signals/market_data_ingest/` is documented as "the one place in this repo allowed to make network calls"; it holds only an unimplemented `snapshot.py` contract | New code goes there, as `market_data_ingest/prices/` |
| Yahoo history as `source='yahoo_legacy'` | Lives as CSVs in `ticker_data/` (Drive symlink, `DCA_TICKER_HISTORY_DIR`), columns `Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits`. `Close` is **split-adjusted** as of download, `Adj Close` is split+dividend adjusted | Yahoo rows are not raw, so they must not go in `daily_bars_raw` (see §3) |
| Core: "requests only" | Core runtime is stdlib-only (`dependencies = []`). `dca-sizing-signals` also has `dependencies = []` | Deps go in a new `dca-sizing-signals/requirements.txt` + an optional `prices` extra, never the core |
| CI | `dca-sizing-signals` has no CI job. AGENTS.md: agents must never edit `.github/workflows/` | CI job + pip-audit manifest entry are **proposed** in the PR body, not committed |
| Calendar | `near_close_dca.nyse_holidays()` exists, but `market_data_ingest` is forbidden from importing `near_close_dca` (`tests/test_architecture_boundaries.py`), and it is computed, so it misses ad-hoc closures (e.g. 2025-01-09, inside the 2-year window) | Use Alpaca's `/v2/calendar` behind an injected `SessionCalendar` Protocol |
| API keys | `MASSIVE_API_KEY`, `ALPACA_*` are unset in this shell and absent from `.env` | Fixtures cannot be live-recorded now (see §6) |

## 1. ADR need: yes

`docs/adr-price-source-massive-alpaca.md` (written first, before any code). Decides: the
`PriceProvider` Protocol and router (new abstraction), DuckDB + Parquet as a new dependency
scoped to `dca-sizing-signals`, the append-only point-in-time schema, Alpaca as calendar
authority, the separate `yahoo_legacy_bars` table, and the splice rule. Status: Proposed.

## 2. Files

All new, under `dca-sizing-signals/` unless noted. Nothing in the core, no live entry point.

```
market_data_ingest/prices/
  __init__.py
  errors.py        ProviderError; StaleDataError and IncompleteBatchError subclass it, so the router
                   catches one base; RouterExhaustedError, SpliceNotValidatedError
  models.py        frozen dataclasses: DailyBar, CorporateAction, Quote, PriceOK, NoSuggestion
  provider.py      PriceProvider Protocol: daily_bars(session), corporate_actions(since), last_trade(symbols)
  calendar.py      SessionCalendar Protocol + AlpacaCalendar (/v2/calendar), previous_sessions(d, n)
  http.py          thin requests wrapper: fixed timeouts, header auth only, status->exception map,
                   strict JSON (dup keys / NaN rejected), injected clock+sleep rate limiter, key redaction
  massive.py       MassiveProvider (grouped daily, /stocks/v1/splits, dividends); last_trade raises ProviderError
  alpaca.py        AlpacaProvider (latest trades iex|delayed_sip, daily bars adjustment=raw, corporate actions)
  router.py        PriceRouter: primary -> fallback -> raise RouterExhaustedError (chains both causes)
  validate.py      schema, OHLC sanity, no negative/zero prices, no negative volume
  store.py         DuckDB DDL, Parquet staging, INSERT ... ON CONFLICT DO NOTHING, views/macros
  daily_job.py     python -m market_data_ingest.prices.daily_job  (idempotent nightly job)
  intraday.py      slot policy (max_age, delayed rule), persists every used quote
  decision.py      price_for_suggestion(symbol, slot, now) -> PriceOK | NoSuggestion(reason)
  legacy_yahoo.py  one-shot import of ticker_data/*.csv into yahoo_legacy_bars
  reconcile.py     Massive vs yahoo_legacy tiers; splice guard
tests/prices/
  fixtures/*.json  (§6)
  test_massive_provider.py  test_alpaca_provider.py  test_http_fail_closed.py
  test_router.py  test_store_point_in_time.py  test_daily_job.py  test_intraday.py
  test_decision.py  test_reconcile.py  test_legacy_yahoo.py  test_no_secrets.py
  contract/test_live_poc.py (opt-in: PRICE_CONTRACT=1 + keys; skipped otherwise)
requirements.txt   requests==<pin>, duckdb==<pin>, pytest  (independent manifest, like its siblings)
pyproject.toml     add optional extra `prices`; pytest marker `contract`
README.md          new "Price source (Massive + Alpaca)" section
docs/adr-price-source-massive-alpaca.md   (repo root docs/)
docs/price_source_poc_report.md           (repo root docs/, written from the PoC run)
.gitignore         (repo root) data/*.duckdb, data/staging/
```

## 3. Schema (DuckDB, append-only, point-in-time)

Every data table carries `source, as_of, ingested_at, run_id`. `as_of` = when the value
became known to us (first observation); `ingested_at` = write time of this row. No
`UPDATE`/`DELETE` anywhere; corrections are new rows.

```sql
daily_bars_raw(
  source VARCHAR NOT NULL CHECK (source IN ('massive','alpaca')),
  symbol VARCHAR NOT NULL, session DATE NOT NULL,
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE NOT NULL, volume DOUBLE NOT NULL,
  vwap DOUBLE, trades BIGINT,
  content_hash VARCHAR NOT NULL,          -- sha256 of canonical OHLCV values
  as_of TIMESTAMPTZ NOT NULL, ingested_at TIMESTAMPTZ NOT NULL, run_id VARCHAR NOT NULL,
  PRIMARY KEY (source, symbol, session, content_hash))
```
Re-pulling identical data conflicts on the key and is dropped (idempotent). A vendor
correction has a new hash, so it lands as a new row with a later `as_of`.

```sql
corporate_actions(
  source VARCHAR NOT NULL, vendor_id VARCHAR,
  action_type VARCHAR NOT NULL CHECK (action_type IN
      ('forward_split','reverse_split','stock_dividend','cash_dividend')),
  symbol VARCHAR NOT NULL, ex_date DATE NOT NULL,
  split_from DOUBLE, split_to DOUBLE,     -- splits and stock dividends
  cash_amount DOUBLE, currency VARCHAR,   -- cash dividends
  declaration_date DATE, record_date DATE, pay_date DATE,
  historical_adjustment_factor DOUBLE,    -- stored as delivered, NOT used for PIT math (it is relative to "now")
  content_hash VARCHAR NOT NULL, as_of TIMESTAMPTZ NOT NULL, ingested_at TIMESTAMPTZ NOT NULL, run_id VARCHAR NOT NULL,
  PRIMARY KEY (source, symbol, action_type, ex_date, content_hash))

ingestion_runs(           -- append-only event log; latest status per run_id via a view
  run_id VARCHAR NOT NULL, job VARCHAR NOT NULL, session DATE,
  status VARCHAR NOT NULL CHECK (status IN ('running','ok','failed','skipped_closed')),
  source VARCHAR, row_count INTEGER, error_class VARCHAR, error_detail VARCHAR,  -- detail is redacted
  as_of TIMESTAMPTZ NOT NULL, ingested_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (run_id, status))

intraday_quotes(
  symbol VARCHAR NOT NULL, ts TIMESTAMPTZ NOT NULL,   -- trade timestamp from the feed
  price DOUBLE NOT NULL, feed VARCHAR NOT NULL CHECK (feed IN ('iex','delayed_sip')),
  delayed BOOLEAN NOT NULL, slot VARCHAR NOT NULL,
  source VARCHAR NOT NULL DEFAULT 'alpaca',
  as_of TIMESTAMPTZ NOT NULL,                          -- fetch time
  ingested_at TIMESTAMPTZ NOT NULL, run_id VARCHAR NOT NULL,
  PRIMARY KEY (run_id, symbol))
```

**Two tables beyond the brief's four** (both need approval):
- `yahoo_legacy_bars(source='yahoo_legacy', symbol, session, open..close, adj_close, volume,
  dividends, stock_splits, adjustment_basis='split_adjusted_at_download', file_sha256, as_of,
  ingested_at, run_id)`. Separate because Yahoo `Close` is already split-adjusted; in
  `daily_bars_raw` the adjusted view would adjust it twice.
- `reconciliation_results(source='massive', reference_source='yahoo_legacy', symbol, window_start, window_end, sessions, close_tier,
  worst_close_diff_pct, volume_flag, adj_close_worst_diff_pct, splice_date, splice_factor,
  status IN ('pass','flag','block','insufficient'), as_of, ingested_at, run_id)`.

**Views / table macros** (adjusted values only ever exist here):
- `bars_known_at(known_at)`: per (symbol, session), rows with `as_of <= known_at`; prefer
  `massive` over `alpaca`, then latest `as_of`.
- `adjusted_bars(as_of_date, known_at)`: raw close x product of split ratios for splits
  with `session < ex_date <= as_of_date`, x product of `(1 - cash/prev_raw_close)` for cash
  dividends in the same range. Stock dividends act as splits. Events with
  `ex_date > as_of_date` never apply (the no-lookahead rule).
- `ingestion_runs_latest`.

NVDA check the tests pin: 10:1 split ex-date 2024-06-10. `adjusted_bars('2024-06-01')`
returns the 2024-05-31 raw close unadjusted; `adjusted_bars(today)` returns it / 10.

## 4. Behaviour

**MassiveProvider.daily_bars(session)**: one call to
`GET /v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=false&include_otc=false`.
Auth by `Authorization: Bearer` header, never `apiKey=` in the URL.
- HTTP 200 with zero results: accepted only if `SessionCalendar` says closed; otherwise
  `IncompleteBatchError`.
- `resultsCount != len(results)`, unparseable or truncated JSON: `IncompleteBatchError`.
- Row count < 90% of the trailing-20-session median (from stored `massive` rows):
  `IncompleteBatchError`. Fewer than 20 stored sessions: refuse unless `--bootstrap`, which
  applies an absolute floor (default 8,000 rows, configurable).
- `NOT_AUTHORIZED` / 403 (date beyond the 2-year plan window): `ProviderError` with
  `code='not_authorized'`, not retried.
- Timeout, connection error, 429, 5xx: `ProviderError` (429 honours `Retry-After` once,
  then raises). Client-side limiter for the free tier's 5 calls/min.
- `last_trade`: always raises `ProviderError('not in plan')` (Basic has no real-time trades).

**AlpacaProvider**: `GET data.alpaca.markets/v2/stocks/trades/latest?symbols=...&feed=iex|delayed_sip`,
`GET /v2/stocks/bars?timeframe=1Day&adjustment=raw&feed=sip` (multi-symbol, paginated,
universe = symbols of the last good Massive session), corporate actions endpoint. Header
auth `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`.

**PriceRouter**: primary, then fallback, then `RouterExhaustedError` (a `ProviderError`
carrying both causes). Every hop is logged with class and redacted message. Nothing is
swallowed; the only catch-and-convert is in `decision.py`, which turns the exhausted router
into `NoSuggestion(reason=...)`, the documented fail-closed output.

**Intraday (`intraday.py`)**: slots 09:35, 10:30, 12:30, 13:30 ET only; any other slot raises.
1. Alpaca `feed=iex`, `now - trade_ts <= 120 s`, else stale.
2. Only if slot >= 10:30: Alpaca `feed=delayed_sip`, flagged `delayed=True`. Freshness for a
   15-minute delayed feed is `now - trade_ts <= 900 + 120 s` (see open question Q2).
3. Otherwise `StaleDataError` -> `NoSuggestion`. 09:35 never uses delayed data.
Every quote that `decision.py` returns as `PriceOK` is persisted to `intraday_quotes` first;
if the write fails, the result is `NoSuggestion` (an unrecorded price is not used).

**Daily job** (`python -m market_data_ingest.prices.daily_job [--session D] [--bootstrap] [--db PATH]`):
1. Calendar check. Closed: write `skipped_closed`, exit 0. Refuses a session that has not
   closed yet (before 20:00 ET on that date).
2. Append `ingestion_runs(running)`.
3. Fetch via router (Massive, then Alpaca daily bars).
4. Stage to `data/staging/<run_id>/daily_bars_<D>.parquet` (DuckDB `COPY`).
5. Validate the staged batch: schema, `low <= min(open,close)`, `high >= max(open,close)`,
   `low <= high`, prices > 0, volume >= 0. Any invalid row fails the batch (strict; Q4).
6. One transaction: `INSERT ... SELECT FROM parquet ... ON CONFLICT DO NOTHING`.
7. Re-pull T-1..T-5 through the same steps 3-6 (corrections land as new rows).
8. Splits + dividends with `ex_date >= T-30`, same staging/validate/insert.
9. Append `ok` or `failed` (with redacted error); exit 1 on any failure.
   Atomicity unit = one batch (one session's bars, or one corporate-action pull), each in its
   own transaction. Batches are independent: a failure in step 7 or 8 fails the run, but
   step 6's batch stays committed because it is complete and valid on its own. The run
   status (not the presence of rows) is what tells a consumer the job did not finish.
   Tests pin both halves: a failed batch leaves zero rows from that batch; a failed later
   batch leaves earlier batches intact and the run `failed`.
DB path: `--db`, else `$DCA_PRICE_DB`, else `<repo>/data/backfill.duckdb`.

**Reconciliation (`reconcile.py`)**: for each symbol in both sources, the last
`min(250, overlap)` sessions; fewer than 60 overlapping sessions -> `insufficient` (treated
as block). Compare Yahoo `Close` with Massive raw close split-adjusted to the Yahoo file's
download date (Yahoo `Close` is split-adjusted, not raw). Per session `|diff|/yahoo`:
<= 0.1% pass, 0.1-1% flag, > 1% block; symbol tier = worst session. Volume: Yahoo volume vs
split-adjusted raw volume, > 10% on any session sets `volume_flag` (flag, never block alone).
Adj Close diff reported, informational only. Results append to `reconciliation_results`.
**Splice guard**: `spliced_adjusted_series(symbol, splice_date)` raises
`SpliceNotValidatedError` unless a `pass` row exists for that symbol whose window ends at
the splice date and carries a factor. Sources are never mixed without it.

## 5. Explicit non-goals this cycle
- No change to `near_close_dca`, the ladder, `config/`, or any of the three entry points.
- Not wired into any live slot or executor prompt. `decision.price_for_suggestion` is a
  library function; wiring it into a live path needs its own review and acceptance run.
- Yahoo daily update (`tickers-daily-update`, `yfinance-prefetch`) keeps running; consumers
  of `ticker_data/` are not repointed. Cutover is a follow-up.
- No `.github/workflows/` edits (AGENTS.md). CI job + pip-audit manifest entry proposed in PR.
- No backfill run of the full 2 years inside this cycle beyond the PoC.

## 6. Tests (written first, gate B)
Behaviour-derived, one assertion theme each: grouped daily parses to raw bars; empty holiday
accepted only when calendar says closed; NOT_AUTHORIZED beyond the window; forward split,
reverse split, stock_dividend parse and adjust; fail-closed on timeout, 429, 5xx, truncated
JSON, `resultsCount` mismatch, stale intraday; IncompleteBatchError under 90% median;
idempotency (same day twice = same row count; correction = new row, later `as_of`);
point-in-time NVDA; reconciliation tiers at 0.1% / 1% boundaries; splice guard; 09:35 never
delayed; every used quote persisted; keys never in logs, exceptions, URLs, or the DB.
HTTP is faked at the `requests.Session` seam (no network). Clock and sleep injected.

**Fixtures caveat**: keys are not available here, so fixtures cannot be live-recorded now.
They will be built from the vendors' documented response shapes, each file carrying a
`"_provenance": "synthetic-from-docs"` field, and NVDA values taken from the public split
record. The opt-in PoC re-records real payloads into `tests/prices/fixtures/recorded/`
when keys are present, and a test then checks the synthetic shapes against them.

## 7. PoC (opt-in contract test)
`PRICE_CONTRACT=1 MASSIVE_API_KEY=... ALPACA_KEY_ID=... ALPACA_SECRET_KEY=... pytest tests/prices/contract -m contract`
20 tickers x last 60 sessions: NVDA, AAPL, MSFT, AMZN, GOOGL, META, JPM, XOM, JNJ, PG, KO,
WMT, HD, UNH, V; plus a reverse-split name, a high-dividend name, a delisted name and an
IPO, **discovered from the data** (reverse_split with ex_date in the window; a symbol present
early in the window and absent later; absent early and present later) because the
choice depends on events after my knowledge cutoff. Diff against Yahoo Close/Adj Close,
confirm the delisted symbol appears through its last session, write
`docs/price_source_poc_report.md`. Needs the operator's keys; I run it, not Codex.

## 8. Rocks (build order after gate B)
1. ADR + store (DDL, views, staging, append-only insert). Proof: `pytest tests/prices/test_store_point_in_time.py`.
2. HTTP + providers + calendar. Proof: `pytest tests/prices/test_{http_fail_closed,massive_provider,alpaca_provider}.py`.
3. Router + daily job + intraday + decision. Proof: `pytest tests/prices/test_{router,daily_job,intraday,decision}.py`.
4. Legacy Yahoo import + reconciliation + splice. Proof: `pytest tests/prices/test_{legacy_yahoo,reconcile}.py`.
5. README, deps, PoC report. Proof: `ruff check --isolated --select=E,F,B,I --line-length=120 dca-sizing-signals && ruff format --check dca-sizing-signals/market_data_ingest/prices && pyright dca-sizing-signals/market_data_ingest/prices && (cd dca-sizing-signals && python -m pytest tests/prices tests/test_architecture_boundaries.py) && .venv/bin/python -m pytest` (core suite still green).

## 9. Risks and rollback
- Purely additive; nothing live reads it. Rollback: revert the PR, delete `data/backfill.duckdb` and `data/staging/`.
- Vendor endpoint/field drift (Massive rebrand is recent): PoC is the canary; fixtures carry provenance.
- Yahoo archive may mix pre- and post-split adjustment if the incremental updater did not
  re-download after a split; reconciliation will surface it as `block`, not hide it.
- A->B->A vendor flip-flop: the third A conflicts with the first row and is dropped, so
  "latest" shows B. Known limitation, recorded in the ADR.
- Free-tier rate limits (Massive 5/min) make a 2-year backfill take ~100+ minutes.

## 10. Open questions for the operator (my recommendation first)
- Q1. Accept DuckDB as a new dependency scoped to `dca-sizing-signals`? (Recommend yes; it is new to the repo, not existing.)
- Q2. Delayed SIP is ~15 min old by design, so 120 s max_age can never pass. Use 120 s past the 15-min delay for `delayed_sip`? (Recommend yes.)
- Q3. Slots: only 09:35/10:30/12:30/13:30 as briefed, or also 11:30, 14:30, 15:30 from the live schedule? (Recommend the four briefed; others raise.)
- Q4. Strict validation (any bad row fails the batch) vs quarantine bad rows below a threshold? (Recommend strict for now.)
- Q5. `yahoo_legacy_bars` + `reconciliation_results` as two extra tables? (Recommend yes.)
