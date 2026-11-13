"""Overnight futures gap forecast: observe-only research package.

Accepted design: docs/adr-overnight-futures-tracker.md. Phase 1 ships this empty
package so the architecture-boundary rules bind before any code lands (ADR
implementation plan, step 1).

What this package will hold (D3): FairValueCalculator, FuturesOvernightTracker,
CatalystCalendar and OvernightGapForecastProvider -- pure computation over rows the
caller passes in, the same shape as comovement_signals. Ingestion and the DuckDB
store belong in market_data_ingest/futures/, never here.

What it must never become (D2): a SizingSuggester. It implements ForecastProvider
only, publishes no suggestion file, and nothing on the live side imports it -- not
even sizing_adjustment_reader.py. tests/test_architecture_boundaries.py enforces all
three. Wiring it into sizing needs its own later ADR after the D8 evaluation gate.
"""
