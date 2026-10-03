# Spec: CapEx delta scanner (2026-10-03)

Picks up `handoff-capex-scanner-2026-10-03.md`. Read-only research tooling, not trading authorization. The four
decisions below adopt the handoff's suggestions. Status: the pure core (`agent-vault/scripts/scan_capex_deltas.py`,
`tests/test_scan_capex_deltas.py`) is implemented: threshold evaluation, quarter differencing, CIK parsing, request
pacing, URL validation and Markdown reporting. Live SEC fetching is still a stub that returns nothing.

## Decisions

| # | Decision | Effect on tests |
| --- | --- | --- |
| 1 | Flag only when the 15% quarter-over-quarter delta AND the tier dollar threshold both trip. | One case per side of each gate; a delta that trips only one gate is not flagged. |
| 2 | "Over $5B" means quarterly capex (prior quarter, the baseline). | Tier is chosen from the prior quarter's spend; 5,000M exactly is the lower tier. |
| 3 | A company with no baseline gets one recorded on first scan and is never flagged. The report says so. | "Baseline recorded, nothing flagged" is a distinct report state. |
| 4 | TSM, ASML, STM (20-F, annual only) and CBRS, SPCX are skipped with a "not scannable" line and a reason. | Skip is a result, not an error. |

Item 4 caveat: CBRS and SPCX are labelled private in `capex-monitoring-kpis.md` but are traded here, so the labels
look stale. Verify before relying on them; until then they are skipped, not scanned.

## Scope arithmetic

51 operating lines, minus 5 not scannable = 46. Of those, 6 have a baseline (NVDA, MSFT, GOOGL, AMZN, AMAT, AVGO) and
40 are record-only on first scan. Only MSFT, GOOGL and AMZN baselines are single quarters; NVDA, AMAT and AVGO are
YTD, so they need differencing before they can be compared. The 15 non-operating vehicles are never scanned.

## Contract

- Input: per-ticker quarterly capex facts `(period_end, amount, unit, source)` plus the stored baseline.
- Output per ticker, exactly one of: `flagged`, `passed`, `baseline_recorded`, `not_scannable`, `not_found`.
- Flag test: `delta_pct > capex_delta_flag_pct` and `delta_usd_m >= tier_threshold`, signed. The dollar gate is an
  addition threshold, so a decrease never flags, however large (a 20% cut is `passed`). Hyperscaler tier
  (prior quarter over $5,000M) uses `capex_research_addition_usd_m`; the $250M suppliers and mid-caps tier is not a
  config key yet (see Open items).
- Every number carries its accession or URL; a differenced quarter cites both input filings, and an input fact
  without one is dropped. No source, no number: a missing value is `not_found`, never estimated.
- 10-Q cash-flow facts are year-to-date; a quarter is `YTD(n) - YTD(n-1)`, and Q4 is `FY - 9-month YTD`.
- Non-USD figures are not compared to USD baselines. Convert or skip, and state which.

## Constraints (unchanged from the handoff)

Standard library only. Pure core (filings in, flags out); the EDGAR fetch is an injected callable so tests run
offline. Contact `User-Agent` and at most 10 requests per second. Dry-run only, no order tools, Notion writes denied.
Threshold changes go through `reflect`, not a direct edit.

## Open items, none blocking the tests

- Add a config key for the $250M tier (through `reflect`); until then the test fixture supplies it.
- `capex-monitoring-kpis.md` "Thresholds" text reconciled 2026-10-03 to record settled two-tier rule matching `config.yaml`.
- `taste.md` says 59 tickers; the universe is 67. `dl-trading-backtests.md` gives LSTM Sharpe as 3.56 and 3.26
  without saying why. Neither re-checked.
- No baseline has been checked against EDGAR.

## Next

Implement the live SEC fetch as an injected callable (`fetch_company_facts_offline_or_sec` is the stub), tests first
per the TDD-first rule in `AGENTS.md`, then the weekly report automation.
