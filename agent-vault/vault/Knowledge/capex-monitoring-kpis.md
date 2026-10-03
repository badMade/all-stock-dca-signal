# capex monitoring KPIs

STATUS: skeleton. Nothing in this note has been run. The `analyze-capex` skill reads it first.

## Watched companies

None provided yet. The skill stops and asks rather than inventing a list. Add one line per company when the
owner supplies them: ticker, the filer's own capex line label, fiscal year end.

## Thresholds

Live in `config.yaml`: `capex_delta_flag_pct` and `capex_research_addition_usd_m`. Both are `UNVERIFIED`
(they come from the original brief). See `threshold-review.md`.

## Baselines

| Company | Period end | Capex (unit) | Source (accession or URL) |
| --- | --- | --- | --- |
| (none yet) | | | |

## Sources the skill may read

Read-only only: Robinhood SEC filing tools (`get_sec_filing_index`, `get_sec_filing_facts`, `get_sec_filing`),
Fiscal.ai, Bigdata.com, a filing the owner supplies, and Notion for reading. No source, no number.

## Prior findings

None yet.
