# yfinance-prefetch

Pre-fetches a labeled `sources.yahoo_finance_yfinance` market-data block from
Yahoo Finance (`yfinance`) for the DCA strategy's yfinance fallback policy
(`config/scheduled_tasks.json`'s `market_data_source_policy`): use as a
whole-universe fallback when a slot's primary source is unavailable, stale,
incomplete, or unresolved.

This is a sibling of `ticker-advisor/`, `ml-forecaster/`, `wondrous-octopus/`,
and `ticker-market-breadth/`: a self-contained, third-party-dependency
subproject with its own `requirements.txt` and `tests/`, excluded from the
core repo's stdlib-only ruff and coverage gates (see the root `CLAUDE.md`).

## What this is for

`scripts/generate_buy_candidates.py` (the 1530ET final-buy slot) and
`scripts/stream_candidates.py` (the 0935ET/1030ET/1130-1330-1430ET/1230ET
slots) both already accept a labeled `sources.yahoo_finance_yfinance` block
in their `--market-data` payload and substitute it for a Finance fetch that
is unavailable, errored, or incomplete for any attempted ticker -- switching
the *entire* ordinary universe to the fallback, never mixing real Finance
evidence for one ticker with fallback evidence for another in the same run.

Neither module fetches that data itself: this repo's core makes no network
calls and has zero third-party runtime dependencies (see the root
`CLAUDE.md`). Historically the only place instructed to *populate* the
fallback block was the live executor prompt itself, fetching yfinance
evidence mid-run with whatever tools that session happens to have. When that
session has no real path to Yahoo Finance -- no outbound network, no
`yfinance` package installed -- the fallback this repo's own code already
supports can never actually be populated, and a Finance outage at 12:30 or
3:30 has no way to recover; both slots simply degrade to whatever
Robinhood-only (or, for the six executor-class slots, no) reconciliation
their engine already falls back to.

This script closes that gap as a small, standalone, run-*before*-the-slot
fetch step, not an OAuth integration or a paid connector: given a ticker
list, a trading day, and a slot's decision time and bar interval, it
downloads previous-close bars, intraday bars, and a current quote from
Yahoo Finance and writes them in the exact shape the two candidate
generators already expect under `sources.yahoo_finance_yfinance`. A caller
(an executor prompt, or a scheduled task that runs a few minutes ahead of a
live slot) merges this script's JSON output into its own `--market-data`
payload. **This script never calls either candidate generator, never places
an order, and is read-only against the DCA strategy's own state.**

## Scheduled use

None. The yfinance fallback was removed from every scheduled run on 2026-10-12
(692b5887), along with `deploy/launchd/prefetch_slot.sh` and its planner, plist
and docs; the candidate generators now reject a `yfinance` source as an
unsupported key. This tool is kept as an unscheduled research helper only.
Robinhood-primary slots get their payload from `deploy/launchd/robinhood_payload.sh`
(see `docs/robinhood_payload.md`).

## Usage

```bash
python fetch_yfinance_fallback.py \
    --tickers AAPL,MSFT,TXN \
    --trading-day 2026-08-05 \
    --decision-time-et 12:30 \
    --interval-minutes 5 \
    --out /path/to/yfinance_fallback_1230.json
```

`--interval-minutes` should match the slot's primary source: `5` for
Robinhood-primary slots such as 12:30 and `15` for Finance-primary 15:30.
`--decision-time-et` is the slot's own decision anchor (e.g. `12:30` for the
midday slot, `15:30` for the 3:30 final buy).

The output is ready to merge directly into an executor's own `--market-data`
JSON:

```python
import json

fallback = json.loads(Path("yfinance_fallback_1230.json").read_text())
market_data.setdefault("sources", {}).update(fallback["sources"])
market_data.setdefault("ticker_errors", {}).update(fallback["ticker_errors"])
```

## Output shape

```json
{
  "sources": {
    "yahoo_finance_yfinance": {
      "AAPL": {
        "previous_close_bar": {"timestamp": "2026-08-04T16:00:00-04:00", "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0},
        "intraday_bars": [{"timestamp": "2026-08-05T09:30:00-04:00", "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0}],
        "quote": {"price": 0.0, "as_of": "2026-08-05T12:29:00-04:00"}
      }
    }
  },
  "source_errors": [],
  "ticker_errors": {"yahoo_finance_yfinance": ["TICKER_WHOSE_FETCH_RAISED"]}
}
```

A ticker whose fetch raises for any reason (network error, unrecognized
symbol, malformed response) is recorded under `ticker_errors` and left out
of `sources.yahoo_finance_yfinance` entirely, matching how both candidate
generators already treat a `ticker_errors` entry -- authoritative evidence
of a fetch exception, never the benign "this source doesn't cover it" case.

`previous_close_bar` is a real daily OHLCV bar from Yahoo, so `low` is
always the genuine prior-session low -- unlike the live Robinhood adapter's
quote-derived previous-close bar (which stamps `low=0.0` to mean "unknown"),
this fallback never needs that sentinel.

## Data only, read-only, no live wiring

This script has no awareness of budgets, the sizing ladder, or order
placement, and it never imports anything from the core repo. It is not
wired into any scheduled task by itself -- adding it to
`config/scheduled_tasks.json` and the relevant live-mirror executor
prompt(s) is a separate, deliberate operational change.
