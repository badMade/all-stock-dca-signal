# yfinance: where it is allowed, and the plan to replace it

**Status: plan, nothing implemented.** Written 2026-10-18 from the owner's direction that day.

## The rule

- yfinance **may** feed any scheduled task that does **not** place orders: the daily ticker-history
  archive refresh, the `^TICK` breadth fetch, `ticker-advisor`, `ml-forecaster` and the research trees.
- yfinance **may not** feed any scheduled **buying** slot (09:35, 10:30, 11:30, 12:30, 13:30, 14:30,
  15:30). Those use Robinhood (and the Finance connector for 15:30) and abort with nothing placed when
  the primary is unusable. `tests/test_yfinance_retired.py` enforces this half, and the candidate
  generators reject a `yahoo_finance_yfinance` source as an unsupported key (692b5887, 2026-10-12).

The 2026-10-12 removal left the non-buying users alone on purpose, so nothing needs restoring there:
the package was only absent from venvs that never installed it.

## Declared once, installed everywhere

`pip install -e '.[data]'` or `pip install -r requirements-data.txt` (both `yfinance>=1.7`); `setup.sh`
now runs the second. The core strategy's stdlib-only runtime invariant is unchanged: this is an extra, not
a runtime dependency. The console's Environment page reads it as `yfinance: Installed` once the venv has it.

## Who uses it today (verified by grep, 2026-10-18)

| User | What it pulls | Buying path? |
| --- | --- | --- |
| `scripts/run_tickers_daily_update.py` -> external `incremental_update.py` (the Drive archive updater) | Daily OHLCV for the whole archive | No |
| `ticker-market-breadth/fetch_tick.py` | `^TICK` NYSE breadth index history | No |
| `ticker-advisor/pricing.py` | History for candidate tickers not yet archived | No |
| `ml-forecaster/features.py` | Reads the archive; its docstring says it never downloads live | No |
| `trading-strategies/`, `yfinance-prefetch/` | Research; prefetch is unscheduled since 692b5887 | No |

Everything the archive feeds (per-ticker calibration, `ml-forecaster`, `wondrous-octopus`, the market graph)
is downstream of the first row, so that is the dependency that matters.

## Replacement plan

**Goal:** the archive refresh and the other non-buying users stop depending on one unofficial scraper, with
no gap in daily coverage.

1. **Put a seam in front of it.** One `DailyBarSource` interface (`fetch(ticker, start, end) -> rows`) with
   yfinance as the first implementation. The archive updater and `ticker-advisor` call the seam, never the
   package. No behaviour change; this is the step that makes every later one cheap.
2. **Write the acceptance test before choosing a vendor.** Run a candidate and yfinance side by side over
   the active universe for 20 trading days and require: same trading-day set, adjusted-close agreement within
   a stated tolerance, split/dividend handling that matches the archive's existing columns, and share-class
   symbols (`BRK-B` vs `BRK.B`) resolving without special cases.
3. **Evaluate candidates against that test** (terms, rate limits, coverage and cost all need checking
   before anyone commits; none is assumed to pass): the Robinhood MCP historicals (only reachable from an
   agent session, as `docs/robinhood_payload.md` explains, so it fits a Mac-side job rather than repo code),
   a paid daily-bars API, and Stooq-style free CSV endpoints. Prefer one with an official API and written terms.
4. **Run both for two weeks in shadow.** The new source writes a parallel file; a drift report diffs it
   against the archive. Switch the archive's source only when the report is clean, and keep yfinance as the
   second implementation behind the seam for a rollback.
5. **Handle `^TICK` separately.** It is an index feed that few vendors carry. Decide whether it is still
   used by anything that matters (`ticker-market-breadth/README.md` says data only), and either find a
   source, rebuild it from constituents, or retire it.
6. **Retire yfinance** from `[project.optional-dependencies].data` only after step 4 holds and `^TICK` is resolved.

## Decisions needed from the owner

- Is paying for a daily-bars API acceptable, or must the replacement be free?
- Is `^TICK` worth keeping?
- Is the external Drive `incremental_update.py` in scope for change, given it sits outside this repo?
