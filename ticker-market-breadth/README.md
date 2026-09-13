# ticker-market-breadth

Fetches the NYSE TICK breadth index's full available history from Yahoo Finance
(`yfinance`, symbol `^TICK`) and saves it as `_breadth/TICK.csv` inside the same
shared local ticker-history archive (`dca_ticker_paths.configured_ticker_history_dir()`,
the `ticker_data/` directory the rest of the ticker archive lives in) that every
per-ticker CSV is already stored in.

This is a sibling of `ticker-advisor/`, `ml-forecaster/`, and `wondrous-octopus/`: a
self-contained, third-party-dependency subproject with its own `requirements.txt`
and `tests/`, excluded from the core repo's stdlib-only ruff and coverage gates
(see the root `CLAUDE.md`).

## What this is

**Data only.** `_breadth/TICK.csv` is not read by `near_close_dca.py`, any of the
stream executors, `dca_runner.py`, or any other live code path in this repo --
nothing here is wired into DCA signal logic, sizing, or safety gates. It exists
purely as an archived market-breadth series alongside the equity/ETF price
history, for later analysis. If it's ever wanted as a live signal input, that's
a separate, deliberate change -- not something this script does on its own.

**Deliberately stored in a subdirectory, not alongside the per-ticker CSVs.**
`ARCHIVE_SUBDIRECTORY` (`"_breadth"`) puts this series under
`<history_dir>/_breadth/TICK.csv` instead of a top-level `TICK.csv`, for two
reasons found in review:

  * The documented daily archive-refresh flow enumerates every `{TICKER}.csv`
    DIRECTLY under the archive root and re-fetches each stem AS A YAHOO
    SYMBOL. That updater is external to this repo (not part of this
    checkout), so its exact filtering logic can't be verified from here --
    an earlier cut of this module relied on an underscore-prefixed top-level
    filename (`"_TICK.csv"`) on the unverifiable assumption that updater
    also skips underscore stems. A subdirectory is a structural guarantee
    instead: a top-level `*.csv` glob never descends into it, matching the
    archive's existing `_intraday/` subtree.
  * `ml-forecaster/features.py`'s and `wondrous-octopus/data.py`'s training
    pipelines assume every archived series is a positive asset price -- NYSE
    TICK is a signed breadth count that regularly goes negative, and pooling
    it in as an ordinary "ticker" would feed meaningless features into the
    pooled Ridge model behind live sizing adjustments. Their own
    `discover_available_tickers()` helpers glob only `*.csv` directly under
    the archive root too, so the subdirectory keeps this out of their
    default `--all-tickers-in-archive` discovery for the same structural
    reason.

See `tests/test_fetch_tick.py::test_archive_path_is_a_subdirectory_not_a_top_level_file`
for the regression test that pins this down.

## Install

```bash
cd ticker-market-breadth
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python fetch_tick.py
# or, to target a specific archive directory (matches DCA_TICKER_HISTORY_DIR):
python fetch_tick.py --history-dir /path/to/ticker_data
```

**This already runs automatically as part of the daily ticker-history refresh.**
`scripts/run_tickers_daily_update.py`'s `run_incremental_update()` (and
`scripts/run_ticker_maintenance.py update`, which calls the same function)
invokes this script as a best-effort SUBPROCESS step after the per-ticker
update, by default (`fetch_breadth_series=True` / no `--skip-breadth-fetch`
flag). It is a subprocess call, not an import: `scripts/run_tickers_daily_
update.py` is part of the core, stdlib-only-at-runtime `scripts/` tree
(CLAUDE.md: "no runtime third-party dependencies"), and calling out to a
separate `python ticker-market-breadth/fetch_tick.py` process keeps this
subproject's `yfinance`/`pandas` dependency out of that process entirely --
it's spawned exactly like the external per-ticker updater already is. Its
outcome is recorded under the daily-update summary's `"breadth_fetch"` key
(`{"status": "completed"|"failed"|"skipped", "detail": ...}`) and logged to
the same `_daily_update_log.txt`; a failure there (yfinance not installed,
network error, timeout, missing script) is never fatal to the per-ticker
update it's attached to. Run `python fetch_tick.py` directly (as shown above)
only for a one-off/manual refresh, or when debugging a `"failed"`/`"skipped"`
outcome from the daily run.

**Interpreter selection for the automatic subprocess call.** `scripts/
run_tickers_daily_update.py` normally runs under a plain, stdlib-only
interpreter that does NOT have this subproject's `yfinance`/`pandas`
installed, so launching the subprocess with that same interpreter would
usually fail with `ModuleNotFoundError`. `_default_breadth_fetch_python()`
auto-detects an executable `.venv` next to `fetch_tick.py` (i.e. this
subproject's own documented install path above) and prefers it when
present; set
`DCA_BREADTH_FETCH_PYTHON` (or pass `--breadth-fetch-python` to
`run_tickers_daily_update.py`) to point at a different interpreter
explicitly, e.g. one where these dependencies were installed some other way.

`yf.Ticker("^TICK").history(period="max", ...)` requests Yahoo's *entire*
available history for the symbol -- the same "all available data" range the
per-ticker archive downloader requests for equities -- not a fixed lookback
window. Yahoo's own TICK history is much shorter than most equities', so
`_breadth/TICK.csv` will not reach as far back as, say, `AAPL.csv`; that's a property
of what Yahoo has for this symbol, not a limitation of this script.

## Tests

```bash
python -m pytest tests/
```

No test makes a real network call: `fetch_tick_history()`'s yfinance download is
exercised only via a monkeypatched `yfinance` module; `write_tick_csv()` is
tested directly with a synthetic in-memory frame.
