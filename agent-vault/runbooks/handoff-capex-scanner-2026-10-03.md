# Handoff: CapEx delta scanner (2026-10-03)

Written to move a cloud session to a local one. Read this, then the three files under "Inputs".
Nothing here is live trading authorization. The scanner is read-only research tooling.

## State

- Scanner implementation completed in `agent-vault/scripts/scan_capex_deltas.py` with test suite `tests/test_scan_capex_deltas.py` (commit `d90435ec`).
- Two-tier rule ($2,000M hyperscalers, $250M suppliers) and 15% delta thresholds verified test-first across 10 test cases.
- Also implemented and tested: year-to-date cash flow differencing (with accession provenance), CIK mapping from `company_tickers.json`, and request pacing.
- Live SEC fetching completed: fetch_sec_company_tickers and fetch_company_facts_from_sec support live EDGAR ingestion (--live) with SECRateLimiter pacing (10 req/s), contact User-Agent, and SSRF host protection.

## Inputs (all on `main`)

- `agent-vault/vault/Knowledge/capex-monitoring-kpis.md`: 51 operating lines (52 tickers, GOOGL and GOOG share a line), 15 non-operating vehicles, 8 baselines.
- `agent-vault/vault/Knowledge/threshold-review.md` and `agent-vault/config.yaml`: 15% delta; $2,000M for hyperscalers (over $5B), $250M for suppliers and mid-caps. Only the $2,000M value is a config key (`capex_research_addition_usd_m`). The $250M tier exists in prose only.
- The 67 symbols match `config/tickers.json` (65 active + 2 whole-share) exactly.

## Decisions needed before the tests can be written

| # | Question | Suggestion (yours to overrule) |
| --- | --- | --- |
| 1 | Flag when the 15% delta AND the tier dollar threshold both trip, or when either does? | AND. For hyperscalers 15% is already billions, so AND mostly filters small suppliers where 15% is noise. |
| 2 | Is "over $5B" quarterly or annual spend? | Quarterly. `threshold-review.md` says "quarterly spend" and the baseline table is quarterly. The `config.yaml` comment just says "capex > $5B". |
| 3 | The 43 companies with no baseline: record one on first scan, or report "no baseline"? | Record and flag nothing, and say so in the report. The skill rule is "no source, no number." |
| 4 | TSM, ASML, STM (Form 20-F, annual only) and CBRS, SPCX (file labels them private)? | Skip with an explicit "not scannable" line. CBRS and SPCX are listed as traded on Robinhood, so the file labels look stale. Check before trusting them. |

## Known problems to design around

- Baselines mix quarterly, YTD and annual figures, and USD, NT$ and EUR. Nothing has been checked against EDGAR.
- 10-Q cash flow facts are year-to-date, so a quarter needs differencing.
- The "Thresholds" section of `capex-monitoring-kpis.md` still says `UNVERIFIED`; `config.yaml` says verified or settled.
- `taste.md` says "59 tickers"; the universe is 67.
- `dl-trading-backtests.md` gives LSTM mean Sharpe as 3.56 and 3.26 without saying why. Not re-run.

## Constraints

- Standard library only. No new dependency.
- Pure core (filings in, flags out). The EDGAR fetch is an injected callable, so tests run offline.
- EDGAR needs a contact `User-Agent` and a rate limit (SEC asks for no more than 10 requests per second).
- Dry-run only. No order tools. Vault sessions deny Notion writes.
- Any threshold change goes through `reflect`, not a direct edit.

## First commands locally

```bash
git pull origin main
.venv/bin/python -m pytest tests/test_agent_vault.py -q
```

Then answer the four decisions above, and the tests come next.
