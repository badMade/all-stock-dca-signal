# Scheduled Tasks Manifest

Backup of the current scheduled tasks. Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

**Timezone note:** cron expressions are in **UTC**. ET equivalents below are for
US Eastern Daylight Time (EDT, UTC-4). Re-verify around DST transitions.

> **Schedule integrity note:** the live trading task runs at **3:30 PM ET**;
> the post-trade slippage / Notion sync runs at **3:45 PM ET**. These are two
> different tasks. Do not move the live trade schedule to 3:45 — the 3:45 slot
> is analytics only.

| ID | Name | Type | Trades | Cron (UTC) | ET (EDT) | Status |
|---|---|---|---|---|---|---|
| `ce16ba6b` | Live graded DCA task | trading | **yes** | `30 19 * * 1-5` | 3:30 PM weekdays | active |
| `a46bf007` | Live-run watchdog | monitoring | no | `40 19 * * 1-5` | 3:40 PM weekdays | active |
| `67aa802b` | Post-trade slippage + compact Notion latest-run sync | analytics | no | `45 19 * * 1-5` | 3:45 PM weekdays | active |
| `cd45e95b` | Compact volatility outlier scan | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
| `041d9d90` | Weekly DCA Notion performance dashboard refresh | analytics | no | `0 21 * * 5` | Fri 5:00 PM | active |
| `0656657a` | Monthly graded DCA dashboard refresh | analytics | no | `45 12 1-7 * 1-5` | 1st weekday/mo 8:45 AM | active |
| `bec4a6ee` | Monthly DCA automation cost review | analytics | no | `55 13 1-7 * 1-5` | 1st weekday/mo (see note) | active |
| `f7411889` | One-time reminder: evaluate past Aug 28 | reminder | no | one-shot | 2026-08-15 11:45 AM | active |

## Task details

### `ce16ba6b` — Live graded DCA task (the only trading task)
- Approved live trading task. Places real market buy orders automatically within safeguards.
- Uses **only** Robinhood account `562057042`.
- Trigger gate: `programmatic_trigger_check.sh`, allowing weekdays **2026-07-02 through 2026-08-28**, **3:20–3:55 PM ET**.
- Writes the standardized same-day state file ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)); must **not** fall back to interactive confirmation.
- The task's stored display name may be stale (references 3:45 / Jul 10). The authoritative schedule is **3:30 PM ET weekdays**.

### `a46bf007` — Live-run watchdog
- Read-only, no trading. Alerts on missed run, error, or discrepancy.

### `67aa802b` — Post-trade slippage + compact Notion latest-run sync
- Analytics only. Reads live artifacts and Robinhood order history, writes slippage artifacts, updates the compact Notion **Latest Run** section only.

### `cd45e95b` — Compact volatility outlier scan
- Analytics only. Alerts only for **>5% outliers** or actionable errors; no full 41+ ticker narrative on no-outlier days.

### `041d9d90` — Weekly Notion performance dashboard refresh
- Analytics only. Refreshes the full Notion performance dashboard. No routine email.

### `0656657a` — Monthly graded DCA dashboard refresh
- Analytics only. No-op if the day is not the first weekday of the month.

### `bec4a6ee` — Monthly DCA automation cost review
- Analytics only. Usage proxies, no exact credits.
- **Known discrepancy:** requested 9:15 AM ET but observed `next_run` at 9:55 AM ET — flagged for reconciliation.

### `f7411889` — One-time reminder
- Reminder only. Fires 2026-08-15 11:45 AM ET to evaluate continuing live testing past 2026-08-28.
