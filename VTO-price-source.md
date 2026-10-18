# VTO: Replace Yahoo as the ongoing price source

**What.** A point-in-time, append-only daily-bar and corporate-action store fed nightly by
Massive (ex-Polygon) Stocks Basic, with Alpaca Basic as daily failover and as the intraday
last-trade source for the 9:35 / 10:30 / 12:30 / 13:30 slots.

**Why now.** Yahoo Finance is unreliable and costly. Its history is already pulled
(`ticker_data/*.csv`) and stays, labelled `source='yahoo_legacy'`, but no new Yahoo pulls
should be needed once this source is reconciled.

**Who.** One operator, personal non-professional use. Consumers are research and
suggestion-only tooling (`dca-sizing-signals`, Octopus, ml-forecaster); nothing live.

**Success this cycle.**
1. `python -m market_data_ingest.prices.daily_job` ingests a session idempotently and exits
   non-zero on any failure. Each batch (one session's bars, or one corporate-action pull) is
   atomic: it commits whole or not at all. A job can commit some batches and still fail;
   the run is then marked `failed` and the committed batches are each complete and valid.
2. A price lookup either returns a fresh, provenance-stamped price or an explicit
   "no suggestion" with a reason. Never a stale price.
3. Reconciliation against `yahoo_legacy` tiers every overlapping symbol pass/flag/block.
4. An opt-in live PoC (20 tickers x 60 sessions) runs and its report is checked in.

**Non-negotiables.**
- Fail closed. No swallowed exceptions. No lookahead: an as-of query never sees an event
  dated after it.
- RAW bars only on disk; adjusted values exist only in views.
- Keys from env vars only; never logged, never in URLs, never committed.
- Existing sizing logic, the ladder, and every live entry point are untouched.
- No new runtime dependency for the stdlib-only core.
