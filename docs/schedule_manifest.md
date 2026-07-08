# Scheduled Tasks Manifest

Backup of the current scheduled tasks. Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

**Timezone note:** cron expressions are in **UTC**. ET equivalents below are for
US Eastern Daylight Time (EDT, UTC-4). Re-verify around DST transitions.

> **Afternoon completion rule:** all afternoon **trading and artifact-writing**
> tasks complete **no later than 3:40 PM ET**, when the read-only watchdog
> (`a46bf007`) begins its final verification. The watchdog's own post-3:40 PM
> verification and the Friday weekly Notion performance refresh at **5:00 PM ET**
> are the only approved post-3:40 PM DCA-related exceptions.

> **Schedule integrity note:** the live trading task runs at **3:20 PM ET**; the
> post-trade slippage / Notion sync runs at **3:35 PM ET**. These are two
> different tasks. Do not merge them or move the live trade off 3:20 PM ET.

> **Run-classification note:** the live task (`10ce362c`, the repaired task
> that supersedes the deleted `ce16ba6b`; artifact root stays under
> `.../ce16ba6b` for continuity) classifies every
> run as scheduled or manual. Every scheduled 3:20 PM ET run_state must be
> written with **both** `cron_id='10ce362c'` **and**
> `supersedes_cron_id='ce16ba6b'`; placeholder/ambiguous ids such as
> `ce16ba6b_replacement` are **forbidden**. Scheduled runs write under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` and update
> `latest_scheduled_run.json`; manual runs write under `manual_runs/<timestamp>/`
> and never touch that pointer. The slippage (`67aa802b`), watchdog
> (`a46bf007`), weekly (`041d9d90`), and monthly (`0656657a`) tasks consume only
> validated scheduled runs and exclude manual runs/order IDs. See
> [run_classification.md](run_classification.md).

> **Morning dry-run note:** a separate **dry-run / analytics-only** morning
> stream (`MorningDCAExecutor`) grades morning weakness vs. yesterday's close on
> its own **0x-to-3x** ladder and its own `morning` budget ledger, writing under
> `cron_tracking/morning_dca/YYYYMMDD_0945ET/` (run_type `morning_dca_dry_run`,
> `valid_afternoon_evidence=false`). It places **no trades**, never updates
> `latest_scheduled_run.json`, and is never afternoon scheduled evidence. See
> [morning_dca.md](morning_dca.md).
> **Base-amount note:** the graded DCA base amount is exactly **$3.00**; every
> candidate dollar amount is **`$3.00 × multiplier`**. The automation must never
> infer, discover, or fall back to **$50.00** or any broker/account default. If
> the base-amount config (`config/budgets.json`) is missing or unreadable,
> continue using **$3.00** and record
> `base_amount_source='hardcoded_user_confirmed_3_usd'`.

| ID | Name | Type | Trades | Cron (UTC) | ET (EDT) | Status |
|---|---|---|---|---|---|---|
| `b7961407` | Pre-market DCA buying-power dashboard | analytics | no | `00 13 * * 1-5` | 9:00 AM weekdays | active |
| `cd45e95b` | Compact DCA volatility outlier scan | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
| `morning-dca-0945` | Morning graded DCA dry-run analytics (`MorningDCAExecutor`) | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
| `e3e6f09e` | Monitor-only graded DCA signal check | monitoring | no | `5 19 * * 1-5` | 3:05 PM weekdays | active |
| `10ce362c` | Graded DCA automatic live test - $3 allocation (supersedes `ce16ba6b`) | trading | **yes** | `20 19 * * 1-5` | 3:20 PM weekdays | active |
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

### `morning-dca-0945` — Morning graded DCA dry-run analytics (`MorningDCAExecutor`)
- Analytics only, **no trading**. Runs **9:45 AM ET weekdays**.
- Evaluates morning price weakness vs. yesterday’s regular-session close on the **morning 0x-to-3x ladder** (`config/morning_dca.json`). Writes dry-run artifacts under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_0945ET/`.
- `stream='morning'`, `run_type='morning_dca_dry_run'`, `valid_afternoon_evidence=false`. Never writes into the afternoon `ce16ba6b` tree or updates `latest_scheduled_run.json`.

### `e3e6f09e` — Monitor-only graded DCA signal check
- Analytics and notifications only; **no trades**. Runs **3:05 PM ET weekdays**.
- Monitor-only signal check that surfaces the planned allocation and tier selection using the current $3 base 0x-to-5x ladder.

### `10ce362c` — Graded DCA automatic live test - $3 allocation (the only trading task)
- Repaired live task `10ce362c`, which **supersedes the deleted `ce16ba6b`**; the artifact root stays under `.../ce16ba6b` for continuity.
- Approved live trading task running the **$3 base 0x-to-5x ladder with tiered caps**. Places real market buy orders automatically within safeguards. Runs **3:20 PM ET weekdays**.
- Uses **only** Robinhood account `562057042`; Alpaca is read-only.
- Trigger gate: `programmatic_trigger_check.sh`, allowing weekdays **2026-07-02 through 2026-08-28**, **3:20–3:55 PM ET**.
- Writes standardized same-day state files under `/home/user/workspace/cron_tracking/ce16ba6b` ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)); must **not** fall back to interactive confirmation.
- **Run classification:** a scheduled run writes `scheduled_runs/YYYYMMDD_1520ET/run_state.json` (`run_origin='scheduled'`, `manual_run=false`, `scheduled_for_et='15:20'`, `state_schema_version='run-classification-v1'`, `run_id='scheduled_YYYYMMDD_1520ET'`, and **both** `cron_id='10ce362c'` **and** `supersedes_cron_id='ce16ba6b'`) and updates `latest_scheduled_run.json`; a manual run writes `manual_runs/<timestamp>/run_state.json` (`run_origin='manual'`, `manual_run=true`) and never updates that pointer. Placeholder/ambiguous ids such as `ce16ba6b_replacement` are forbidden; for continuity, consumers still accept `cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'` as valid evidence. See [run_classification.md](run_classification.md).
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
