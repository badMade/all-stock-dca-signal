"""Prediction-market ingestion, Phase 5a: Polymarket International and Polymarket US.

Research-only (docs/adr-prediction-markets.md D1, D5): nothing here is read by a live buying
slot, and tests/prediction_markets/test_pm_boundaries.py fails if a live-slot module imports
it. Nothing here is scheduled either; a snapshot job is only proposed in the ADR (§14).

Layout, one module per concern so each ADR decision has one home:

* ``config``          strict loader for ``dca-sizing-signals/config/prediction_markets.json``
* ``models``          the typed rows every adapter returns
* ``source``          ``PredictionMarketSource`` (D2) and the fail-closed ``run_ingestion`` (D6, D7)
* ``polymarket_intl`` Gamma / CLOB / Data API v2 adapter
* ``polymarket_us``   public gateway adapter, plus the injected-signer authenticated path (D12)
* ``throttle``        per-endpoint request budgets per 10 s (ADR §4)
* ``cadence``         default vs watchlist polling interval (D3)
* ``store``           DuckDB, append-only, point-in-time reads, license gate (D4, D7, D9)
* ``alignment``       daily value = last mid strictly before 16:00 ET
* ``composite``       cross-venue log-odds pool (D13)
"""
