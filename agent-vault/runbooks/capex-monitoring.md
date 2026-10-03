# Runbook: capex monitoring (quarterly)

STATUS: untested. Every threshold here is UNVERIFIED (`vault/Knowledge/threshold-review.md`).

1. Fill the watched list in `vault/Knowledge/capex-monitoring-kpis.md` first. The skill refuses to guess one.
2. `cd agent-vault && claude`, then ask: "Analyze the latest quarter's capex filings".
3. The agent follows `skills/analyze-capex/SKILL.md` and reads filings with read-only tools only.
4. Each extracted value carries its accession number or URL; a value it cannot find is `not found`, never estimated.
5. It flags changes beyond `capex_delta_flag_pct` and research additions beyond `capex_research_addition_usd_m`.
6. It logs to `vault/Daily/YYYY-MM-DD.md` (date name, never a quarter name) and updates the Knowledge note.
7. It closes with `reflect`; the commit summary starts `[capex-monitoring] YYYY-MM-DD <outcome>`.
8. After the first real run, review which thresholds fired and record the result in the threshold review note.
