"""Phase 0 outcome ledger, tax-lot ledger, trial registry and universe tables (research only).

Implements the PROPOSED ADR ``docs/adr-outcome-ledger-pit-harness.md`` (PR #2096) on the
existing append-only DuckDB price store, using the labelled proposed defaults in
``defaults.py`` for the ADR's open questions. Nothing here is operator-approved policy.

Suggestion-only and fail-closed, like ``market_data_ingest.prices``: no network, no broker
calls, no orders. Callers fetch rows (run artifacts, read-only broker tools) and hand plain
Python dicts to ``LedgerStore.ingest_*``. Nothing in the live DCA paths imports this.
"""
