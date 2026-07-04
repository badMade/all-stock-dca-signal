# Reporting Workflow

How run artifacts flow into analytics and the Notion dashboard. Machine-readable
config: [`config/reporting.json`](../config/reporting.json). **No API tokens or
private links are stored in this repo.**

## Daily flow (weekdays, EDT)

1. **3:30 PM ET — Live graded DCA task (`ce16ba6b`)**
   Places buys (Robinhood `562057042`) and writes the standardized same-day
   **run state** artifact ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)).
2. **3:40 PM ET — Watchdog (`a46bf007`)**
   Read-only. Verifies the run happened and flags missing/error/discrepancy.
3. **3:45 PM ET — Post-trade slippage + Notion sync (`67aa802b`)**
   Reads the run-state artifact and Robinhood order history, computes slippage
   ([`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)),
   and updates the **compact Notion "Latest Run" section only**.
4. **9:45 AM ET — Volatility outlier scan (`cd45e95b`)**
   Alerts only for >5% outliers or actionable errors.

## Periodic flow

- **Fridays 5:00 PM ET — Weekly dashboard (`041d9d90`)**: full Notion
  performance dashboard refresh. No routine email.
- **First weekday of month 8:45 AM ET — Monthly dashboard (`0656657a`)**:
  refresh; no-op otherwise.
- **First weekday of month — Cost review (`bec4a6ee`)**: usage proxies only
  ([`schemas/cost_tracker.schema.json`](../schemas/cost_tracker.schema.json)).

## Cost-control policy

- Daily tasks are **compact / local-first**.
- **Heavy analysis** is reserved for **Fridays and monthly**.
- **Avoid manual full runs** unless automation fails.

## Notion & Drive

- Notion dashboard: `https://app.notion.com/p/391202e5e677812f9ce4c9bc0904d412`
- A Google Drive report pack exists. Exact links may live in `cloud_links.json`
  in the workspace, which is **not committed** (it may contain private links).
- See [`notion_dashboard_template.md`](notion_dashboard_template.md) for the page
  structure and the conceptual Notion API sync (no credentials).
