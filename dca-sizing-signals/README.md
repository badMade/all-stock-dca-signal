# dca-sizing-signals

Independent sizing-suggestion producers for the graded DCA strategy, each optionally
publishing a bounded per-ticker multiplier adjustment that `near_close_dca.graded_buy_multiplier()`
may read, and none of them ever able to write to live sizing directly. A sibling of
`ml-forecaster/`, `wondrous-octopus/`, and `ticker-advisor/`: a self-contained subproject
with its own `pyproject.toml` and `tests/`, following the same conventions.

## What this is

Three packages that only depend on each other in one direction, enforced by both the
type system and a CI test, not by convention alone:

| Package | Role |
| --- | --- |
| `dca_shared_interfaces/` | The neutral contracts (`ForecastProvider`, `SizingSuggester`, `SizingSuggestion`) and the reusable fail-closed guarded-publish base class. Depends on nothing else in this repo. |
| `connector_signals/` | A concrete `SizingSuggester` sourced from Robinhood-connector-obtained market data. Implements `dca_shared_interfaces.forecasting.SizingSuggester`. |
| `market_data_ingest/` | The one place in this repo allowed to make network calls: fetches fundamentals, options/IV, index, and earnings data from external sources and writes versioned, point-in-time-stamped local files. `ml_forecaster/` and `wondrous_octopus/` never import this -- only `connector_signals/` does. |

## What this does NOT do

- Does not write to live DCA sizing. `SizingSuggester.suggest()` returns an immutable
  `SizingSuggestion`; nothing in this package holds a reference to `near_close_dca` or any
  live engine state. Enforced by `tests/test_architecture_boundaries.py`, which fails if
  any package here imports `near_close_dca`, `ml_forecaster`, or `wondrous_octopus`.
- Does not import `ml_forecaster/` or `wondrous_octopus/`, and is not imported by them.
  `wondrous_octopus/` may optionally implement `ForecastProvider` from
  `dca_shared_interfaces` for future ensembling; it deliberately does not implement
  `SizingSuggester`, matching its own README's "does not influence live DCA sizing."
- `edgar_fundamentals.py` and `dolthub_options.py` under `market_data_ingest/` do not exist
  yet. Their tests (`tests/test_edgar_fundamentals.py`, `tests/test_dolthub_options.py`) are
  written and currently fail on import, per this repo's TDD-first policy -- built from
  live-verified fixtures (SEC EDGAR CIK0000320193 companyfacts, DoltHub
  post-no-preference/options), not fabricated. Implementation is pending explicit
  approval; do not write it without that.
- Does not integrate SimFin or Sharadar. Both require a registered API key this build
  environment did not have; SEC EDGAR alone already covers free, unregistered,
  point-in-time fundamentals history, so they're deferred, not abandoned.

## Where sizing_adjustment_reader.py goes

Shipped alongside this package as `core-repo-additions/live_dca_side/sizing_adjustment_reader.py`,
but deliberately NOT part of it. It belongs next to `near_close_dca.py` in the core repo,
since it's the one module allowed to know more than one `SizingSuggester` exists --
combining their independent output files is its job, not either suggester's.

## Install

```bash
cd dca-sizing-signals
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/
```

## Status

Architecture and boundary tests are real and passing. `edgar_fundamentals.py` and
`dolthub_options.py` are test-first stubs awaiting implementation approval -- see "What
this does NOT do." Built across a chat session with no repo access; packaged for handoff
to Claude Code for actual integration into `all-stock-dca` and any git operations, since
that environment has the credentials and repo access this one didn't.
