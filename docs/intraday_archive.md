# Intraday Observation Archive

Every scheduled poll prices the whole active universe in order to grade it. This
archive keeps those prices — with the timestamps and volume behind them — in a
shape you can analyse later, instead of leaving them scattered one-run-at-a-time
under `cron_tracking/`.

It **supplements** the daily OHLCV archive; it does not replace or modify it.

```
ticker_data/AMD.csv            <- daily OHLCV, one row per trading day (unchanged)
ticker_data/_intraday/AMD.csv  <- this archive, one row per poll per trading day
```

Implementation: [`dca_intraday_archive.py`](../dca_intraday_archive.py).
Backfill: [`scripts/backfill_intraday_archive.py`](../scripts/backfill_intraday_archive.py).

## What gets recorded

Every ticker evaluated by every scheduled slot, on **every** run mode — the
read-only 9:45 pre-flight and watchlist scans included. A price observed by a
scan is no less real for having been observed by a run that places no orders.

| Slot (ET) | Stream | Cron |
| --- | --- | --- |
| 9:35 | `market_open` | `2ba19526` |
| 9:45 | `morning` (watchlist / pre-flight) | `bb4f6ef1` |
| 10:30 | `morning` (live) | `42173154` |
| 11:30 / 1:30 / 2:30 | `intraday` | `7be82b97` |
| 12:30 | `midday` | `a6239dae` |
| 3:30 | `afternoon` (near-close) | `10ce362c` |

A ticker that produced **no** price is still archived, as a row with an empty
`price`, its `action` of `NO_DATA`, and the provider warnings that explain why.
That the ticker was polled and returned nothing is itself data — a silently
missing row would be indistinguishable from a slot that never fired.

## Columns

Grouped by purpose; the on-disk order is `OBSERVATION_FIELDS` in
`dca_intraday_archive.py`. New columns are appended at the end, so a file
written by an older revision still parses — **read by column name, not index**.

**Which poll produced this row**
`trading_day`, `stream`, `slot`, `observed_at_et`, `observed_at_utc`, `run_id`,
`run_type`, `manual_run`, `source`

**The price it decided on**
`price`, `price_source` (`quote` or `completed_bar`), `price_as_of`

`price_as_of` is the timestamp of the *price*, which is not the timestamp of the
run. A 10:30 slot normally decides on a completed bar that closed at 10:25; the
run-level `generated_at` alone could never show that five-minute gap.

**What it was graded against**
`previous_close`, `previous_close_as_of`, `previous_low`, `drop_pct`

**Intraday OHLCV behind the price** — the part the daily archive cannot express
`bar_timestamp`, `bar_open`, `bar_high`, `bar_low`, `bar_close`, `bar_volume`,
`session_open`, `session_high`, `session_low`, `session_volume`, `bar_count`,
`interval_minutes`

`session_*` aggregate the 9:30-ET-to-decision-anchor window the executor already
fetched, so they add no provider calls. An empty `session_volume` means
*unknown*, never zero — a window with no usable bars writes nothing rather than
a misleading `0`.

**The live quote, when the feed returned one**
`quote_price`, `quote_time`

Recorded whether or not the quote was *used*. A quote rejected as stale or
out-of-window is exactly the case where knowing what the feed actually returned
is worth having later.

**What the strategy concluded**
`action`, `multiplier`, `data_source`, `warnings`

## Write semantics

- **Natural key:** `(trading_day, stream, slot, ticker, source)`. A retry,
  recovery rerun, or artifact rebuild of the same slot **replaces** that slot's
  row rather than appending a duplicate.
- **Sorted:** rows are kept ordered by `(trading_day, observed_at_utc, stream,
  slot, source)`, so each file reads top-to-bottom as that ticker's price path.
- **Atomic:** each ticker file is rewritten through `dca_fileio.atomic_write_text`.
- **Never fatal.** Archiving is observability. A failure is recorded on the
  run_state as `intraday_archive.error` and logged as a warning; it never raises.
  A Drive sync stall or a read-only mount must not fail a run that priced real
  money. Every executor archives *after* its other artifacts are written, so
  nothing a live run depends on is downstream of it.

Each run also writes its own `observations.csv` beside its `run_state.json`, so
a run directory stays self-describing even when the shared archive is
unavailable — and so a backfill can be rebuilt from run directories alone.

## Location

Default: `<ticker archive>/_intraday/`, following `DCA_TICKER_HISTORY_DIR`
wherever it points. On this Mac that resolves inside the managed Google Drive
`Tickers` folder, next to the daily history.

Override with `DCA_INTRADAY_ARCHIVE_DIR` to put it elsewhere entirely.

The `_intraday` name is underscore-prefixed to match the archive's existing
convention for entries that are not a `{TICKER}.csv` daily history
(`_delisted.txt`, `_update_progress.json`, ...). It is also a *subdirectory*, and
the daily updater enumerates the universe with a top-level `glob("*.csv")` — so
an intraday file can never be mistaken for a daily one, and the intraday tickers
can never widen the daily updater's universe.

## Backfilling history

The executors write the archive going forward. To recover polls that predate
that wiring, replay the `run_state.json` files already on disk:

```bash
python -m scripts.backfill_intraday_archive --dry-run          # report only
python -m scripts.backfill_intraday_archive                    # write
python -m scripts.backfill_intraday_archive --since 2026-07-01 # bounded
```

It reads `run_state.json` rather than the CSVs, because the run state carries
both the `signal_rows` and the `run_classification` (stream, slot, run id,
scheduled-vs-manual) needed to key an observation. Runs predating
`run_classification` fall back to the `.../<stream>/YYYYMMDD_HHMMET/` folder
layout rather than being dropped.

Backfilled rows will not have the price-provenance columns, which did not exist
when they were written; those land empty. Manual-override runs **are** included
and stay identifiable by `manual_run` — they are excluded from scheduled
*evidence*, but this archive is analytics, and they still observed real prices.

Re-running is safe: the same natural key means a second pass refreshes rather
than duplicates.

## Reading it back

```python
from dca_intraday_archive import read_ticker_observations

for row in read_ticker_observations("AMD"):
    print(row["observed_at_et"], row["price"], row["price_source"])
```

Or with pandas, for the intraday path of one ticker on one day:

```python
import pandas as pd

df = pd.read_csv("ticker_data/_intraday/AMD.csv", parse_dates=["observed_at_utc"])
day = df[df.trading_day == "2026-08-04"].sort_values("observed_at_utc")
print(day[["slot", "price", "drop_pct", "session_volume"]])
```

Joined against the daily archive, so each poll sits beside its own day's close:

```python
daily = pd.read_csv("ticker_data/AMD.csv", parse_dates=["Date"])
intraday = pd.read_csv("ticker_data/_intraday/AMD.csv", parse_dates=["trading_day"])
merged = intraday.merge(daily, left_on="trading_day", right_on="Date", how="left")
merged["vs_close_pct"] = (merged.price / merged.Close - 1) * 100
```

## What this is not

- **Not scheduled evidence.** Run classification, slippage, watchdog
  confirmation, fill counts, and cost basis are all unchanged and still read
  `run_state.json` / `submitted_orders.csv`. Nothing consumes this archive as
  proof a run fired.
- **Not a replacement for `raw/`.** Scheduled live runs still persist every raw
  connector response under `raw/` before computing, per the
  persist-before-compute contract. That is the audit record; this is the
  time series.
- **Not a new data feed.** Every value here comes from a call the run was
  already making. The archive adds no provider calls, no network traffic, and no
  API cost.
