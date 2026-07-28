# Reporting Workflow

How run artifacts flow into analytics and the Notion dashboard. Machine-readable
config: [`config/reporting.json`](../config/reporting.json). **No API tokens or
private links are stored in this repo.**

## Daily flow (weekdays, EDT)

1. **9:35 AM ET: Market-open live execution (`2ba19526`)**
   Places the **first buys of the day** (Robinhood `<ROBINHOOD_ACCOUNT_NUMBER>`) using the dynamic first-allocation base ($2 below prior close+low, $1 below prior close only) on the 0x-to-5x ladder. Writes `cron_tracking/market_open_dca/YYYYMMDD_0935ET/`.
2. **9:45 AM ET: Watchlist early-warning scan (`bb4f6ef1`)**
   Read-only. Alerts only for actionable early-warning rows. Supersedes the retired
   `cd45e95b` outlier scan and the retired `morning-dca-0945` alias -- and absorbed
   its required pre-flight-writer duty too, not just its display name.
3. **10:30 AM ET: Morning live execution (`42173154`)**
   Places buys and writes `cron_tracking/morning_dca/YYYYMMDD_1030ET/`.
4. **11:30 AM, 1:30 PM, 2:30 PM ET: Intraday live executions (`7be82b97`)**
   One task, three slots. Each run writes
   `cron_tracking/intraday_dca/YYYYMMDD_<HHMM>ET/` and records the slot that fired.
   The 2026-07-24 2:30 PM ET scheduled recovery (18 fills, $72.00) belongs to this task.
5. **12:30 PM ET: Midday live execution (`a6239dae`)**
   Places buys and writes `cron_tracking/midday_dca/YYYYMMDD_1230ET/`.
6. **3:05 PM ET: $2-base monitor-only signal check (`e3e6f09e`, PAUSED)**
   Analytics only. Currently **paused**, so it does not fire.
7. **3:30 PM ET: Final live graded DCA task (`10ce362c`, supersedes deleted `ce16ba6b`)**
   Places buys (Robinhood `<ROBINHOOD_ACCOUNT_NUMBER>`) and writes the standardized same-day
   **run state** artifact ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)).
   A **scheduled** run writes under
   `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/` with
   `run_classification.run_origin='scheduled'` and updates
   `latest_scheduled_run.json`. Manual runs write under `manual_runs/<timestamp>/`
   and never update that pointer. See
   [run_classification.md](run_classification.md).
8. **3:35 PM ET: Post-trade slippage + Notion sync (`67aa802b`)**
   Reads the **scheduled** run-state artifact and Robinhood order history,
   computes slippage
   ([`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)),
   writes slippage/coverage artifacts, and updates the **compact Notion "Latest
   Run" section only**. Scheduled-live rows exclude all manual order IDs and any
   `run_origin='manual'` / `manual_run=true` state; manual fills go only in the
   labeled **Manual Test Runs** section.
9. **3:40 PM ET: Watchdog (`a46bf007`)**
   Read-only, the final scheduled afternoon event. Confirms a valid **scheduled**
   run_state exists and that `latest_scheduled_run.json` points at it; flags
   missing/incomplete/error/discrepancy. Never infers a scheduled run from
   same-day broker orders or manual runs alone.

Every manual or scheduled run produces a **DCA Manual Run Slippage and Coverage
Report** where fills exist, or a compact run artifact explaining no fills.
`target_price` and slippage are tracked for every future trade, and daily live
runs write structured JSON/CSV locally first.

### Scheduled vs manual runs

All reporting honors the **run-classification contract**
([run_classification.md](run_classification.md),
[`config/run_classification.json`](../config/run_classification.json)):
scheduled-live cost basis, slippage, reliability, and order-count metrics are
computed only from validated scheduled runs, with every manual order ID excluded.
A broker time-window fallback (3:15–3:35 PM ET) is used only when a valid
scheduled state exists but lacks order IDs due to a known write failure — manual
IDs stay excluded. Manual fills appear only under a labeled **Manual Test Runs**
section and never affect scheduled-live metrics.

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
