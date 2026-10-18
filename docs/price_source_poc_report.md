# Price source PoC report (Massive + Alpaca vs yahoo_legacy)

**Status: not yet run.** The live proof of concept needs `MASSIVE_API_KEY`, `ALPACA_KEY_ID`
and `ALPACA_SECRET_KEY`, which were not available in the environment that built the price
source (2026-10-18). Nothing below is a measured result.

Running the opt-in contract test replaces this file with the measured report:

```bash
cd dca-sizing-signals
PRICE_CONTRACT=1 DCA_TICKER_HISTORY_DIR=/path/to/ticker_data python -m pytest tests/prices/contract -m contract
```

The keys are read from the environment (see the "Price source" section of
`dca-sizing-signals/README.md`). The run takes roughly 15 minutes on the free Massive tier.

## What the run checks

- **Universe:** 20 tickers over the last 60 closed sessions. That is 16 S&P 500 names (NVDA,
  AAPL, MSFT, AMZN, GOOGL, META, JPM, XOM, JNJ, PG, KO, WMT, HD, UNH, V, COST) plus four role
  names discovered from the window's own data: a reverse split, a high-dividend name, a
  delisting and an IPO. The brief's "15 S&P + 4" added up to 19, so COST is the 20th.
- **Delisting:** the delisted name appears in every grouped batch through its last session,
  and in none after.
- **Yahoo comparison:** for every name with a Yahoo CSV, `Close` against Massive split-adjusted
  close is tiered pass (<= 0.1%), flag (<= 1%) or block (> 1%). `Adj Close` against the
  store's total-return close is reported for information. Volume flags above 10%.
- **Alpaca:** failover daily bars for the last session are compared with Massive, and an IEX
  last trade is requested for the core names.
- **Fixtures:** live payloads are re-recorded, credentials removed, into
  `tests/prices/fixtures/recorded/`. `test_fixture_shapes.py` then checks the synthetic
  fixtures against them.
