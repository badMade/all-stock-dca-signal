# Backtest protocol gap audit

**Status: audit only, nothing implemented.** Written 2026-11-23 on branch
`badMade/backtest-protocol-audit-adr-aecee8` from `main` at `9eee87e4`. Read-only: this document
changes no code, config or test. The decision record for items 7 to 17 is
[`adr-backtest-protocol.md`](adr-backtest-protocol.md).

## How to read this

- **Status** is `present` (the protocol item is met for a backtest today), `partial` (a piece exists,
  usually in the research ingest, but no backtest uses it end to end) or `missing`.
- **Evidence** is `file:line` at the commit above. Line numbers were read directly, not inferred.
- **Confidence** is how sure the audit is about the status, given the search done (grep plus a
  read of the cited code). A `missing` row means "not found by that search", so it never reaches
  100%.
- **Proposed CI test** names a test that would turn the row green. Tests for code in
  `dca-sizing-signals/` belong in `dca-sizing-signals/tests/` (CLAUDE.md: each sibling keeps its own
  `tests/`); see "Conflicts" below.

## Scope of what exists

Two separate things are called "backtest" today, and neither is the harness this protocol describes:

1. `all_stock_dca/backtest.py`: an Octopus Tier C sizing replay. It reads walk-forward prediction
   records, sizes a buy at the record's `ref_close`, and scores it against the close 5 sessions later.
   It writes to SQLite despite the `.duckdb` file name (`all_stock_dca/backtest.py:17`, `:292`).
2. Analytics-only one-offs: `docs/backtest_curve.md` (60-session cap-curve replay, results in
   `config/backtest_intraday_cap_curve.json`) and the `analysis/` trees.

The strongest foundations are in the research ingest, not in either backtest:
`dca-sizing-signals/market_data_ingest/prices/store.py` (append-only point-in-time price store) and
`dca-sizing-signals/market_data_ingest/ledger/store.py` (outcome ledger, `trials`, `universe_membership`,
`market_sessions`), both under `docs/adr-price-source-massive-alpaca.md` and
`docs/adr-outcome-ledger-pit-harness.md`.

## Audit table

| # | Item | Status | Evidence (file:line) | Gap | Proposed CI test | Confidence |
|---|---|---|---|---|---|---|
| 1 | Raw downloads stored unchanged with hash and timestamp (delta archive) | partial | `prices/store.py:66-74` (`daily_bars_raw`: `content_hash`, `as_of`, `ingested_at`, `run_id`); `prices/store.py:350`, `:381` (each batch staged to Parquet named by hash); `prices/store.py:103-111` (`yahoo_legacy_bars.file_sha256`); `scripts/ingest_market_data.py:374`, `:428` (live payload `payload_sha256`) | What is stored is *parsed* rows, not the vendor's response bytes, so a parser bug cannot be replayed from source. No archive of raw HTTP bodies with a byte hash. The referenced ADR `docs/adr/yf-delta-archive.md` is **not in the repo** (nor is `claude/adr-yf-delta-archive.md`), so the intended design cannot be checked | `test_raw_response_archived_byte_identical`: an ingest writes the response body to a content-addressed file whose sha256 matches a recorded column, and re-parsing that file reproduces the stored rows | 80% |
| 2 | Raw prices plus a separate corporate-actions table; adjustments derived in DuckDB | present (research store) / missing (backtests) | `prices/store.py:28-31` (adjusted values never stored); `prices/store.py:66-86` (`daily_bars_raw`, `corporate_actions`); `prices/store.py:154-180` (`adjusted_bars(as_of_date, known_at)` macro) | Neither backtest reads it: `all_stock_dca/backtest.py:94-114` reads per-ticker CSVs from the Yahoo archive, whose `Adj Close` is vendor-adjusted as of download day (lookahead in the adjustment factor) | `test_backtest_reads_only_adjusted_bars_macro`: the harness's price loader issues queries only against `adjusted_bars(...)` and refuses a CSV path | 85% |
| 3 | Fundamentals joined on filed date / `acceptanceDateTime`, never period end; frames API not used; proxy forms excluded | partial | `edgar_fundamentals.py:6-7`, `:56`, `:130` (`filed` kept apart from `end`); `edgar_fundamentals.py:182-205` (`first_filed_facts`: only `filed <= as_of`, first-filed wins); `ledger/store.py:114-119` (`universe_scores.source_filed_date`); no `xbrl/frames` URL anywhere in Python (grep) | `filed` is a **date**, not the `acceptanceDateTime` timestamp: an 8-K or 10-Q accepted at 17:30 ET reads as public at that day's open. No form allowlist: `select_annual_facts` (`edgar_fundamentals.py:177-179`) filters by span only, so any form carrying a matching concept (including `DEF 14A` pay-versus-performance facts) passes. Frames non-use is by absence, not by a guard | `test_fact_filed_after_close_not_actionable_same_session`, `test_proxy_forms_excluded_from_point_in_time_facts`, `test_no_frames_api_url_in_backtest_code` (grep guard, same pattern as `tests/test_yfinance_retired.py`) | 80% |
| 4 | `actionable_at` column on one ET exchange calendar (next session after acceptance) | missing (partial calendar) | `config/market_calendar.json` (NYSE, `years_covered` 2026 to 2028 only); `prices/provider.py:29-32` (`SessionCalendar.is_session`); `prices/alpaca.py:323`; `ledger/store.py:60-62`, `:196-209` (`market_sessions` used for outcome horizons) | No `actionable_at` anywhere. Three calendars exist (the JSON for live Gate 0, Alpaca's for the price store, `market_sessions` in the ledger) with no rule naming one as authoritative for backtests, and the JSON cannot answer for any year before 2026 | `test_actionable_at_is_next_session_open_after_acceptance` (after-close, pre-open, weekend and holiday acceptances), `test_backtest_calendar_refuses_uncovered_year` | 85% |
| 5 | Universe as of each date, including delisted names; survivorship flagged | partial | `ledger/store.py:129-132` (`universe_membership`, half-open `effective_from`/`effective_to`); `ledger/store.py:279-282` (`universe_members_as_of`); `docs/adr-outcome-ledger-pit-harness.md` Decision 5 and Open question 1 (results labelled survivor-biased until a membership source exists); `all_stock_dca/backtest.py:42` (hard-coded current S&P 500 names) | `universe_membership` records the *program's* approved list (`approval_id`), not an index or investable universe, and has no delisted-name source or delisted prices. No backtest output carries a `survivorship_biased` flag; `all_stock_dca/backtest.py` filters to today's large caps | `test_backtest_result_flags_survivorship_when_current_membership_used`, `test_universe_as_of_includes_delisted_member` | 85% |
| 6 | Fail closed: missing, stale or anomalous inputs give no suggestion; anomalies quarantined, never interpolated | partial | `prices/validate.py:1-5`, `:32-56` (strict: one bad bar rejects the batch); `prices/decision.py:1-11`, `:36-80` (`NoSuggestion`, never a fallback price); `ledger/store.py` `outcomes.status IN ('scored','unscored')` at `:81-91`; `edgar_fundamentals.py:13-14` (missing concept is an empty list) | Rejection is all-or-nothing: there is no quarantine table, so a rejected batch leaves no queryable record of *which* row was anomalous. No staleness bound for daily bars or facts in a backtest. `all_stock_dca/backtest.py:326-328` substitutes defaults (`0.0`, `0.02`, `-0.02`) when a prediction record lacks fields, which is the opposite of fail closed | `test_missing_quantiles_give_no_decision_not_default`, `test_anomalous_bar_lands_in_quarantine_not_bars`, `test_stale_input_beyond_bound_gives_no_suggestion` | 85% |
| 7 | Fills at the next tradable price with spread or commission; no same-bar fills | missing | `all_stock_dca/backtest.py:212` (`current_price=ref_close`: the signal's own close is the fill); no commission, spread or slippage term in the file (grep); `docs/backtest_curve.md` "Intraday proxy" (10:30 fills at the daily open, the final slot at the daily close of the same bar) | Same-bar fills in both. No cost model | `test_fill_uses_next_tradable_price_after_decision`, `test_fill_charges_spread_and_commission` | 90% |
| 8 | Cash and budget modeled: undeployed cash earns T-bill yield; no implicit borrowing | missing | `docs/backtest_curve.md` (weekly $300 rollover, no interest); `all_stock_dca/backtest.py` (no cash account); `ticker-advisor/signals.py:34` (`DEFAULT_RISK_FREE_RATE = 0.05`, a constant, outside any backtest); `dca-sizing-signals/overnight_futures/fair_value.py:8` (3-month T-bill, first-release vintage, for futures fair value only) | No cash ledger, no yield series, no check that spend never exceeds cash | `test_idle_cash_accrues_tbill_yield_by_session`, `test_spend_beyond_cash_is_refused_not_borrowed` | 90% |
| 9 | Benchmarks: plain DCA and lump sum on identical price paths, every run | partial | `ledger/store.py:72-79` (`baseline_fills`, a flat-1x counterfactual at the same timestamp and price, `docs/adr-outcome-ledger-pit-harness.md` Open question 4); `all_stock_dca/backtest.py` (no benchmark) | `baseline_fills` scores *live* decisions, not a backtest. No lump-sum benchmark anywhere. Nothing forces every run to emit both | `test_every_run_emits_plain_dca_and_lump_sum_on_same_paths` (shared path hash across the three legs) | 85% |
| 10 | Primary metric is the paired terminal-wealth distribution; IRR and average cost not headlines | missing | `docs/backtest_curve.md` results table (headline is "Discount vs prior close", an average-cost measure); `all_stock_dca/backtest.py:350` (hit rate against the 5-session close); `docs/sizing_suggestion_history.md:63` (`tilt_weighted_excess`, `hit_rate`) | No terminal-wealth computation, no pairing against a benchmark | `test_report_headline_is_paired_terminal_wealth` (report schema puts the paired distribution first and refuses IRR or average cost as headline) | 85% |
| 11 | Walk-forward or purged/embargoed CV, embargo >= label horizon; scalers fit inside folds | partial | `wondrous-octopus/horizons.py:1-5`, `:137-140` (horizon-aware embargo of h rows); `wondrous-octopus/harness.py:565` (per-step `fit`, seeded) | Exists for Octopus forecasts only. The DCA sizing backtest has no fold structure. Scaler-in-fold was not verified in `wondrous-octopus` or `ml-forecaster` (not found by grep; not ruled out) | `test_embargo_at_least_label_horizon`, `test_preprocessing_fit_sees_only_train_fold` | 65% |
| 12 | Trial ledger in DuckDB, one row per variant tried | partial | `ledger/store.py:106-112` (`trials` table); `ledger/store.py:272-276` (`trial_counts` counts every trial, re-evaluations included); `ledger/store.py:729-762` (`ingest_trials`); `dca-sizing-signals/tests/ledger/test_trials.py` | Table exists; nothing writes to it. No backtest registers a trial, so the count it would feed is zero | `test_every_backtest_run_registers_one_trial_row` | 90% |
| 13 | Deflated Sharpe, PBO (CSCV), Reality Check or SPA vs plain DCA, block bootstrap | partial | `ledger/defaults.py:31-34` (`PROMOTION_DSR_THRESHOLD = 0.95`, recorded as a field only); `wondrous-octopus/band_uncertainty.py:105`, `:129-134` (seeded moving-block bootstrap, for band scorecards) | No DSR, PBO, CSCV, White's Reality Check or Hansen's SPA implementation anywhere (grep). The bootstrap is Octopus-local and numpy-based | `test_dsr_matches_published_worked_example`, `test_pbo_cscv_known_overfit_case`, `test_spa_rejects_null_on_synthetic_edge` | 90% |
| 14 | Sub-period, parameter-neighborhood and cost-multiplier robustness reports | missing | none found (grep for `robust`, `neighborhood`, `cost_multiplier`) | No report | `test_robustness_report_has_all_three_sections` | 85% |
| 15 | Sealed holdout window, every access logged | missing | `ml-forecaster/evaluate.py:283-299` (`select_holdout_tickers`: a seeded *ticker* holdout, not a time window, no access log); `wondrous-octopus/harness.py:766-771` ("sealed" refers to a config spec, not data) | No time-sealed window, no access log | `test_holdout_read_appends_access_log_row`, `test_holdout_read_refused_without_reason` | 85% |
| 16 | Suggest-only shadow log with input hash and decision timestamp per suggestion | partial | `docs/sizing_suggestion_history.md:24-25` (`generated_at`, `recorded_at`); `dca-sizing-signals/comovement_signals/shadow.py`; `dca-sizing-signals/market_data_ingest/ledger/csp_shadow.py`, `hedge_shadow.py` | Decision timestamps exist; an input hash per suggestion was not found in the suggestion history format. Live envelopes do hash their payloads (`scripts/ingest_market_data.py:428`), so the pattern is in the repo, just not on the suggester path | `test_shadow_row_carries_input_hash_and_decision_ts` | 70% |
| 17 | Pinned data snapshots and seeds; reproducible runs | partial | `prices/store.py` `known_at` macros (`:128`, `:137`, `:154`) make a snapshot a timestamp; `wondrous-octopus/harness.py:565` (`derive_seed`); `wondrous-octopus/band_uncertainty.py:131-134` (seed per call); `dca-sizing-signals/requirements.txt` (pinned deps) | No run manifest that records `known_at`, seed, code commit and dependency lock together, and no test that two runs of one manifest give identical output | `test_same_manifest_reproduces_identical_results` | 75% |

Evidence paths starting `prices/` or `ledger/` are under `dca-sizing-signals/market_data_ingest/`;
`edgar_fundamentals.py` is `dca-sizing-signals/market_data_ingest/edgar_fundamentals.py`.

## Summary

- **Present:** 1 of 17 (item 2, in the research store only).
- **Partial:** 10 (items 1, 3, 5, 6, 9, 11, 12, 13, 16, 17).
- **Missing:** 6 (items 4, 7, 8, 10, 14, 15).
- The data layer (items 1 to 6) is mostly built in `dca-sizing-signals/` and needs guards and a few
  columns. The harness and statistics layer (items 7 to 17) is mostly missing; the ADR covers it.

## Phase A progress (2026-11-24)

The table above is the audit as found on 2026-11-23 and is left as it was. Since then, on this branch:

- **Item 4:** `dca-sizing-signals/market_data_ingest/exchange_calendar.py` provides `ExchangeCalendar`
  (loads `config/market_calendar.json`) and `actionable_at`. Every test in `tests/test_actionable_at.py`
  passes. A year the file does not cover raises `CalendarCoverageError`, so any acceptance before 2026
  stays unusable until the calendar file is extended backwards.
- **Item 3:** `market_data_ingest/edgar_acceptance.py` provides `parse_edgar_accepted`,
  `POINT_IN_TIME_FORMS` and `actionable_facts`. Every test in `tests/test_edgar_actionable_facts.py`
  passes. Nothing fetches acceptance times yet; callers pass the mapping in.
- **Item 6, pure check:** `prices/validate.py` `bar_anomalies` and `prices/errors.py`
  `AnomalousBarsError` are implemented, and the four pure-check tests pass.
- **Item 6, store wiring: held for an operator decision.** Rejecting a batch at ingest conflicts with
  the store's existing contract that a split may be learned after the bars that cross it
  (`tests/prices/test_store_point_in_time.py`, e.g. `test_known_at_hides_a_split_we_learned_about_later`).
  A hard reject broke 24 existing tests. The three store tests in `tests/prices/test_bar_quarantine.py`
  stay `xfail(strict=True)` until the operator picks between an ingest-time reject and a read-time
  quarantine.

## Conflicts and notes for the operator

1. **Input files missing.** `claude/architecture.md` does not exist; `docs/codebase_roadmap.md` and
   `docs/architecture_plain_english.json` were read in its place. The delta-archive ADR
   (`docs/adr/yf-delta-archive.md`, or its source `claude/adr-yf-delta-archive.md`) is not in the repo,
   so item 1 is audited against the code alone.
2. **ADR path.** The kickoff asked for `docs/adr/`. Every ADR in this repo is flat
   (`docs/adr-<name>.md`, 21 files), and the operator approved the flat path on 2026-11-23.
3. **Test location (Step 3).** The phase A guards (items 3, 4, 6) test code in
   `dca-sizing-signals/market_data_ingest/`. CLAUDE.md puts that sibling's tests in
   `dca-sizing-signals/tests/`, which is outside the kickoff's "docs/ and tests/ only" rule. Step 3
   needs the operator to pick one.
4. **yfinance.** Backtests may read the Yahoo archive (`docs/yfinance_replacement_plan.md`: non-buying
   uses are allowed), but its `Adj Close` is adjusted as of download day. Item 2's test would steer the
   harness to `adjusted_bars` instead. `tests/test_yfinance_retired.py` is unaffected: it scans buying
   paths only.
5. **"No in-repo caller" is not dead code** (CLAUDE.md). `all_stock_dca/backtest.py` is audited, not
   proposed for deletion.
