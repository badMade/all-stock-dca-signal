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

> **3:20 PM state-file guarantee:** the live task (`10ce362c`) **always** writes
> a scheduled `run_state.json` under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` and updates
> `latest_scheduled_run.json` every time it fires, **even when no order is
> placed**. If Robinhood review passes but submission is blocked by required
> post-review user confirmation, the run_state stays valid and carries
> `order_submit_status='not_submitted_confirmation_required'`, `reviewed_order`
> details where available, candidate details, and **no `order_id`**. That is
> valid scheduled-run evidence the task ran — **not** evidence of an order/fill.
> See [run_classification.md](run_classification.md).

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

> **Same-day top-up guard:** a morning/manual run must **never block** the
> scheduled 3:20 PM ET run. Instead the scheduled run reads earlier same-day
> morning/manual allocations (as prior-allocation context only — never as
> scheduled evidence, slippage, watchdog confirmation, scheduled cost basis, or
> scheduled fill counts) and, per ticker, buys only the **incremental** dollar
> difference between the afternoon desired amount and the amount already bought,
> and only when the afternoon decision price is **strictly below** the earlier
> run's reference price. Otherwise it buys **$0** for that ticker. Only earlier
> runs generated **before 2:30 PM ET** contribute; scheduled runs are excluded.
> The scheduled run still writes a valid scheduled run_state even when every buy
> is fully offset to $0. See [strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).
>
> **Morning two-stream note:** the morning `MorningDCAExecutor` runs on its own
> **0x-to-3x** ladder and its own `morning` budget ledger in **two** modes.
> (1) A **9:45 AM ET read-only watchlist / early-warning scan**
> (`morning-dca-0945`, run_type `morning_dca_watchlist_scan`,
> `state_schema_version='morning-watchlist-v1'`, **no trades**) writing under
> `cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/`; it may surface early
> candidates but every amount is **hypothetical / watchlist-only**.
> (2) A **10:30 AM ET live Robinhood execution** (`42173154`, run_type
> `morning_dca_live`, `state_schema_version='morning-live-v1'`, **trades=yes**)
> writing under `cron_tracking/morning_dca/YYYYMMDD_1030ET/`
> (`run_id='morning_live_YYYYMMDD_1030ET'`), moved from the former 9:45 AM live
> slot to give a true **60-minute intraday trend window**. **Confidence rule:**
> if price/drop passes but the intraday trend cannot be validated, the live run
> does **not** auto-buy — it classifies the candidate as
> `execution_status='manual_review_candidate'`. Both modes carry `stream='morning'`
> and `valid_afternoon_evidence=false`, never update `latest_scheduled_run.json`,
> and are never afternoon scheduled evidence; live morning fills count only as
> prior-allocation context for the afternoon same-day top-up guard. See
> [morning_dca.md](morning_dca.md).
>
> **Midday stream note:** the midday `MiddayDCAExecutor` (`a6239dae`, run_type
> `midday_dca_live`, `state_schema_version='midday-live-v1'`, **trades=yes**)
> is a single **12:30 PM ET live Robinhood execution** on account `562057042`,
> writing under `cron_tracking/midday_dca/YYYYMMDD_1230ET/`
> (`run_id='midday_live_YYYYMMDD_1230ET'`). It reuses the **afternoon 0x-to-5x
> ladder** and the same $3 base (not the morning 0x-to-3x ladder), the same
> active universe (excluding BYDDY/VCX/TRLV/RVI), the same confidence rule, and
> its own `midday` budget ledger. It carries `stream='midday'` and
> `valid_afternoon_evidence=false`, never updates `latest_scheduled_run.json`,
> and is never afternoon scheduled evidence. Morning/manual same-day fills are
> **prior-allocation context only** for the midday top-up guard (midday buys the
> incremental difference only when its decision price is strictly below the prior
> reference price); midday fills in turn count only as prior-allocation context
> for the afternoon 3:20 PM top-up guard. See [midday_dca.md](midday_dca.md).
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
| `morning-dca-0945` | Morning graded DCA watchlist / early-warning scan (`MorningDCAExecutor`) | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
| `42173154` | Weekday 10:30 AM morning DCA live Robinhood execution (`MorningDCAExecutor`) | trading | **yes** | `30 14 * * 1-5` | 10:30 AM weekdays | active |
| `a6239dae` | Weekday 12:30 PM midday DCA live Robinhood execution (`MiddayDCAExecutor`) | trading | **yes** | `30 16 * * 1-5` | 12:30 PM weekdays | active |
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

### `morning-dca-0945` — Morning graded DCA watchlist / early-warning scan (`MorningDCAExecutor`)
- Analytics / **read-only**, **no trading**. Runs **9:45 AM ET weekdays** (executor mode `watchlist`).
- Evaluates morning price weakness vs. yesterday’s regular-session close on the **morning 0x-to-3x ladder** (`config/morning_dca.json`). May surface early candidates but labels every dollar amount **hypothetical / watchlist-only**; runs no Robinhood order review and calls no broker placement path. Writes watchlist artifacts under `/home/user/workspace/cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/` (`run_id='morning_YYYYMMDD_0945ET'`).
- `stream='morning'`, `run_type='morning_dca_watchlist_scan'`, `state_schema_version='morning-watchlist-v1'`, `valid_afternoon_evidence=false`. Never writes into the afternoon `ce16ba6b` tree or updates `latest_scheduled_run.json`.

### `42173154` — Weekday 10:30 AM morning DCA live Robinhood execution (`MorningDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **10:30 AM ET weekdays** — moved from the former 9:45 AM live slot to give a true **60-minute intraday trend window**.
- Uses the **$3 base morning 0x-to-3x ladder** and the `morning` budget ledger (`config/morning_dca.json`). **Robinhood account 562057042 only**; Alpaca is read-only for validation/context.
- **Confidence rule:** if a candidate's price/drop passes the ladder but the **intraday trend cannot be validated** (fewer than `trend_min_bars` usable intraday bars at/before the decision time), the run does **not** auto-buy; it classifies the candidate as `execution_status='manual_review_candidate'`. There is no loosened trend-unavailable execution path.
- Writes live artifacts under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/` (`run_id='morning_live_YYYYMMDD_1030ET'`).
- `stream='morning'`, `run_type='morning_dca_live'`, `state_schema_version='morning-live-v1'`, `valid_afternoon_evidence=false`. Never writes into the afternoon `ce16ba6b` tree or updates `latest_scheduled_run.json`. Morning fills count **only** as prior-allocation context for the afternoon same-day top-up guard — never afternoon scheduled evidence, slippage, watchdog confirmation, scheduled cost basis, or scheduled fill counts.

### `a6239dae` — Weekday 12:30 PM midday DCA live Robinhood execution (`MiddayDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **12:30 PM ET weekdays** (window 12:25–12:35 PM ET).
- Uses the **$3 base afternoon 0x-to-5x ladder** (`config/midday_dca.json`, same ladder as the afternoon 3:20 PM stream, intentionally distinct from the morning 0x-to-3x ladder) and its own `midday` budget ledger. **Robinhood account 562057042 only**; Alpaca is read-only for validation/context.
- Same active universe (excludes BYDDY/VCX/TRLV/RVI). **Confidence rule:** if a candidate's price/drop passes the ladder but the **intraday trend cannot be validated** (fewer than `trend_min_bars` usable intraday bars at/before 12:30), the run does **not** auto-buy; it classifies the candidate as `execution_status='manual_review_candidate'`.
- Writes live artifacts under `/home/user/workspace/cron_tracking/midday_dca/YYYYMMDD_1230ET/` (`run_id='midday_live_YYYYMMDD_1230ET'`) and updates its own `latest_midday_run.json` pointer.
- `stream='midday'`, `run_type='midday_dca_live'`, `state_schema_version='midday-live-v1'`, `cron_id='a6239dae'`, `supersedes_cron_id=null`, `valid_afternoon_evidence=false`. Never writes into the morning or afternoon (`ce16ba6b`) trees or updates `latest_scheduled_run.json`.
- **Same-day top-up guard:** morning/manual earlier same-day allocations (before **2:30 PM ET**, non-scheduled) are **prior-allocation context only** — never midday scheduled evidence. Per ticker, the run buys only the **incremental** dollar difference (midday desired minus amount already bought) and only when the midday decision price is **strictly below** the prior reference price; otherwise **$0**. The run_state records `prior_same_day_amount`, `prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`, `midday_desired_amount`, `midday_decision_price`, `incremental_order_amount`, `top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. Midday fills count only as prior-allocation context for the afternoon 3:20 PM top-up guard. See [midday_dca.md](midday_dca.md).

### `e3e6f09e` — Monitor-only graded DCA signal check
- Analytics and notifications only; **no trades**. Runs **3:05 PM ET weekdays**.
- Monitor-only signal check that surfaces the planned allocation and tier selection using the current $3 base 0x-to-5x ladder.

### `10ce362c` — Graded DCA automatic live test - $3 allocation (the only trading task)
- Repaired live task `10ce362c`, which **supersedes the deleted `ce16ba6b`**; the artifact root stays under `.../ce16ba6b` for continuity.
- Approved live trading task running the **$3 base 0x-to-5x ladder with tiered caps**. Places real market buy orders automatically within safeguards. Runs **3:20 PM ET weekdays**.
- Uses **only** Robinhood account `562057042`; Alpaca is read-only.
- Trigger gate: `programmatic_trigger_check.sh`, allowing weekdays **2026-07-02 through 2026-08-28**, **3:20–3:35 PM ET** (aligned to the 15:15–15:35 ET scheduled classification window so a late-but-permitted start cannot place buys after the 3:35 PM post-trade sync).
- Writes standardized same-day state files under `/home/user/workspace/cron_tracking/ce16ba6b` ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)); must **not** fall back to interactive confirmation.
- **State-file guarantee:** **always** writes the scheduled `run_state.json` and updates `latest_scheduled_run.json` on every fire, **even when no order is placed**. A confirmation-blocked run (review passed, submission blocked by required post-review confirmation) records `order_submit_status='not_submitted_confirmation_required'`, `reviewed_order` details where available, candidate details, and **no `order_id`** — valid scheduled-run evidence, not order/fill evidence.
- **Run classification:** a scheduled run writes `scheduled_runs/YYYYMMDD_1520ET/run_state.json` (`run_origin='scheduled'`, `manual_run=false`, `scheduled_for_et='15:20'`, `state_schema_version='run-classification-v1'`, `run_id='scheduled_YYYYMMDD_1520ET'`, and **both** `cron_id='10ce362c'` **and** `supersedes_cron_id='ce16ba6b'`) and updates `latest_scheduled_run.json`; a manual run writes `manual_runs/<timestamp>/run_state.json` (`run_origin='manual'`, `manual_run=true`) and never updates that pointer. Placeholder/ambiguous ids such as `ce16ba6b_replacement` are forbidden; for continuity, consumers still accept `cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'` as valid evidence. See [run_classification.md](run_classification.md).
- **Manual runs do not count as scheduled-run evidence.** Any stale stored display name is superseded by the authoritative **3:20 PM ET** schedule.
- **Same-day top-up guard:** loads earlier same-day (before **2:30 PM ET**, non-scheduled) allocations from the artifact root and offsets each ticker so it buys only the incremental amount into a lower price; per-candidate the run_state records `prior_same_day_amount`, `prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`, `afternoon_desired_amount`, `afternoon_decision_price`, `incremental_order_amount`, `top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. A fully-offset day still writes a valid scheduled run_state. See [strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).

### `d72aa525` — Capital-priority briefing
- Analytics only, **no trading**. Runs **3:25 PM ET weekdays**.
- Uses the current $3 base 0x-to-5x ladder. Sends **email/in-app notification only if at least one ticker triggers 1.5x or higher**; otherwise silent.

### `67aa802b` — Post-trade slippage + compact Notion latest-run sync
- Analytics only, **no trading**. Runs **3:35 PM ET weekdays**.
- Reads the **scheduled** live-run state and Robinhood order history, writes slippage/coverage artifacts, updates the compact Notion **Latest Run** section only.
- **Run classification:** scheduled-live rows exclude all manual order IDs and any `run_origin='manual'`/`manual_run=true` state; manual fills go only in the labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).
- **Confirmation-blocked run:** when the scheduled state has `order_submit_status='not_submitted_confirmation_required'`, report **no scheduled fills and no scheduled order IDs**; do **not** invalidate the run_state and do **not** apply the broker time-window fallback (no write failure occurred).

### `a46bf007` — Live-run watchdog
- Read-only, **no trading**. Runs **3:40 PM ET weekdays** — the **final scheduled afternoon event**.
- Alerts only on missed, incomplete, error, or discrepancy conditions.
- **Run classification:** validates that a scheduled run_state exists and `latest_scheduled_run.json` points at it; a manual-only or broker-orders-only day is a discrepancy, not a scheduled run.
- **Confirmation-blocked run:** when `order_submit_status='not_submitted_confirmation_required'`, alert that **no order was placed**, but do **not** mark the lineage/scheduled state invalid — a confirmation-blocked run with the required fields present is a valid scheduled run that fired.

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
