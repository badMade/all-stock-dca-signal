# Scheduled Tasks Manifest

Backup of the current scheduled tasks. Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

**Timezone note:** cron expressions are in **UTC**. ET equivalents below are for
US Eastern Daylight Time (EDT, UTC-4). Re-verify around DST transitions.

> **Afternoon completion rule:** all afternoon tasks complete **no later than
> 3:40 PM ET**, except the Friday weekly Notion performance refresh at
> **5:00 PM ET** (the only approved post-3:40 PM DCA-related exception).

> **Schedule integrity note:** the live trading task runs at **3:20 PM ET**; the
> post-trade slippage / Notion sync runs at **3:35 PM ET**. These are two
> different tasks. Do not merge them or move the live trade off 3:20 PM ET.

> **Run-classification note:** the live task (`ce16ba6b`) classifies every
> run as scheduled or manual. Scheduled runs write under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` and update
> `latest_scheduled_run.json`; manual runs write under `manual_runs/<timestamp>/`
> and never touch that pointer. The slippage (`67aa802b`), watchdog
> (`a46bf007`), weekly (`041d9d90`), and monthly (`0656657a`) tasks consume only
> validated scheduled runs and exclude manual runs/order IDs. See
> [run_classification.md](run_classification.md).

| ID | Name | Type | Trades | Cron (UTC) | ET (EDT) | Status |
|---|---|---|---|---|---|---|
| `b7961407` | Pre-market DCA buying-power dashboard | analytics | no | `00 13 * * 1-5` | 9:00 AM weekdays | active |
| `cd45e95b` | Compact DCA volatility outlier scan | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
| `e3e6f09e` | Monitor-only graded DCA signal check | monitoring | no | `5 19 * * 1-5` | 3:05 PM weekdays | active |
| `ce16ba6b` | Graded DCA automatic live test - $3 allocation | trading | **yes** | `20 19 * * 1-5` | 3:20 PM weekdays | active |
| `d72aa525` | Capital-priority briefing | analytics | no | `25 19 * * 1-5` | 3:25 PM weekdays | active |
| `67aa802b` | Post-trade slippage + compact Notion latest-run sync | analytics | no | `35 19 * * 1-5` | 3:35 PM weekdays | active |
| `a46bf007` | Live-run watchdog | monitoring | no | `40 19 * * 1-5` | 3:40 PM weekdays | active |
| `041d9d90` | Weekly DCA Notion performance dashboard refresh | analytics | no | `0 21 * * 5` | Fri 5:00 PM | active |
| `0656657a` | Monthly graded DCA dashboard refresh | analytics | no | `45 12 1-7 * 1-5` | 1st weekday/mo 8:45 AM | active |
| `bec4a6ee` | Monthly DCA automation cost review | analytics | no | `55 13 1-7 * 1-5` | 1st weekday/mo 9:55 AM | active |
| `f7411889` | One-time reminder: evaluate past Aug 28 | reminder | no | one-shot | 2026-08-15 11:45 AM | active |

## Task details

### `b7961407` — Pre-market DCA buying-power dashboard
- Analytics only, **no trading**. Runs **9:00 AM ET weekdays**.
- Pre-market buying-power dashboard using the current **$3 base 0x-to-5x ladder**.

### `cd45e95b` — Compact DCA volatility outlier scan
- Analytics only, **no trading**. Runs **9:45 AM ET weekdays**.
- Scans only **>5% outliers**; no full 41+ ticker narrative on no-outlier days. Uses the current $3 base 0x-to-5x ladder.

### `e3e6f09e` — Monitor-only graded DCA signal check
- Analytics and notifications only; **no trades**. Runs **3:05 PM ET weekdays**.
- Monitor-only signal check that surfaces the planned allocation and tier selection using the current $3 base 0x-to-5x ladder.

### `ce16ba6b` — Graded DCA automatic live test - $3 allocation (the only trading task)
- Approved live trading task running the **$3 base 0x-to-5x ladder with tiered caps**. Places real market buy orders automatically within safeguards. Runs **3:20 PM ET weekdays**.
- Uses **only** Robinhood account `562057042`; Alpaca is read-only.
- Trigger gate: `programmatic_trigger_check.sh`, allowing weekdays **2026-07-02 through 2026-08-28**, **3:20–3:55 PM ET**.
- Writes standardized same-day state files under `/home/user/workspace/cron_tracking/ce16ba6b` ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)); must **not** fall back to interactive confirmation.
- **Run classification:** a scheduled run writes `scheduled_runs/YYYYMMDD_1520ET/run_state.json` (`run_origin='scheduled'`, `run_id='scheduled_YYYYMMDD_1520ET'`) and updates `latest_scheduled_run.json`; a manual run writes `manual_runs/<timestamp>/run_state.json` (`run_origin='manual'`, `manual_run=true`) and never updates that pointer. See [run_classification.md](run_classification.md).
- **Manual runs do not count as scheduled-run evidence.** Any stale stored display name is superseded by the authoritative **3:20 PM ET** schedule.

### `d72aa525` — Capital-priority briefing
- Analytics only, **no trading**. Runs **3:25 PM ET weekdays**.
- Uses the current $3 base 0x-to-5x ladder. Sends **email/in-app notification only if at least one ticker triggers 1.5x or higher**; otherwise silent.

### `67aa802b` — Post-trade slippage + compact Notion latest-run sync
- Analytics only, **no trading**. Runs **3:35 PM ET weekdays**.
- Reads the **scheduled** live-run state and Robinhood order history, writes slippage/coverage artifacts, updates the compact Notion **Latest Run** section only.
- **Run classification:** scheduled-live rows exclude all manual order IDs and any `run_origin='manual'`/`manual_run=true` state; manual fills go only in the labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).

### `a46bf007` — Live-run watchdog
- Read-only, **no trading**. Runs **3:40 PM ET weekdays** — the **final scheduled afternoon event**.
- Alerts only on missed, incomplete, error, or discrepancy conditions.
- **Run classification:** validates that a scheduled run_state exists and `latest_scheduled_run.json` points at it; a manual-only or broker-orders-only day is a discrepancy, not a scheduled run.

### `041d9d90` — Weekly Notion performance dashboard refresh
- Analytics only, **no trading**. Runs **Fridays 5:00 PM ET** — the only approved post-3:40 PM exception.
- Recalculates and refreshes the full Notion performance dashboard. No routine email.
- **Run classification:** weekly scheduled-live metrics use only validated scheduled runs with manual runs/order IDs excluded; manual fills, if shown, are confined to a labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).

### `0656657a` — Monthly graded DCA dashboard refresh
- Analytics only, **no trading**. First weekday of the month **8:45 AM ET**. No-op otherwise.

### `bec4a6ee` — Monthly DCA automation cost review
- Analytics only, **no trading**. First weekday of the month **9:55 AM ET**.
- Reports **usage proxies only**; exact Perplexity credit consumption is not exposed, so do not claim exact credits.

### `f7411889` — One-time reminder
- Reminder only, **no trading**. Fires **2026-08-15 11:45 AM ET** to evaluate continuing live testing past 2026-08-28.
