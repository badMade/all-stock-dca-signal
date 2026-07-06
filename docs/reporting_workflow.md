# Reporting Workflow

How run artifacts flow into analytics and the Notion dashboard. Machine-readable
config: [`config/reporting.json`](../config/reporting.json). **No API tokens or
private links are stored in this repo.**

## Daily flow (weekdays, EDT)

1. **9:00 AM ET — Pre-market buying-power dashboard (`b7961407`)**
   Analytics only. Pre-market buying-power view using the $3 base 0x-to-5x ladder.
2. **9:45 AM ET — Volatility outlier scan (`cd45e95b`)**
   Alerts only for >5% outliers or actionable errors.
3. **3:05 PM ET — Monitor-only signal check (`e3e6f09e`)**
   Analytics only. Surfaces the planned allocation and tier selection.
4. **3:20 PM ET — Live graded DCA task (`ce16ba6b`)**
   Places buys (Robinhood `562057042`) and writes the standardized same-day
   **run state** artifact ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)).
5. **3:25 PM ET — Capital-priority briefing (`d72aa525`)**
   Analytics only. Emails/notifies **only if at least one ticker triggers 1.5x or higher**.
6. **3:35 PM ET — Post-trade slippage + Notion sync (`67aa802b`)**
   Reads the run-state artifact and Robinhood order history, computes slippage
   ([`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)),
   writes slippage/coverage artifacts, and updates the **compact Notion "Latest
   Run" section only**.
7. **3:40 PM ET — Watchdog (`a46bf007`)**
   Read-only, the final scheduled afternoon event. Verifies the run happened and
   flags missing/incomplete/error/discrepancy.

Every manual or scheduled run produces a **DCA Manual Run Slippage and Coverage
Report** where fills exist, or a compact run artifact explaining no fills.
`target_price` and slippage are tracked for every future trade, and daily live
runs write structured JSON/CSV locally first.

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
