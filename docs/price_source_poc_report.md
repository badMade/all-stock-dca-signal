# Price source PoC report (Massive + Alpaca vs yahoo_legacy)

Generated 2026-10-18 15:32 UTC by `market_data_ingest.prices.poc` (tests/prices/contract/test_live_poc.py). Window: 60 sessions, 2026-07-24 to 2026-10-16.

| Symbol | Role | Sessions present | First | Last | Yahoo close tier | Worst close diff % | Worst Adj Close diff % | Volume flag |
|---|---|---|---|---|---|---|---|---|
| NVDA | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| AAPL | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| MSFT | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| AMZN | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| GOOGL | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| META | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.001 | 0.001 | False |
| JPM | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| XOM | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | insufficient |  |  | False |
| JNJ | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| PG | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | insufficient |  |  | False |
| KO | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| WMT | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | insufficient |  |  | False |
| HD | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| UNH | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | insufficient |  |  | False |
| V | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | insufficient |  |  | False |
| COST | S&P 500 | 60 | 2026-07-24 | 2026-10-16 | pass | 0.001 | 0.001 | False |
| AEHL | reverse_split | 60 | 2026-07-24 | 2026-10-16 | block | 1.267 | 1.267 | False |
| CPIX | high_dividend | 60 | 2026-07-24 | 2026-10-16 | pass | 0.000 | 0.000 | False |
| QNCX | delisted | 55 | 2026-07-24 | 2026-10-09 | insufficient |  |  | False |
| APMD | ipo | 55 | 2026-07-31 | 2026-10-16 | n/a (no Yahoo CSV in the archive) |  |  |  |

Delisted check: QNCX appears in every grouped batch through 2026-10-09 and in none after it.

- Alpaca failover bars for 2026-10-16: 19 symbols, worst close diff vs Massive 0.000%
- Alpaca IEX last trade returned 16/16 core symbols

## Reading the results

Added by hand after the run (2026-10-18). A re-run regenerates the table above and drops this
section.

**Test outcome:** 7 of 8 contract tests passed in 16m45s. The one failure was an over-strict
test (it required an Adj Close diff for symbols whose status is `insufficient`, which by
definition have none), since fixed. No API key appears in this report or in any of the five
recordings under `dca-sizing-signals/tests/prices/fixtures/recorded/`.

- **`insufficient` for XOM, PG, WMT, UNH, V is Yahoo staleness, not a data mismatch.** Their
  Yahoo CSVs end 2026-10-14, two sessions before the window's end, so each has 58
  overlapping sessions against the 60-session minimum. A 60-session window with a 60-session
  minimum fails whenever the Yahoo archive lags at all.
- **The AEHL `block` (1.267%) is Yahoo rounding, not a Massive error.** AEHL reverse-split
  1-for-16 on 2026-08-10. Before it, Massive's raw closes were about $0.30, which
  split-adjust to, for example, $4.8608 on 2026-08-07. Yahoo stores the adjusted value
  rounded to the cent, $4.80, and on sub-$1 history that rounding alone exceeds the 1%
  tier. All four of the worst AEHL sessions are pre-split.
- **QNCX is probably not a true delisting.** It leaves Massive's grouped data after
  2026-10-09, but the Yahoo archive shows trades on 2026-10-12 and 2026-10-13 with real
  volume. The likeliest explanation is a move to OTC, which the grouped call excludes
  (`include_otc=false`). From grouped presence alone the role discovery cannot tell a
  delisting from an OTC move.
- **APMD (IPO) has no Yahoo CSV**, so it has no comparison; it first appears on 2026-07-31.
- **The vendor field names held.** Massive's grouped bars match the synthetic fixtures exactly.
  Live dividends add `split_adjusted_cash_amount`. `historical_adjustment_factor` appears on
  only some split rows (15 of the first 50), which the parser already treats as optional.

**Follow-ups, not done here:**
- a cents-level floor on the reconciliation tiers, so sub-$1 rounding cannot block a symbol;
- a real delisting flag (rather than grouped presence) before naming a "delisted" ticker;
- a window longer than the 60-session minimum, or a lower minimum, to tolerate a lagging
  Yahoo archive.
