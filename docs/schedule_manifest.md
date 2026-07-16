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

> **Raw data persistence fix (added Jul 10 2026):** before signal computation the
> live task (`10ce362c`) must persist every raw Robinhood/Finance/Alpaca/Yahoo
> connector response it used under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/raw/` with deterministic
> filenames (`quotes_batchN.json`, `historicals_batchN.json`,
> `tradability_pre_place.json`, `portfolio_pre_place.json`,
> `orders_after_place.json`, `place_SYMBOL.json`), normalizing large connector
> `output_path` pointers into parseable JSON. **Compute must read only these
> persisted files, not in-memory tool responses.** If persistence/normalization
> fails, the task still writes a valid scheduled `run_state.json`
> (`run_origin='scheduled'`, `manual_run=false`,
> `order_submit_status='not_submitted_error'`, no orders) rather than escalating
> without state. See the `10ce362c` task detail below.

> **Post-artifact drift detection (added Jul 16 2026):** every live trading task
> (morning `42173154` 10:30 AM, midday `a6239dae` 12:30 PM, afternoon
> `10ce362c` 3:20 PM) must invoke `scripts/drift_detection.py` against its own
> run directory **immediately after writing artifacts**. The detector reconciles
> `signals.csv` (intended auto-buys) against `submitted_orders.csv` (actual order
> records) and fires an **immediate alert** when any symbol has
> `auto_buy_signal` true (case-insensitive `true`/`1`/`yes`) and
> `incremental_order_amount > 0` or a positive desired amount, but **no
> submitted-order record exists** for that symbol. This exists because of the
> **Jul 16 2026 failure mode**: `signals.csv` held **8** `auto_buy_signal=true`
> rows with positive incremental amounts while `submitted_orders.csv` was
> **empty** and `raw/` was **missing** — signals computed, no orders placed or
> persisted, and nothing alerted until the next day. The detector writes a
> compact JSON + markdown diagnostic (affected symbols, expected amount,
> suspected cause, run directory, recommended action) and, when
> `DCA_DRIFT_SLACK_WEBHOOK` is set and `--slack` is passed, posts an immediate
> Slack alert (no webhook is ever hardcoded). The 3:20 PM check must complete
> **before** the 3:40 PM watchdog (`a46bf007`). See
> [drift_detection.md](drift_detection.md) and the `drift_detection_policy` block
> in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

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
> **Morning two-stream note:** the morning `MorningDCAExecutor` runs on the
> shared **$3-base 0x-to-5x** live ladder (as of the Fri Jul 10 2026
> live-automation update every live stream shares one ladder; the former
> morning-only 0x-to-3x ladder is historical/superseded) and its own `morning`
> budget ledger in **two** modes.
> (1) A **9:45 AM ET read-only pre-flight validation**
> (`morning-dca-0945`, run_type `morning_dca_preflight`,
> `state_schema_version='morning-preflight-v2-machine-complete'`, **no trades**)
> writing under `cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/`
> (`run_state.json`, `validation.csv`, `signals.csv`, `run_summary.json`,
> `latest_preflight.json`). As of **2026-07-13** the `run_state.json` is
> **machine-complete** (schema
> [`schemas/morning_preflight.schema.json`](../schemas/morning_preflight.schema.json)):
> it always writes top-level `overall_ok`, `eligible_count`, `ineligible_count`,
> `unresolved_tickers`, `blocking_errors`, `warnings`, `run_classification`, and a
> `per_ticker_eligibility` list with **every active-universe symbol exactly once**
> so the 10:30 live task can gate directly off machine fields (added after the
> 2026-07-13 10:30 run was blocked by a 9:45 artifact missing `overall_ok` and the
> per-ticker eligibility list). Unresolved symbols (e.g. AAAU, SPCX) are rows with
> `eligible_to_execute=false`, `ineligible_reason='unresolved_ticker'` and never
> set `overall_ok=false`; `overall_ok` is false only for
> global/account/buying-power/auth/schema-write failures.
> It emits **`eligible_to_execute=true/false` per ticker** — a **coarse
> pre-screen** for the 10:30 evaluator, **not** a final approval. (A read-only
> `watchlist` early-warning mode remains available under
> `cron_tracking/morning_dca_watchlist/`, run_type `morning_dca_watchlist_scan`.)
> (2) A **10:30 AM ET live Robinhood execution** (`42173154`, run_type
> `morning_dca_live`, `state_schema_version='morning-live-v1'`, **trades=yes**)
> writing under `cron_tracking/morning_dca/YYYYMMDD_1030ET/`
> (`run_id='morning_live_YYYYMMDD_1030ET'`), giving a true **60-minute intraday
> trend window**. It **loads the same-day 9:45 pre-flight artifact and evaluates
> only `eligible_to_execute=true` tickers** (recomputing signals/budget/spread/
> trend/top-up fresh); if the pre-flight artifact is missing it degrades to the
> full universe with a warning rather than blocking all buys. **Confidence rule:**
> if price/drop passes but the intraday trend cannot be validated, the live run
> does **not** auto-buy — it classifies the candidate as
> `execution_status='manual_review_candidate'`. All morning modes carry
> `stream='morning'` and `valid_afternoon_evidence=false`, never update
> `latest_scheduled_run.json`, and are never afternoon scheduled evidence; live
> morning fills count only as prior-allocation context for the afternoon same-day
> top-up guard. See [morning_dca.md](morning_dca.md).
>
> **Midday stream note:** the midday `MiddayDCAExecutor` (`a6239dae`, run_type
> `midday_dca_live`, `state_schema_version='midday-live-v1'`, **trades=yes**)
> is a single **12:30 PM ET live Robinhood execution** on account `562057042`,
> writing under `cron_tracking/midday_dca/YYYYMMDD_1230ET/`
> (`run_id='midday_live_YYYYMMDD_1230ET'`). It reuses the **afternoon 0x-to-5x
> ladder** and the same $3 base (the shared live ladder used by all streams as
> of Jul 10 2026), the same
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
| `morning-dca-0945` | Morning graded DCA pre-flight validation (`MorningDCAExecutor`) | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
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

### `morning-dca-0945` — Morning graded DCA pre-flight validation (`MorningDCAExecutor`)
- Analytics / **read-only**, **no trading**. Runs **9:45 AM ET weekdays** (executor mode `preflight`).
- Evaluates morning price weakness vs. yesterday’s regular-session close on the shared **$3 base 0x-to-5x ladder** (`config/morning_dca.json`) as a pre-screen for the 10:30 live evaluator; runs no Robinhood order review and calls no broker placement path. Writes pre-flight artifacts under `/home/user/workspace/cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/` — `run_state.json`, `validation.csv`, `signals.csv`, `run_summary.json`, and the `latest_preflight.json` pointer (`run_id='morning_preflight_YYYYMMDD_0945ET'`).
- Emits **`eligible_to_execute=true/false` per ticker**. This is a **coarse pre-screen** (is a ticker worth re-evaluating at 10:30?), **NOT** a final buy approval — the 10:30 live run recomputes signals/budget/spread/trend/top-up from scratch and may still decline. A ticker is eligible when it graded to a positive-multiplier BUY at 9:45 with a usable current price and previous close.
- `stream='morning'`, `run_type='morning_dca_preflight'`, `state_schema_version='morning-preflight-v2-machine-complete'`, `valid_afternoon_evidence=false`. Never writes into the afternoon `ce16ba6b` tree or updates `latest_scheduled_run.json`. (A read-only `watchlist` early-warning mode remains available, writing under `cron_tracking/morning_dca_watchlist/`.)

### `42173154` — Weekday 10:30 AM morning DCA live Robinhood execution (`MorningDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **10:30 AM ET weekdays** — moved from the former 9:45 AM live slot to give a true **60-minute intraday trend window**.
- Uses the shared **$3 base 0x-to-5x live ladder** and the `morning` budget ledger (`config/morning_dca.json`). **Robinhood account 562057042 only**; Alpaca is read-only for validation/context.
- **Loads the same-day 9:45 pre-flight artifact** (`cron_tracking/morning_dca_preflight/`) and evaluates **only `eligible_to_execute=true` tickers**, recomputing signals/budget/spread/trend/top-up fresh. If the pre-flight artifact is missing it degrades to the full universe with a warning rather than blocking all buys.
- **Pre-authorized direct placement:** as approved Fri Jul 10 2026 11:20 AM ET, this scheduled live task auto-places qualifying buys within all caps/circuit breakers **without per-order confirmation** and **without calling `review_equity_order`**; it calls `place_equity_order` directly only after every internal validation, signal, budget, spread, confidence, and top-up check passes. (Manual/ad hoc runs still use the review + confirmation path.)
- **Confidence rule:** if a candidate's price/drop passes the ladder but the **intraday trend cannot be validated** (fewer than `trend_min_bars` usable intraday bars at/before the decision time), the run does **not** auto-buy; it classifies the candidate as `execution_status='manual_review_candidate'`. There is no loosened trend-unavailable execution path.
- Writes live artifacts under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/` (`run_id='morning_live_YYYYMMDD_1030ET'`).
- `stream='morning'`, `run_type='morning_dca_live'`, `state_schema_version='morning-live-v1'`, `valid_afternoon_evidence=false`. Never writes into the afternoon `ce16ba6b` tree or updates `latest_scheduled_run.json`. Morning fills count **only** as prior-allocation context for the afternoon same-day top-up guard — never afternoon scheduled evidence, slippage, watchdog confirmation, scheduled cost basis, or scheduled fill counts.

### `a6239dae` — Weekday 12:30 PM midday DCA live Robinhood execution (`MiddayDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **12:30 PM ET weekdays** (window 12:25–12:35 PM ET).
- Uses the shared **$3 base 0x-to-5x live ladder** (`config/midday_dca.json`, the same ladder used by all live streams as of Jul 10 2026) and its own `midday` budget ledger. **Robinhood account 562057042 only**; Alpaca is read-only for validation/context.
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
- **RAW DATA PERSISTENCE FIX ADDED JUL 10 2026:** the 2026-07-10 3:20 PM run
  (`cron_id 10ce362c`) escalated because quotes and historicals were fetched
  successfully but the **raw connector responses were never persisted to disk
  before signal computation** — the bash compute step had no
  `quotes_batchN.json` / `historicals_batchN.json` to read. From this date on,
  **before signal computation** the scheduled run must **persist every raw
  Robinhood/Finance/Alpaca/Yahoo connector response** it used under
  `scheduled_runs/YYYYMMDD_1520ET/raw/` with deterministic filenames:
  `quotes_batchN.json`, `historicals_batchN.json`, `tradability_pre_place.json`,
  `portfolio_pre_place.json`, `orders_after_place.json`, and `place_SYMBOL.json`
  where applicable.
  - **Normalize before persisting.** Large connector outputs that return an
    `output_path` pointer instead of inline data must be normalized into
    parseable JSON (the referenced file's contents embedded under `data`, the
    original pointer preserved under `output_path`) so the compute step can parse
    the saved file directly.
  - **Compute reads persisted files only.** Signal computation must read from the
    persisted/normalized files under `raw/`, **not** from in-memory tool
    responses. Fetch → persist → normalize → compute → place, in that order.
  - **Persistence failure still writes state.** If raw persistence or
    normalization fails, the task must **still write a valid scheduled**
    `run_state.json` with `run_origin='scheduled'`, `manual_run=false`,
    `order_submit_status='not_submitted_error'`, the error in
    `raw_persistence_error`, empty `raw_inputs_saved`, and **no orders placed** —
    rather than escalating without any state.
  - The run_state records `raw_input_dir` (absolute path to `raw/`),
    `raw_inputs_saved` (filenames actually written), and
    `recovery_from_escalation` (true when the state was produced by recovering an
    escalated run). Helpers: `scripts/run_classification.py`
    (`raw_input_dir`, `save_raw_input`, `normalize_connector_payload`,
    `build_raw_persistence_state`, `raw_persistence_failure_run_state`); schema
    fields in [`schemas/run_state.schema.json`](../schemas/run_state.schema.json).
  - **Recovered run (2026-07-10 3:20 PM ET):** the escalated run was recovered by
    re-fetching quotes/historicals, saving raw connector outputs under
    `/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260710_1520ET/raw/`,
    normalizing the large `output_path` pointers, computing signals from those
    files, then placing qualifying incremental orders: **9 incremental scheduled
    afternoon orders totalling $31.50**, saved under
    `/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260710_1520ET/`.
    These are valid afternoon scheduled evidence. Broker policy is unchanged
    (Robinhood-only execution; Alpaca/Finance/Yahoo read-only; no
    `review_equity_order` in scheduled live runs).
- **Same-day top-up guard:** loads earlier same-day (before **2:30 PM ET**, non-scheduled) allocations from its own artifact root **plus** the morning-live root (`.../cron_tracking/morning_dca`) and the midday-live root (`.../cron_tracking/midday_dca`, which also holds manual override reruns under `manual_reruns/`), and offsets each ticker so it buys only the incremental amount `max(0, desired − prior)` into a lower price. The prior reference price is resolved by preference: logged `target_price`, then `decision_price`, then `average_fill_price` (from `orders.csv`/`filled_orders.csv`); a missing reference → `manual_review_candidate` at `$0`. Per-candidate the run_state records `prior_same_day_amount`, `prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`, `afternoon_desired_amount`, `afternoon_decision_price`, `incremental_order_amount`, `top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. Morning/midday/manual override prior-context fills are **never** afternoon scheduled evidence, slippage, reliability, fill counts, or cost basis. A fully-offset day still writes a valid scheduled run_state. See [strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).
- **Final buy of the day behavior:** for the **final live buy of the day** the run performs a **full active-universe scan** and, **for this final-buy decision only**, **ignores the 60-minute downward-trend filter**. Every other gate still holds: current/decision price **strictly below the prior completed regular-session close**, ladder **multiplier > 0** (from the $3-base 0x-to-5x ladder), **data-confidence** pass, **`spread_ok=true`**, **budget/tiered-cap** pass, **regular trading hours only**, **whitelisted (active-universe) ticker**, and all **hard circuit breakers** clear. The same-day top-up guard above still applies to the final buy. (Morning 10:30 and midday 12:30 live runs still require trend validation unless changed later.) See [strategy.md](strategy.md#final-buy-of-the-day-320-pm-et-task-10ce362c).
- **Jul 13 2026 final manual override incident:** the scheduled 3:20 PM ET run placed **0 orders**; the user then requested the final buy **manually**. A final manual-review scan ran the final-buy behavior above (full scan, trend ignored) with same-day top-up offsets and filled **34 orders**, planned total **$234.00** (`DFTX TRIN SPCX LUNR IRDM DXYZ LITE ASML DRAM STX AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR INTC GOOGL GOOG GLW GS MU XLG TOPT QTOP SATA RGTI`). These are **manual final-buy overrides / same-day top-up context only — NOT scheduled-afternoon execution evidence**. Their run_state carries `run_type='final_manual_buy_override'`, `run_origin='manual'`, `manual_run=true`, `valid_afternoon_scheduled_evidence=false`, `same_day_top_up_context_only=true`. Artifacts (run_state + `filled_orders.csv` with all order IDs) were backfilled under `/home/user/workspace/cron_tracking/ce16ba6b/manual_runs/final_manual_override_20260713_1536ET/` and `/home/user/workspace/cron_tracking/midday_dca/manual_reruns/final_manual_override_20260713_1536ET/`. All order IDs are excluded from scheduled slippage/fills/reliability/cost basis/watchdog confirmation and shown only in the **"Manual Final Buy Override / Same-Day Top-Up Context"** section. See [run_classification.md](run_classification.md) and [strategy.md](strategy.md#manual-final-buy-override--2026-07-13-incident).

### `d72aa525` — Capital-priority briefing
- Analytics only, **no trading**. Runs **3:25 PM ET weekdays**.
- Uses the current $3 base 0x-to-5x ladder. Sends **email/in-app notification only if at least one ticker triggers 1.5x or higher**; otherwise silent.

### `67aa802b` — Post-trade slippage + compact Notion latest-run sync
- Analytics only, **no trading**. Runs **3:35 PM ET weekdays**.
- Reads the **scheduled** live-run state and Robinhood order history, writes slippage/coverage artifacts, updates the compact Notion **Latest Run** section only.
- **Run classification:** scheduled-live rows exclude all manual order IDs and any `run_origin='manual'`/`manual_run=true` state; manual fills go only in the labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).
- **Manual exclusion roots + final override (added Jul 13 2026):** collect and exclude manual order IDs from all manual exclusion roots — the task's own `manual_runs/**` **plus** `final_manual_review/**`, `.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and `.../cron_tracking/midday_dca/manual_reruns/`. Fills from a `final_manual_buy_override` run (e.g. the 2026-07-13 incident) are excluded from scheduled slippage/fills/reliability/cost basis and reported only in the labeled **"Manual Final Buy Override / Same-Day Top-Up Context"** section (`manual_final_buy_override` in [`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).
- **Confirmation-blocked run:** when the scheduled state has `order_submit_status='not_submitted_confirmation_required'`, report **no scheduled fills and no scheduled order IDs**; do **not** invalidate the run_state and do **not** apply the broker time-window fallback (no write failure occurred).
- **Schema-version acceptance (added Jul 10 2026):** accept scheduled run_states tagged **either** `state_schema_version='run-classification-v1'` **or** `run-classification-v2-preauthorized` as long as the required scheduled identity fields are correct (`run_origin='scheduled'`, `manual_run=false`, `cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'`, `scheduled_for_et='15:20'`, window, `run_id`). v2 is a **stricter successor** of v1 (pre-authorized direct placement), not a different contract.
- **Order-history payload persistence (added Jul 10 2026):** persist the Robinhood order-history payloads used for slippage (e.g. `scheduled_runs/YYYYMMDD_1520ET/raw/orders_after_place.json`) **before** slippage computation, and compute average fill price / executions from the **saved payloads** (`dca_reporting.fills_from_order_history_payload`) rather than re-querying the broker. Record the payload paths in `source_order_history_payloads` (see [`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).

### `a46bf007` — Live-run watchdog
- Read-only, **no trading**. Runs **3:40 PM ET weekdays** — the **final scheduled afternoon event**.
- Alerts only on missed, incomplete, error, or discrepancy conditions.
- **Run classification:** validates that a scheduled run_state exists and `latest_scheduled_run.json` points at it; a manual-only or broker-orders-only day is a discrepancy, not a scheduled run.
- **Confirmation-blocked run:** when `order_submit_status='not_submitted_confirmation_required'`, alert that **no order was placed**, but do **not** mark the lineage/scheduled state invalid — a confirmation-blocked run with the required fields present is a valid scheduled run that fired.
- **Watchdog scan fix (added Jul 13 2026):** when broker order history shows orders after 3:15 PM ET but no scheduled state accounts for them, scan the manual exclusion roots — `final_manual_review/**`, `.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and `.../cron_tracking/midday_dca/manual_reruns/` — before flagging a discrepancy. Orders matched to a `final_manual_buy_override` run are manual overrides (`valid_afternoon_scheduled_evidence=false`): exclude them from scheduled-confirmation and do **not** treat them as scheduled evidence. The 3:40 PM 2026-07-13 alert was caused by final-override fills lacking artifacts under the scanned paths; artifacts were backfilled under the two `final_manual_override_20260713_1536ET/` roots. See [run_classification.md](run_classification.md).

### `041d9d90` — Weekly Notion performance dashboard refresh
- Analytics only, **no trading**. Runs **Fridays 5:00 PM ET** — the only approved post-3:40 PM exception.
- Recalculates and refreshes the full Notion performance dashboard. No routine email.
- **Run classification:** weekly scheduled-live metrics use only validated scheduled runs with manual runs/order IDs excluded; manual fills, if shown, are confined to a labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).
- **Schema-version compatibility (added Jul 10 2026):** include **v2-preauthorized** scheduled runs (`state_schema_version='run-classification-v2-preauthorized'`) in weekly metrics whenever the required scheduled identity fields are present, alongside v1 runs.
- **Slippage fallback (added Jul 10 2026):** if per-day slippage artifacts are missing, compute from the persisted broker-history payloads (`scheduled_runs/YYYYMMDD_1520ET/raw/orders_after_place.json` via `dca_reporting.fills_from_order_history_payload`) **before** declaring slippage unavailable for that day.

#### Recovered slippage — 2026-07-10 (afternoon scheduled)
- **9 valid afternoon scheduled fills**, total **$31.50**.
- Average `slippage_pct` ≈ **0.1047%**; largest absolute slippage symbol **QBTS** ≈ **0.2089%**.
- **46 manual order IDs excluded** from the scheduled-live metrics.
- Artifacts under `/home/user/workspace/cron_tracking/ce16ba6b/slippage/20260710/`; fills computed from the scheduled run's raw `orders_after_place.json`.

### `0656657a` — Monthly graded DCA dashboard refresh
- Analytics only, **no trading**. First weekday of the month **8:45 AM ET**. No-op otherwise.

### `bec4a6ee` — Monthly DCA automation cost review
- Analytics only, **no trading**. First weekday of the month **9:55 AM ET**.
- Reports **usage proxies only**; exact Perplexity credit consumption is not exposed, so do not claim exact credits.

### `f7411889` — One-time reminder
- Reminder only, **no trading**. Fires **2026-08-15 11:45 AM ET** to evaluate continuing live testing past 2026-08-28.
