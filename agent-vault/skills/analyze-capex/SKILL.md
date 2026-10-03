---
name: analyze-capex
description: Analyze quarterly capex from SEC filings for watched companies and flag large changes. Use when the user says "analyze capex", "Q4 capex filings", or asks for a capex check. Read-only. Operational.
---

# Analyze CapEx

One job: extract capex from filings for watched companies, compare with baselines,
and flag breaches under settled two-tier addition rules ($2,000M hyperscalers, $250M suppliers)
and the 15% quarter-over-quarter delta rule.

STATUS: OPERATIONAL. Transitioned from UNTESTED status with UNVERIFIED thresholds following implementation in `scripts/scan_capex_deltas.py` and `scripts/run_weekly_capex_scan.py` with 26 unit tests in `tests/test_scan_capex_deltas.py`. Thresholds are formally settled (see `vault/Knowledge/threshold-review.md`).

> `vault/…` paths resolve against the vault base printed in the SessionStart header.

## Steps

1. **Read** `vault/Knowledge/capex-monitoring-kpis.md`: 51 watched operating companies,
   established baseline values, and prior findings.
2. **Execute scanner** via command line or injected functions:
   - Offline check: `python3 scripts/scan_capex_deltas.py --report`
   - Live SEC EDGAR ingest: `python3 scripts/scan_capex_deltas.py --live --report`
   - Weekly runner: `python3 scripts/run_weekly_capex_scan.py --live`
3. **Verify Decision 1 AND gate**: a company is flagged only if BOTH the percentage change
   exceeds `capex_delta_flag_pct` (15%) AND the addition exceeds the tier limit ($2,000M for
   hyperscalers over $5,000M spend, $250M for suppliers and mid-caps).
4. **Enforce provenance**: every extracted value carries its accession number or URL.
   If unavailable, return `not_found`; never estimate. Form 20-F annual filers are `not_scannable`.
5. **Log** under today's `## Sessions` in `vault/Daily/YYYY-MM-DD.md`: companies evaluated,
   tier, deltas, flags, accession provenance.
6. **Capture** new baselines and flagged items into the Knowledge note with the `capture` skill.
7. **Close** with the `reflect` skill. Start its commit summary with
   `[capex-monitoring] YYYY-MM-DD <outcome>`.

## Rules

- Read-only. No Notion writes, no order tools, no files outside `vault/`.
- Every number carries its source. No source, no number.
- Do not change a threshold here; propose it through `reflect`.
