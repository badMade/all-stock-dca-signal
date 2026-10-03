---
name: analyze-capex
description: Analyze quarterly capex from SEC filings for the watched companies and flag large changes. Use when the user says "analyze capex", "Q4 capex filings", or asks for a capex check. Read-only. UNTESTED.
---

# Analyze CapEx

One job: extract capex from the latest filings for the watched companies, compare it
with the last recorded values, and flag changes that cross the configured threshold.

STATUS: UNTESTED. Drafted from the setup brief and never run. Every threshold it uses
is `UNVERIFIED` (see `vault/Knowledge/threshold-review.md`). Treat the first run as a
trial and say so in the log.

> `vault/…` paths resolve against the vault base printed in the SessionStart header.

## Steps

1. **Read** `vault/Knowledge/capex-monitoring-kpis.md`: the watched companies, the
   baseline values, and the prior findings. If the watched list is empty, stop and ask
   the user for it; never invent one.
2. **Fetch filings with read-only tools only.** The Robinhood SEC filing tools
   (`get_sec_filing_index`, `get_sec_filing_facts`, `get_sec_filing`), Fiscal.ai or
   Bigdata.com, or a filing the user supplies. Notion may be read, never written.
3. **Extract** the capex line (purchases of property and equipment, or the filer's own
   label) with period end, value and unit. Record the accession number or URL beside each
   value. If a value cannot be found, write `not found`; do not estimate.
4. **Compare** with the baseline in the note. Flag a company when the change exceeds
   `capex_delta_flag_pct` in `config.yaml`, and flag research additions above
   `capex_research_addition_usd_m`. State that `capex_delta_flag_pct` is verified (2026-10-03
   baseline) and `capex_research_addition_usd_m` is still `UNVERIFIED`.
5. **Log** under today's `## Sessions` in `vault/Daily/YYYY-MM-DD.md`: companies read,
   values, deltas, flags, sources. Name the file by date only, never by quarter.
6. **Capture** new baselines and flagged items into the Knowledge note with the
   `capture` skill.
7. **Close** with the `reflect` skill. Start its commit summary with
   `[capex-monitoring] YYYY-MM-DD <outcome>`.

## Rules

- Read-only. No Notion writes, no order tools, no files outside `vault/`.
- Every number carries its source. No source, no number.
- Do not change a threshold here; propose it through `reflect`.
