# Scope: the fundamentals feed for the daily universe ranking

- **Status:** Scoping only (2026-11-19). No code. It needs operator answers (section 6) before any build.
- **Why:** `docs/adr-program-maintained-universe.md` ranks every symbol in `tickers.txt` each day. The
  engine (`ticker-advisor/universe.py`) needs four factors per name. Two come from prices. Two come from
  SEC filings, and no code produces them for the whole pool.

## 1. What the engine needs (verified in `ticker-advisor/universe.py`)

| Input | Used for | Source |
| --- | --- | --- |
| `momentum_6m`, `momentum_12m` | factor 1 (12-1 style, skips the latest month) | price archive |
| `volatility_1y` | factor 3 (lower scores higher) | price archive |
| `profitability` = gross profits / total assets | factor 2 | SEC filings, first-filed, `filed <= as_of` |
| `value` = earnings yield or book-to-price | factor 4 | SEC filings plus price |
| `sector`, `industry` | z-scoring inside a sector, and every add must carry both | SEC SIC code |
| Pool flags per name: listed on a US exchange, security type, price, market cap, 60-day median dollar volume, history length | who can be ranked and bought | price archive, SEC, broker |
| Input freshness: quotes at most 3 sessions old, SEC data at most 7 days old | fail-closed safeguards | the feed's own stamps |

A name missing two or more factors is `unscored` (it is listed with that status, never given a low rank).

## 2. What already exists

| Piece | Where | Status |
| --- | --- | --- |
| Price archive for the `tickers.txt` symbols | `ticker_data` (Google Drive, on the operator's Mac), refreshed by `scripts/run_tickers_daily_update.py` | Exists. Not in this cloud container, so I could not inspect it |
| One SEC fact at a time | `dca-sizing-signals/market_data_ingest/edgar_fundamentals.py` (`fetch_point_in_time_facts`, 171 lines) | Exists and is tested. Keeps `filed` apart from `end`. Called by no scheduled job |
| Network boundary | `dca-sizing-signals/market_data_ingest/` is the only place allowed to make network calls (`tests/test_architecture_boundaries.py`) | Exists. The new feed belongs here |
| Contact-identifying SEC User-Agent | environment variable `SEC_EDGAR_USER_AGENT`, no default | Exists. The operator must set it on the Mac with their own contact |
| Selection and safeguards | `ticker-advisor/universe.py`, plus `UniverseConfig.strict_daily_top(100)` | Merged (PR 2371) |
| Pool filter | `universe.filter_pool` (needs `nms_listed`, `security_type`, `price`, `market_cap`, `median_dollar_volume_60d`, `history_sessions`) | Exists. Nothing fills those fields |

## 3. The gaps

1. **Per-concept fetch does not scale.** `fetch_point_in_time_facts` downloads a company's whole
   `companyfacts` document and keeps one concept. Four or five concepts for about 5,500 names would
   download each document four or five times. Use the SEC's nightly bulk file (`companyfacts.zip`) or
   one request per company that is parsed for every concept at once. I have not checked the bulk
   file's size or the container's network access to `sec.gov`.
2. **No first-filed selection.** The fetcher returns every filing of a fact, including restated ones.
   The engine needs the value as first reported for each period, with `filed <= as_of`. This
   function does not exist yet.
3. **No ticker to CIK map.** SEC data is keyed by CIK. The SEC publishes a ticker-to-CIK file
   (`company_tickers.json`), which I have not fetched or checked for coverage.
4. **No sector data.** The SEC `submissions` data carries a SIC code per company, which gives industry
   (4-digit) and sector (the broader group). Not fetched anywhere yet.
5. **No market cap or share count.** Needs shares outstanding from SEC filings, times price.
6. **Pool flags are not computed anywhere.** Dollar volume and history length come from the price
   archive. Exchange and security type need a source (SEC exchange data or the broker).
7. **Fractional eligibility is a broker fact.** Only an agent session holding Robinhood access can
   read it (`get_equity_tradability`). A `launchd` script on the Mac cannot call it for 5,500 names.
8. **No output contract.** Nothing defines the file the ranking job reads.

## 4. Proposed design

```
Mac, launchd, after the close
  run_tickers_daily_update.py        (existing) refreshes the price archive
  fundamentals feed (new)            reads SEC bulk data, writes fundamentals_snapshot_<as_of>.json
  ranking job (later)                reads archive + snapshot, builds FactorInputs, runs universe.py
```

- **Location:** `dca-sizing-signals/market_data_ingest/` (network allowed, stdlib only, point in time).
- **Contract, not an import:** the feed writes a versioned JSON snapshot with `as_of`, `fetched_at`,
  `source`, a content hash and one record per symbol: CIK, SIC, shares outstanding, first-filed gross
  profit, total assets, equity, net income (with `filed` and `end` dates), and a status such as
  `ok`, `no_cik`, `no_us_gaap_facts`. `ticker-advisor` reads the file. Neither subproject imports the
  other, so the existing boundaries hold.
- **Freshness:** the snapshot's `fetched_at` feeds the 7-day SEC safeguard.
- **Missing data is explicit:** a name with no facts gets a status and a reason, never a zero.
- **Cadence:** filings change quarterly. Refresh the bulk data weekly (meets the 7-day rule) and check
  for new filings daily.
- **SEC fair access:** at most 10 requests per second, contact User-Agent required.

Factor definitions I recommend (operator to confirm, section 6):
- **Profitability:** latest annual `GrossProfit` over `Assets`, first-filed. If a company does not tag
  `GrossProfit`, use revenue minus cost of revenue. If neither exists the factor is missing.
- **Value:** book-to-price first (`StockholdersEquity` over market cap), because it needs one instant
  fact and no four-quarter sum. Earnings yield can follow.

## 5. Build order (each step test-first, each its own draft PR)

| Step | Delivers | Size |
| --- | --- | --- |
| 1 | First-filed and point-in-time selection over the existing fact type | small |
| 2 | Ticker to CIK map and SIC sector and industry reader | small |
| 3 | Bulk reader that extracts the concepts for every CIK in one pass | medium |
| 4 | Snapshot writer and its schema, hash and freshness stamp | small |
| 5 | Price-derived inputs from the archive: momentum, volatility, dollar volume, history length | medium |
| 6 | Adapter that turns snapshot plus archive into `FactorInputs` and pool `Candidate` records | medium |
| 7 | The daily ranking job and its `launchd` template (needs the Mac checkout answer) | medium |

Steps 1 to 4 need no network in their tests (trimmed fixtures, as `test_edgar_fundamentals.py` does).

## 6. Decisions the operator must make

1. **ETFs, funds and foreign filers.** The pool filter requires `common_stock`, so ETFs fail it. ETFs
   also have no filing facts, so they would be missing two factors and `unscored`. Several current
   names look like ETFs or funds (for example QQQ, VT, XLG, AAAU), and ASML, TSM and STM are foreign
   filers that may use a different tagging standard. I have not verified instrument types. Options:
   exclude them from the ranked buy list, rank them on a reduced factor set, or keep a hand-held
   pinned list outside the ranking. I recommend a hand-held pinned list, which keeps today's buys and
   needs no new scoring rule.
2. **Fractional eligibility.** Check only the top 150 or so after ranking, through an agent session
   with broker access, or skip it in the ranking and rely on the existing run-time gate. Skipping can
   leave a top-100 slot on a name that cannot be bought fractionally.
3. **Value factor:** book-to-price first, or earnings yield?
4. **Bulk file or per-company requests.** I recommend the bulk file, if the Mac can download it.
5. **SEC User-Agent contact.** The operator sets `SEC_EDGAR_USER_AGENT` on the Mac. I will not choose
   a contact.

## 7. Not verified

- Network access from this container to `sec.gov` (not tested).
- Size, format and refresh time of the SEC bulk files.
- Coverage of `company_tickers.json` for ETFs, ADRs and recent listings.
- The contents and layout of the price archive on the Mac.
- How many of the 5,520 symbols would end up scored. That is only known after step 6 runs.
