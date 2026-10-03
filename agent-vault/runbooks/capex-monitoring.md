# Runbook: capex monitoring (recurring)

STATUS: operational. Thresholds previously flagged as UNVERIFIED are now formally settled (`vault/Knowledge/threshold-review.md`) and verified by test suite `tests/test_scan_capex_deltas.py`.

1. Watched filers are maintained in `vault/Knowledge/capex-monitoring-kpis.md` (51 operating filers, 15 non-operating exclusions, 8 verified baselines).
2. Execute the scanner tool:
   - Offline dry-run: `python3 scripts/scan_capex_deltas.py --report`
   - Live SEC EDGAR ingest: `python3 scripts/scan_capex_deltas.py --live --report`
   - Recurring runner: `python3 scripts/run_weekly_capex_scan.py --live`
3. The scanner enforces the Decision 1 AND gate: flags only when QoQ delta exceeds 15% AND dollar addition exceeds tier limit ($2,000M hyperscalers, $250M suppliers).
4. Each extracted value carries its accession number provenance; missing data returns `not_found`, never an estimate. Form 20-F annual filers (TSM, ASML, STM) and private entities are marked `not_scannable`.
5. Run reports are written in Markdown to `reports/capex-scan-YYYY-MM-DD.md` and summarized in `vault/Daily/YYYY-MM-DD.md`.
6. Close the session with `reflect`; the commit summary starts `[capex-monitoring] YYYY-MM-DD <outcome>`.
