# ADR: Prediction market data ingestion (Polymarket Intl, Polymarket US, Kalshi, index binaries, forecasting platforms)

Status: proposed (permissions pending for some sources; build and test everything now; no implementation until tests approved)
Date: 2026-11-12 (D1-D13; voice-call decisions added: composite score, Polymarket US in scope, build without waiting for permissions; Polymarket terms reviewed)
Source of truth while drafting: claude.ai Project "all-stock-dca", `claude/prediction-markets-adr.md`. This repo copy supersedes it once merged.

## 1) Problem

Add prediction market and forecasting-platform probabilities as point-in-time research inputs.
- Company-event markets (e.g. NVIDIA CEO successor) feed volatility and downside risk, not expected return, because the stock price already reflects public odds.
- Index binaries (S&P 500, Nasdaq-100) feed the market-level part of a stock forecast (beta, regime, tail risk).
- Forecasting platforms (Metaculus, GJ Open) feed slow-moving macro and policy regime inputs.
- Training data for AI models: keep every version available (each venue's raw price plus a cross-venue composite).

How stored probabilities become forecast inputs is in `docs/adr-prediction-markets-methods.md`.

## 2) Decisions

| # | Decision |
|---|---|
| D1 | Use: research-only feature for volatility and downside risk in backtests, and training data for AI predictive models. |
| D2 | Design: object-oriented. Abstract `PredictionMarketSource` base class with one adapter per venue. Built so a later `ForecastProvider` can consume it. Feeding `SizingSuggester` requires a separate ADR. |
| D3 | Cadence is config. Default: hourly quotes for all markets. Watchlist markets get quotes and order-book snapshots every few minutes, tightened toward each venue's rate limit after measuring. |
| D4 | Store every market. Liquidity is a flag (`usable`), not a filter at ingestion. |
| D5 | Never read in a live buying slot (09:35, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30). |
| D6 | Fail closed: HTTP, auth or parse error writes no rows, logs a failed run in `ingestion_runs`, never raises downstream. |
| D7 | Each source carries a `license_scope` in config (`store_ok`, `live_only`, `manual_only`). It gates persistence only. The ingestion layer refuses to persist rows from a source not marked `store_ok`. |
| D8 | No new client dependency. Adapters use `requests` (existing). Not adopted: `py-clob-client` (archived 2026-05-25), `pmxt` (needs dependency review). Endpoint paths isolated per adapter. |
| D9 | Markets are append-only. Never delete voided, delisted, duplicate or rule-changed markets. Every pull stores `rules_hash`; a changed hash writes a new `pm_markets` version row. |
| D10 | Ingest structured fields only. Never parse venue-generated narrative or summary text as data. |
| D11 | **Build without waiting for permissions.** Every adapter (Polymarket Intl, Polymarket US, Kalshi, CME, Cboe, Metaculus) is built and tested now against real captured responses. Pending permissions gate only two switches per source: persistence (`license_scope`) and live scheduling. Flipping a switch is a config change plus review, not new code. |
| D12 | **Polymarket US is in scope.** Adapter built now, including API-key authentication, identity-verification prerequisites and the venue's conformance tests. Credentials come only from the authorized vault or environment; never in code, config files, fixtures or logs. Missing or invalid credentials fail closed (D6). |
| D13 | **Composite score.** For each real-world event, one pooled probability across all venues: log-odds average, weighted by liquidity and spread, no extremizing (cross-venue prices are arbitraged, not independent; Polymarket US and Intl share no books, so US adds a partly independent crowd). Raw per-venue prices are always kept, so models can train on any version. Requires a reviewed event-linking table because venues word the same event differently. |

## 3) Venues

### Company and macro event markets

| Venue | Login needed to read | license_scope (initial) | Status |
|---|---|---|---|
| Polymarket International (polymarket.com) | No | store_ok, conditional (see 9a) | Phase 5a. |
| Polymarket US (polymarket.us) | Account, identity verification, API keys; some public endpoints reportedly open (about 20 req/s, unverified) | live_only until account approved and terms confirmed | Phase 5a. Adapter built now (D12). |
| Kalshi | No for market data | live_only until written permission | Adapter built now (D11), persistence off. |
| Robinhood / Rothera | n/a | n/a | Not a separate source. Routes to Rothera, Kalshi, ForecastEx. |
| IBKR ForecastEx | IBKR account (TWS API) | unknown | Deferred. Bid/ask only, no historical trades. |
| Crypto.com CDNA (ex-Nadex) / OG | Unknown | unknown | Deferred. No confirmed public data API. Index contracts duplicate CME/Cboe. |

#### Polymarket US vs International

| | Polymarket US | Polymarket International |
|---|---|---|
| Operator | QCX LLC, CFTC-regulated | Adventure One QSS, not CFTC-regulated |
| Settlement | CFTC rules | UMA oracle (documented manipulation history) |
| Order book | Separate; different crowd | Separate |
| Coverage | Narrower (was invite-only and sports-only in April 2026; current listings unconfirmed). The user's NVIDIA CEO link is a US market. | Broader |
| API access | Account, identity verification, conformance tests, written approval before live use | No key for reads |
| Value here | Cleaner resolution data; partly independent price for the composite; markets that may not exist on Intl | Breadth, history back to 2022 |

Never merge the two venues' prices without the `venue` label.

### Index binaries (no single-stock contracts on any of these)

| Venue | Contracts | Regulator | Data source | History | Status |
|---|---|---|---|---|---|
| CME event contracts (also via FanDuel Predicts) | E-mini S&P 500 (`ECES`), E-mini Nasdaq-100 (`ECNQ`), Russell 2000, DJIA, commodities, FX, crypto, CPI (`ECCP`), NFP (`ECNF`). Daily, quarter-end, year-end; hourly S&P (`ECS10/11/13/15`) | CFTC | Databento `GLBX.MDP3` (paid); CME Daily Bulletin Section 73 PDF (free, daily settle) | From 2025-09-25, BBO 1-minute | Phase 5b. |
| Cboe Predicts | XSP binaries `XSPBW`, `XSPBX`. $100 if XSP settles at or above strike | SEC, OCC-cleared | OPRA via IBKR TWS or a vendor | Unverified | Phase 5c. |
| Nasdaq MRX Outcome-Related Options | Nasdaq-100 and Micro (XND) binaries, $100 payout | SEC (FR 2026-08680, 2026-05-05) | OPRA once listed | None | Watch only. |

Before buying any vendor feed, confirm it carries the event-contract product codes.

### Forecasting platforms (reputation-scored, not markets)

| Platform | Access | Terms | license_scope | Status |
|---|---|---|---|---|
| Metaculus | API token (free account). `GET /api/posts/?with_cp=true`; history in `aggregations`; tier-limited | Terms (modified 2026-09-21): no AI/ML training, evaluation or development without prior written permission | manual_only until permission | Phase 5d. Adapter built now (D11). |
| Good Judgment Open | No public API found | Not reviewed | manual_only | Manual reference. GJP IARPA ACE data (2011-2015) on Harvard Dataverse. Paid: FutureFirst. |

### Bulk and institutional data

| Source | Cost / license | Content | Status |
|---|---|---|---|
| Jon Becker `prediction-market-analysis` | Free, MIT | ~36 GiB Parquet of Kalshi and Polymarket markets and trades | Polymarket part: backfill candidate. Kalshi part: persistence off until Kalshi permission. |
| Dune / Polygon on-chain | Free tier | Polymarket CTF exchange fills, block-timestamped | Optional cross-check. |
| ICE Polymarket Signals and Sentiment | Institutional | Normalized feed, up to 5 years | Out of scope. |
| Tradeweb-distributed Kalshi data | Institutional | Not reviewed | Out of scope. |
| S&P DJI x Kalshi indices | TBD | Planned | Watch. |

## 4) Endpoint and limit details

Per the research report; verify against live docs during fixture capture.

### Polymarket International

| API | Purpose | Notes |
|---|---|---|
| Gamma (`gamma-api.polymarket.com`) | Discovery and metadata | `outcomePrices`, `outcomes`, `clobTokenIds` are double-encoded JSON strings. |
| CLOB (`clob.polymarket.com`) | Live book, price, `/prices-history` | Batches up to 20 tokens; reportedly back to 2022 at 12-hour resolution. No historical order-book endpoint. |
| Data API v2 | Trades, positions, `/v2/prices-history` | |

Limits per 10 seconds: Gamma 4,000 (300 on `/markets`); CLOB 9,000 (1,000 on `/prices-history`); Data v2 `/v2/prices-history` 200.

### Polymarket US

Endpoints, auth scheme and limits not yet verified (docs behind account). First task after account approval: capture real responses for public and authenticated endpoints as fixtures. Until then, only public-endpoint fixtures.

### Kalshi

| Item | Detail |
|---|---|
| Base | `external-api.kalshi.com/trade-api/v2` (host in config) |
| Auth | None for market data; RSA key for trading |
| Live endpoints | markets, events, order book, `/markets/trades`, candlesticks (1m, 1h, 1d OHLC of bid, ask, price) |
| Historical tier | `GET /historical/cutoff`, then `/historical/markets`, `/historical/markets/{ticker}/candlesticks`, `/historical/trades` |
| Limits | One test hit 5,000 candles per request; chunk. Rate-limit tiers set by trading volume since 2026-06-11. |
| NVIDIA series | `KXNVDACEO`, `KXNVDACEOINT` |

## 5) Index binaries: what they add

- A binary price at strike K is the risk-neutral P(index >= K at expiry). A strike ladder gives the distribution directly.
- Derivable from vanilla options (DoltHub chain), so partly redundant. A persistent gap is more likely fees or microstructure than information.
- Options-type signals inform volatility, not directional alpha.
- Acceptance test: binary features must improve backtest volatility or tail forecasts beyond the DoltHub options-implied distribution.

## 6) Schema (DuckDB)

`ts_utc` is the venue's event time; `ingested_at` is when we stored it.

```
-- facts
pm_markets   (venue, market_id, version, title, rules_text, rules_hash, created_ts, close_ts,
              resolved_ts, resolution, status, first_seen_at, ingested_at)
pm_quotes    (venue, market_id, outcome, ts_utc, bid, ask, mid, last, volume_24h,
              liquidity_usd, usable, resolution_sec, backfilled, source_api, ingested_at)
pm_trades    (venue, market_id, outcome, ts_utc, price, size, taker_side, source_api, ingested_at)

-- composite (D13)
pm_events          (event_id, title, kind, created_at)
pm_event_links     (event_id, venue, market_id, outcome, relation, valid_from, valid_to, reviewed)
                   -- relation: same | inverse | subset; reviewed by hand
pm_composite       (event_id, outcome, ts_utc, prob, n_venues, weights_json, method_version, computed_at)
                   -- derived; recomputable from pm_quotes; never overwrites raw quotes

-- supporting
pm_orderbook_snapshots (venue, market_id, outcome, side, level, price, size, ts_utc, ingested_at)
pm_ticker_map          (event_id, ticker, relation, valid_from, valid_to, reviewed)
idx_binaries           (venue, symbol, underlying, strike, expiry_ts, bid, ask, last, volume,
                        open_interest, quote_ts, ingested_at)
fc_forecasts           (platform, question_id, title, prob_or_median, n_forecasters, forecast_ts, ingested_at)
```

Point-in-time rules:
- Never filter on `close_ts`, `resolved_ts` or `expiry_ts` to decide what was known.
- Use the `pm_markets` version whose `ingested_at <= as_of`.
- A Kalshi candle is known at `end_period_ts`.
- Daily alignment to equities: last mid strictly before 16:00 ET.
- Mid, not last trade; keep the spread.
- `resolution_sec` stored so 12-hour history never mixes with hourly data.
- Exclude markets created after the as-of date.
- Composite at time t uses only quotes with `ts_utc <= t` and event links with `valid_from <= t`.
- Ticker mapping is reviewed by hand and dated.

## 7) Liquidity flag (D4)

`liquidity_usd` is the dollar value of resting orders near the price. Rows below the threshold are stored with `usable = false`; backtests, models and the composite exclude or down-weight them. Start at $50K liquidity; spread is the second check. For index binaries use spread and open interest; for forecasting platforms use forecaster count.

## 8) Reliability evidence and design consequences

| Study | Status | Finding | Consequence |
|---|---|---|---|
| Diercks, Katz & Wright, FEDS 2026-010 | Fed working paper | Kalshi macro expectations comparable to benchmarks | Macro markets most trustworthy. |
| Bürgi, Deng & Whelan, CEPR DP20631 | Working paper | Favorite-longshot bias; average return about -20% pre-fee | Calibrate raw prices before use. |
| Becker (2025/26) | Working paper, dataset | 5c contracts won 4.18% | Calibration curve per venue. |
| Gómez-Cram, Guo, Jensen & Kung | Working paper | Earnings-beat markets beat analyst benchmarks, lead price discovery | Earnings markets are the strongest single-company input. |
| Gómez-Cram et al., "Crowd Wisdom or Informed Minority?" | Working paper | Accuracy from a small skilled minority | Store trades; large-trade order imbalance as candidate feature. |
| Ng, Peng, Tao & Zhou (2026) | Working paper | Polymarket led Kalshi; large-trade imbalance predicts returns | Store `taker_side`; lead-lag is research only. |
| Mitts & Ofir | Working paper | 210k+ suspicious wallet-market pairs; flagged trades won 69.9% | Never act live (D5). |
| Sirolly, Ma, Kanoria & Sethi (Columbia) | Working paper | ~25% of Polymarket volume likely wash trading | Volume is not liquidity. |
| Polymarket accuracy page | Marketing | Claims 94% accuracy | Not evidence. |

None are peer-reviewed. None covers CEO-succession or M&A markets.

## 9) Terms review (not legal advice)

### 9a) Polymarket International (polymarket.com terms, effective 2026-08-11; read in full)

- Operator Adventure One QSS; not CFTC-regulated; Panama law; arbitration; $100 liability cap.
- Connecting through the API counts as accepting the terms.
- Data restriction names professional firms (hedge funds, investment advisers, prop-trading firms, fintech, data resellers); those need written permission. A retail individual is not on that list.
- Robots and crawlers banned: use documented API only, never scrape web pages.
- No selling or passing data to those professional firms.
- No clause found banning personal storage or model training.
- US persons may read but not trade.
- Ambiguity: one developer audit reads the data clause as covering API access generally. Recommendation: email hello@polymarket.com to confirm personal storage and model training in writing before large-scale storage.

### 9b) Polymarket US

- Individual Participant and Clearing Member Agreement v1.4 (2026-08-06), read in full: applies once an account is opened. All system data is Polymarket US property; use only as the agreement and rules allow; no redistribution without express permission. No explicit ban on personal storage or model training. API use requires conformance tests and written approval before live use, plus identity verification. New York law.
- Entity version of the agreement also seen; not the one that applies.
- Website terms of service (polymarket.us/tos): unread (page embeds a Google Doc). Open item.

### 9c) Kalshi

Developer Agreement v1.1 bars storing, caching, aggregating or sharing API data for uses other than your own Kalshi trading without written authorization. Applies to the Kalshi part of the Becker dataset.

### 9d) Metaculus

Terms bar AI/ML training or evaluation without prior written permission.

## 10) Known data biases

- Multi-outcome markets often don't sum to 1; some have no "Other" bucket.
- Prices under about 10-15c and above 90c are distorted (favorite-longshot).
- Long-dated markets compressed toward 50%.
- About 25% of Polymarket volume is wash trading.
- Rules can be clarified after the fact; `rules_hash` detects it.
- Market prices are risk-neutral; for crash-correlated events Yes exceeds real-world probability.

## 11) Open items (none block building or testing, per D11)

| # | Item | Gates |
|---|---|---|
| 1 | Email Polymarket Intl (hello@polymarket.com) to confirm personal storage and model training | Large-scale persistence |
| 2 | Open Polymarket US account, complete identity verification, request API approval; read polymarket.us/tos | US authenticated fixtures, persistence, live use |
| 3 | Request Kalshi written permission | Kalshi persistence |
| 4 | Request Metaculus written permission | Metaculus persistence and model use |
| 5 | CME vendor: Databento `GLBX.MDP3` after confirming license and product codes | CME fixtures and persistence |
| 6 | Cboe access: IBKR account vs OPRA vendor | Cboe fixtures |
| 7 | CDNA: confirm a public data API exists; otherwise drop | CDNA adapter |

## 12) Handoff to Claude Code

- This ADR and `docs/adr-prediction-markets-methods.md` are in the repo.
- Claude Code proposes the module location and config file location, consistent with `claude/architecture.md` layers.
- Snapshot job schedule (where and how it runs on the Mac Studio) is not decided; propose it, do not schedule anything without approval.
- Order of work: real fixtures, then tests (wait for approval), then implementation.
- Work runs on the Mac Studio.

## 13) Tests first (awaiting approval)

| Test | Checks |
|---|---|
| `test_parses_stringified_outcome_arrays` | JSON-string fields parse to typed lists (real fixture) |
| `test_casts_numeric_strings` | numerics cast; bad values become null |
| `test_quote_rows_carry_ts_and_ingested_at` | every quote has event time and ingest time |
| `test_pit_query_excludes_future_markets` | as-of D returns no market created after D |
| `test_thin_market_stored_but_flagged` | below threshold stored with `usable = false` |
| `test_cadence_read_from_config` | default interval comes from config |
| `test_watchlist_cadence_overrides_default` | watchlist markets use the faster interval and capture order books |
| `test_daily_alignment_uses_last_mid_before_1600_et` | daily value is the last mid strictly before 16:00 ET, DST-correct |
| `test_narrative_fields_not_ingested` | summary or description text never populates price fields |
| `test_http_failure_fails_closed` | mocked 5xx writes no rows, logs failed run, no raise |
| `test_live_slots_never_import_pm` | buying-slot modules do not import the module |
| `test_adapters_implement_source_interface` | each venue adapter satisfies `PredictionMarketSource` |
| `test_non_store_ok_source_never_persists` | a `live_only` or `manual_only` source writes zero rows |
| `test_rules_hash_change_creates_new_version` | changed rules text writes a new `pm_markets` version |
| `test_pit_query_uses_rules_version_as_of` | as-of D sees the rules version ingested on or before D |
| `test_voided_and_delisted_markets_never_deleted` | status changes never remove rows |
| `test_polymarket_venue_labels_never_merge` | Intl and US rows keep distinct `venue` values |
| `test_prices_history_batches_max_20_tokens` | batch requests never exceed 20 token IDs |
| `test_client_respects_rate_limit_budget` | requests per 10s stay under configured per-endpoint budget |
| `test_quote_stores_resolution_and_backfilled_flag` | history rows record resolution and backfill status |
| `test_pmus_credentials_loaded_from_vault_or_env_only` | US adapter reads keys only from vault/env; none in config or code |
| `test_pmus_credentials_never_logged` | keys and signatures never appear in logs, errors or `ingestion_runs` |
| `test_pmus_missing_credentials_fails_closed` | missing or rejected keys write no rows, log failed run, no raise |
| `test_pmus_public_endpoints_work_without_keys` | public-endpoint fixtures parse with no credentials |
| `test_composite_logodds_pool_weighted_by_liquidity` | pooled probability matches log-odds weighted mean on a known example |
| `test_composite_no_extremizing` | pooled probability never more extreme than the most extreme input |
| `test_composite_excludes_unusable_quotes` | `usable = false` quotes get zero weight |
| `test_composite_pit_uses_only_past_quotes_and_links` | composite at t uses quotes and links valid at or before t |
| `test_composite_never_overwrites_raw_quotes` | computing composites leaves `pm_quotes` unchanged |
| `test_event_links_inverse_relation_flips_probability` | an `inverse` link contributes 1 - p |
| `test_kalshi_casts_dollar_strings` | `_dollars` strings become decimals |
| `test_kalshi_candle_asof_is_end_period_ts` | candle known only at period end |
| `test_kalshi_routes_settled_to_historical` | markets past cutoff come from `/historical/*` |
| `test_kalshi_candles_chunked_under_cap` | candle requests split to stay under 5,000 per call |
| `test_kalshi_persistence_off_by_default` | adapter fetches and parses, but writes zero rows while `live_only` |
| `test_binary_ladder_is_monotonic` | P(index >= K) does not rise as K rises; violations flagged |
| `test_binary_quote_asof_is_quote_ts` | index binary rows known at `quote_ts`, not expiry |
| `test_metaculus_history_filtered_by_end_time` | history entries used only if `end_time <= as_of` |

Fixtures: real captured responses only. Polymarket US authenticated fixtures wait for account approval.
