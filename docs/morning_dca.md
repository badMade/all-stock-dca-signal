# Morning DCA Stream

The **morning** DCA stream grades each ticker's morning price weakness against
**yesterday's regular-session close** on the shared **$2-base 0x-to-5x** live
ladder. As of the **Fri Jul 10 2026** live-automation update every live stream
(morning 10:30, midday 12:30, afternoon 3:20) shares one ladder (bound to
`near_close_dca.StrategyConfig.sizing_tiers`); the former morning-only 0x-to-3x
ladder is **historical/superseded**. It is implemented by
[`morning_dca.MorningDCAExecutor`](../morning_dca.py) and governed by
[`config/morning_dca.json`](../config/morning_dca.json).

The stream runs in these modes:

| Mode | Task | ET | Trades | run_type | schema | Artifact root |
|---|---|---|---|---|---|---|
| `preflight` | `morning-dca-0945` | 9:45 AM | **no** | `morning_dca_preflight` | `morning-preflight-v2-machine-complete` | `.../cron_tracking/morning_dca_preflight/` |
| `live` | `42173154` | 10:30 AM | **yes** | `morning_dca_live` | `morning-live-v2-preauthorized` | `.../cron_tracking/morning_dca/` |
| `watchlist` | (read-only, optional) | 9:45 AM | **no** | `morning_dca_watchlist_scan` | `morning-watchlist-v1` | `.../cron_tracking/morning_dca_watchlist/` |

## 9:45 AM pre-flight validation (read-only)

A **read-only / analytics-only** pre-screen for the 10:30 live evaluator. It
grades morning weakness on the shared ladder and emits
**`eligible_to_execute=true/false` per ticker**. It places **no orders**, runs
**no Robinhood order review**, and calls **no broker placement path**. Writes
under `.../cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/` —
`run_state.json`, `validation.csv`, `signals.csv`, `run_summary.json` — and
updates that root's `latest_preflight.json` (`run_id='morning_preflight_YYYYMMDD_0945ET'`).

**`eligible_to_execute` semantics:** a **coarse pre-screen** answering "is this
ticker worth re-evaluating at 10:30?", **NOT** a final buy approval. A ticker is
eligible when it graded to a positive-multiplier BUY at 9:45 with a usable
current price and previous close. The 10:30 live run recomputes everything fresh
and may still decline; an ineligible ticker is simply skipped by the 10:30 gate.

(An optional read-only `watchlist` early-warning scan mode remains available,
writing under `.../cron_tracking/morning_dca_watchlist/`, run_type
`morning_dca_watchlist_scan`; its dollar amounts are hypothetical/watchlist-only.)

### Machine-complete pre-flight contract (`morning-preflight-v2-machine-complete`)

As of **Mon Jul 13 2026** the 9:45 `run_state.json` is **machine-complete** and
validated by
[`schemas/morning_preflight.schema.json`](../schemas/morning_preflight.schema.json).
It was introduced after the 10:30 AM live execution was **blocked** because the
9:45 artifact lacked the machine-readable gate fields the live task needs — a
top-level `overall_ok` flag and a per-ticker `eligible_to_execute` list. The
pre-flight now **always** writes:

- Top level: `overall_ok`, `eligible_count`, `ineligible_count`,
  `unresolved_tickers`, `blocking_errors`, `warnings`, `run_classification`, and
  `per_ticker_eligibility`.
- `per_ticker_eligibility` — one row per **active-universe symbol, each exactly
  once**, carrying `symbol`, `active_universe`, `quote_available`,
  `prior_close_available`, `tradable`, `account_ok`, `buying_power_ok`,
  `spread_pct`, `spread_ok`, `trend_status`, `data_confidence_status`,
  `early_drop_pct`, `early_multiplier`, `hypothetical_amount`,
  `eligible_to_execute`, and `ineligible_reason`.

The read-only coarse pre-flight does not bind a broker account or evaluate
spread/trend, so `account_ok`, `buying_power_ok`, `spread_pct`, `spread_ok` are
`null` and `trend_status` is `not_evaluated_preflight`; the 10:30 live run
computes them fresh.

**`overall_ok` rule:** `overall_ok` is `false` **only** for
global/account/buying-power/auth/schema-write failures (recorded in
`blocking_errors`).

**Unresolved-ticker rule:** unresolved symbols such as **AAAU** or **SPCX** are
represented as per-symbol rows with `eligible_to_execute=false` and
`ineligible_reason='unresolved_ticker'` (or a specific lookup/tradability
failure code) and listed in `unresolved_tickers`. They **never** set
`overall_ok=false` or invalidate the whole artifact as long as the other required
fields were written.

## 10:30 AM live Robinhood execution

The live execution runs at **10:30 AM ET** (`cron_id=42173154`, cron
`30 14 * * 1-5` = **14:30 UTC weekdays** during EDT) so the trend read has a true
**60-minute intraday window**. It uses **Robinhood account `562057042`** as the
**only execution broker** (Alpaca, Finance, and Yahoo stay **read-only** for
validation/account context), the shared **$2 base 0x-to-5x live ladder**, and the
`morning` budget ledger.

It **depends on the same-day 9:45 pre-flight artifact** and evaluates **only
`eligible_to_execute=true` tickers**, then **recomputes a fresh price, prior
close, spread, 60-minute downtrend, signal, and budget before placing**; if the
trend cannot be validated it **skips / routes to manual review** (never
auto-buys). If the pre-flight artifact is missing, a **scheduled** run **fails
closed** — `_apply_preflight_gate()` evaluates zero tickers and writes a
blocked `run_state.json` — rather than blocking all buys via a full-universe
fallback; only a **manual** run degrades to the full universe with a warning.

#### Pre-flight artifact location (added Jul 20 2026)

On **Mon Jul 20 2026** the 10:30 AM morning live run escalated because it looked
for the same-day 9:45 pre-flight artifact **under its own 10:30 live output
directory** (and a stray `/home/user/workspace/cron_tracking/42173154` path)
instead of the pre-flight root. The rule now is explicit:

- **Load the same-day 9:45 pre-flight artifact from**
  `/home/user/workspace/cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/run_state.json`,
  or from
  `/home/user/workspace/cron_tracking/morning_dca_preflight/latest_preflight.json`
  **only when that pointer points to the same-day** `YYYYMMDD_0945ET/run_state.json`
  file. If the pointer points to any other or older run, ignore it and use the
  same-day path.
- **Do not look** under `/home/user/workspace/cron_tracking/42173154` or under
  the morning live output directory
  (`/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/`) for the
  pre-flight artifact. The pre-flight artifact is never written there.
- **The morning live output directory remains**
  `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/` and holds
  this task's own artifacts only.
- **Bounded recovery lookup:** if the pre-flight artifact is missing from the
  correct path (and `latest_preflight.json` does not point to the same-day file),
  perform exactly **one** bounded recovery lookup before escalating: scan
  `/home/user/workspace/cron_tracking/morning_dca_preflight/` for today's
  `YYYYMMDD_0945ET` directory and load its `run_state.json`. Only after that
  single lookup also fails does a scheduled run fail closed as described above.

This is a path-resolution fix only. It does not weaken the budget caps, top-up
guard, data-confidence rule, spread rule, whitelist, buy-only, regular-hours-only,
Robinhood-execution-only, or any other safety control; fail-closed behavior on a
genuinely missing artifact is preserved. See `config/morning_dca.json`'s
`live_task.preflight_artifact_resolution`.

> **Implemented (2026-07-21).** `load_morning_config()` loads
> `must_not_look_under` into `MorningConfig.preflight_forbidden_roots` and
> `bounded_recovery_lookup` into `MorningConfig.preflight_bounded_recovery`.
> `MorningDCAExecutor._apply_preflight_gate()` rejects (fails closed) a
> `preflight_run_state_root` that resolves under a forbidden root before ever
> attempting a lookup, and `load_same_day_preflight()` performs the single
> bounded directory scan described above when the primary path and
> `latest_preflight.json` pointer both miss, recording
> `recovered_via_bounded_lookup=true` in `preflight_gate` when it was used. See
> `config/morning_dca.json`'s
> `live_task.preflight_artifact_resolution.implementation_status`.

**Pre-authorized direct placement (scheduled live runs only).** As approved Fri
Jul 10 2026 11:20 AM ET this scheduled live task is **pre-authorized** to place
qualifying buys directly via `place_equity_order`: it **does not** request
per-order confirmation and **does not** call Robinhood `review_equity_order`, and
calls `place_equity_order` **only after every safeguard passes**. Manual / ad hoc
runs still go through Robinhood's **review + confirmation path**:
`review_equity_order` (`review_market_buy`) must pass before `place_equity_order`
(`place_market_buy`) is attempted, and if Robinhood's own review requires
additional post-review confirmation (e.g. a step-up/2FA gate), placement raises
`OrderConfirmationRequiredError` and the run records
`not_submitted_confirmation_required` with **no order placed** — there is no
separate confirmation step implemented in this code beyond that broker-driven
gate. The scheduled live run_state carries
`state_schema_version='morning-live-v2-preauthorized'` and
`preauthorized_direct_place=true`. Writes under
`.../cron_tracking/morning_dca/YYYYMMDD_1030ET/`
(`run_id='morning_live_YYYYMMDD_1030ET'`).

### GFUZ special handling

**GFUZ** is in the active universe but needs special handling because **Finance
(and other read-only providers) may not resolve it**:

- **Robinhood is the primary** quote/prior-close source for GFUZ; Alpaca/Finance/
  Yahoo remain read-only validation/context only.
- An **unresolved GFUZ on Finance must not block the whole run**.
- The *intent* is that if the **9:45 pre-flight lacks GFUZ**, the 10:30 live task
  runs a **supplemental GFUZ read-only validation** and **persists
  `gfuz_supplemental_preflight.json`** under
  `.../cron_tracking/morning_dca/YYYYMMDD_1030ET/` **before** any GFUZ signal/
  budget/top-up/order decisioning, with the decision engine reading GFUZ only
  from the persisted file.

  > **Not yet implemented.** `_apply_preflight_gate()` admits tickers via a
  > plain `tickers ∩ eligible_tickers` intersection with no GFUZ-specific
  > exception, so a same-day preflight artifact missing a GFUZ row simply gates
  > GFUZ out for that run — a safe, fail-closed degradation (GFUZ is skipped,
  > never bought on unvalidated data), not the supplemental-validation flow
  > above. See `config/morning_dca.json`'s
  > `gfuz_special_handling.supplemental_preflight.implementation_status`.

### Raw data persistence (added Jul 15 2026, confirmed Jul 16 2026; implemented Jul 21 2026)

Mirroring the afternoon `10ce362c` fix, **before any signal computation,
budget calculation, top-up calculation, or order placement** the 10:30 live
run persists **every raw connector response** it used under
`.../cron_tracking/morning_dca/YYYYMMDD_1030ET/raw/` with deterministic
filenames, with the **decision engine reading only from the persisted files**
— never from an in-memory response that was never durably recorded. If data
fetch succeeds but **raw persistence fails before any order was placed, no
orders are placed** and the task still writes `run_state.json` with
`status='blocked'`, `block_reason` describing the failure, and
`error_class='missing_or_invalid_raw_inputs'`.

> **Implemented.** `MorningDCAExecutor.run()` wraps the injected data provider
> and broker adapter in `_PersistingMarketDataProvider` /
> `_PersistingBrokerAdapter` for every SCHEDULED (non-manual) live run: each
> previous-close/intraday-bar/quote/bid-ask response and each account-number/
> buying-power/tradability/same-day-order-existence broker query is persisted
> via `scripts.run_classification.save_raw_input` the moment it is obtained —
> before `evaluate_signal()`/`_place_broker_orders()` computes from it. This
> differs from the afternoon `run_dca.py` pattern in one respect: morning
> fetches lazily per ticker rather than as one upfront batch, so persistence
> happens per-call rather than as a single preceding step (functionally
> equivalent — nothing is used before its own response is on disk — but there
> is no `raw.tmp`/`raw.bak` staging-and-swap). A persistence failure caught
> during `evaluate()` (before any broker call) fails the run closed with
> `error_class='missing_or_invalid_raw_inputs'` and zero orders placed; a
> failure discovered only after order placement has begun does not
> retroactively block already-placed orders (they passed every existing
> safeguard) and is instead recorded on `raw_persistence_error`. Applies
> identically to the 12:30 midday task (`MiddayDCAExecutor` inherits `run()`
> unchanged). See `config/morning_dca.json`'s
> `raw_data_persistence.implementation_status`.

### Automatic failure recovery

The *intended* contract is that if the 10:30 run fails because of **internal
persistence, parsing, wrapper-normalization, transient connector throttling,
or a raw-file write problem after data was fetched**, it **immediately reruns
the required read-only inputs once**, persists all raw responses first,
recomputes **only from the persisted files**, and continues the **same guarded
execution** if safeguards pass. The recovery **reuses the same logical run
directory** (`.../morning_dca/YYYYMMDD_1030ET/`) with **`recovery_rerun=true`**
and is **never classified as manual** (`run_origin='scheduled'`,
`manual_run=false`). If the recovery rerun also fails, or a safety/account/order
rejection occurs, the task writes `run_state.json` with the blocking reason and
**escalates**.

> **Not yet implemented.** There is no recovery-rerun code path in
> `morning_dca.py`'s `run()` today; `recovery_rerun`/`recovery_reason`/
> `recovery_attempts` are never populated on any morning artifact. See
> `config/morning_dca.json`'s `automatic_failure_recovery.implementation_status`.

### Manual missed-run recovery (added Jul 20 2026)

Separate from the *automatic* (scheduled, in-process) failure recovery above,
this covers the case where the **10:30 AM scheduled live task did not run at
all** (a missed scheduled fire). A human may then trigger a **manual** recovery
that places the missed morning buys by hand. These manual artifacts are written
under a dedicated path:

- Pattern: `/home/user/workspace/cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`
- Example: `/home/user/workspace/cron_tracking/morning_dca/manual_recovery/20260720_1048ET`
  (Jul 20 2026: the 10:30 scheduled morning live task did not run, so a manual
  recovery ran at 10:48 AM ET.)

Every such artifact carries `run_origin='manual'`, `manual_run=true`,
`same_day_top_up_context_only=true`, and `valid_scheduled_evidence=false`. Under
those flags it is valid **only as prior-allocation / same-day top-up context**.
It **never** counts as proof the 10:30 scheduled task ran, is **never** written
into the midday or afternoon scheduled evidence stream, and is **never** used for
scheduled reliability, scheduled fill counts, scheduled slippage, scheduled cost
basis, or watchdog confirmation. It may only **reduce or block** additional buys
through the same-day top-up rule: a later run buys `max(0, desired - prior)` and
only if the later decision price is **strictly below** the prior reference price.

The **12:30 midday** (`a6239dae`) and **3:20 final** (`10ce362c`) runs **must
scan** `manual_recovery/YYYYMMDD_*` for same-day manual recovery artifacts and
fold them into the top-up guard as prior allocation. See
`config/morning_dca.json`'s `manual_missed_run_recovery` and
[run_classification.md](run_classification.md).

### 10:30 runner self-containment and state-file guarantee (added Jul 20 2026)

After the Jul 20 2026 10:30 scheduled morning live run failed (it invoked a saved
runner without first generating the same-day raw inputs and escalated without
persisting a scheduled failure-state artifact), the scheduled 10:30 task must:

**Generate raw inputs first.** The task **must not** assume runner inputs already
exist, except the same-day 9:45 pre-flight artifact at
`/home/user/workspace/cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/run_state.json`
(or `latest_preflight.json` when it points to that same-day file). Before
invoking any saved runner or order module, it **creates**
`/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/raw/` and persists
all required raw inputs: portfolio / buying power, quotes, prior regular-session
close, tradability, `orders_since_midnight` (for same-day top-up context), and the
historical / trend bars needed for the 60-minute trend check. If a runner requires
a `RUN_DATE_ET` argument, pass the current America/New_York date as `YYYYMMDD`. Do
**not** reuse stale files. Inputs come from the Finance connector primary, Alpaca
and Robinhood validation / context, and `yfinance` fallback **only** when connector
data is unavailable, stale, incomplete, or unresolved (see
[safeguards.md](safeguards.md#market-data-source-policy)). If raw input generation
fails once, perform **one** bounded recovery attempt on the alternate approved
data source path, then stop.

**Always write the state files.** The 10:30 scheduled run **always** writes
`run_state.json` and `latest_morning_run.json` under the morning root as part
of the standard artifact persistence flow, **even when execution is blocked or
no orders are placed** (`run_summary.json` is preflight-only — see
`config/morning_dca.json`'s `artifacts.manifest_accuracy_note`). When a
missing pre-flight artifact, missing/invalid raw inputs, trend, budget,
tradability, spread, or broker condition blocks execution, it writes a **valid
scheduled** morning `run_state` with `run_origin='scheduled'`, `manual_run=false`,
`stream='morning'`, `scheduled_for_et='10:30'`, `cron_id='42173154'`,
`valid_afternoon_evidence=false`, `status='blocked'`, a human-readable
`block_reason`, and — for a raw-input persistence failure specifically — a
concrete `error_class='missing_or_invalid_raw_inputs'`. This artifact is valid
proof the 10:30 scheduled task **fired** but is **not** order or fill
evidence. The task **must not** escalate without writing this state file
unless the workspace itself is unavailable. If failure happens after raw
collection has started (i.e. after order placement has begun), the run is
**not** retroactively blocked — the failure is instead recorded on
`raw_persistence_error` in the normally-written `run_state.json`, and
`morning_signals.csv`/`submitted_orders.csv` are written as usual (no
half-done run without a state file). See `config/scheduled_tasks.json`'s
`live_schedule_hardening_2026_07_20.morning_1030`.

### Hard circuit breakers

Every scheduled live order is subject to hard circuit breakers that **skip**
(never resize past a cap): **max per order `$10`** (5× × $2), **budget caps**,
**available buying power**, **whitelist-only** (active-universe tickers),
**data-confidence**, **spread threshold**, **trend validation**,
**regular-hours-only**, and **one strategy order per ticker per run**.

### Confidence rule (no trend, no auto-buy)

A candidate whose price/drop passes the ladder is auto-bought **only when the
intraday trend can be validated** — at least `trend_min_bars` (default **6**)
usable intraday bars (finite, positive close, timestamp at or before the decision
time). If price/drop passes but the intraday trend **cannot** be validated, the
run does **not** auto-buy; the candidate is classified
`execution_status='manual_review_candidate'` and `trend_validated=false`. There is
**no** loosened trend-unavailable execution path.

Signal rows in live mode add `execution_status`
(`auto_buy_eligible` | `manual_review_candidate`) and `trend_validated`; the
run_state counts and lists `manual_review_candidates` / `manual_review_tickers`.

## Why it is fully separate from the afternoon stream

The afternoon live task (`10ce362c`, which supersedes the deleted `ce16ba6b`)
must not be cannibalized or validated by morning activity. The morning stream is
isolated on every axis except the same-day top-up guard (see below):

| Axis | Afternoon live (`10ce362c`) | Morning stream |
|---|---|---|
| Ladder | 0x → **5x** (`config/sizing_ladder.json`) | 0x → **5x** (shared) |
| Budget ledger | `StrategyState` / `BUDGET_TIERS` | `MorningLedger` namespace `morning` |
| Artifact root | `.../cron_tracking/ce16ba6b/` | `.../cron_tracking/morning_dca[_watchlist]/` |
| Run type | `scheduled_live_test` @ `15:20` | `morning_dca_watchlist_scan` @ `09:45` / `morning_dca_live` @ `10:30` |
| Pointer | `latest_scheduled_run.json` | `latest_morning_run.json` |
| Trades | **yes** | watchlist **no** / live **yes** (morning-only) |

Neither morning mode writes into the `ce16ba6b` tree, neither updates
`latest_scheduled_run.json`, and each `run_classification` carries
`valid_afternoon_evidence=false` plus `stream="morning"`. Afternoon consumers
(`67aa802b`, `a46bf007`, `041d9d90`, `0656657a`) glob only
`.../ce16ba6b/scheduled_runs/**`, so they never traverse either morning root.

**Prior-allocation context only:** morning **live** fills are read by the
afternoon **same-day top-up guard** as prior-allocation context, but never as
afternoon scheduled evidence, slippage, watchdog confirmation, scheduled cost
basis, or scheduled fill counts. See
[strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).

Morning orders/fills are therefore **never** counted as afternoon
scheduled-execution evidence, afternoon slippage, afternoon fill counts,
afternoon reliability, afternoon watchdog confirmation, or afternoon scheduled
cost basis.

## Same-day top-up linkage (the only afternoon linkage)

Morning orders/fills **do** count as **prior same-day allocation context** for
the afternoon (`10ce362c`) same-day top-up guard. When the afternoon task
evaluates a ticker that already received a same-day morning buy, it tops up
rather than duplicating:

- `incremental_order_amount = max(0, afternoon_desired_amount - prior_same_day_amount)`
- When earlier same-day buys exist, the afternoon task buys only if the current
  price is **strictly below** the prior-run reference price; otherwise it holds.

Top-up guard fields carried for the 9:45 → afternoon linkage:
`prior_same_day_amount`, `prior_same_day_reference_price`,
`prior_same_day_source`, `prior_same_day_run_id`, `afternoon_desired_amount_usd`,
`afternoon_desired_amount`, `afternoon_decision_price`, `incremental_order_amount`,
`top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`.

## Sizing ladder (0x → 5x)

Drop is measured as a fraction below yesterday's regular-session close. The
largest tier threshold ≤ the actual drop wins. This is the current $2-base
0x-to-5x ladder shared with the afternoon stream.

| Drop vs. prior close | Multiplier |
|---|---|
| < 0.1% | 0x (no buy) |
| 0.1% – < 1% | 1x |
| 1% – < 2% | 1.5x |
| 2% – < 3% | 2x |
| 3% – < 4% | 2.5x |
| 4% – < 5% | 3x |
| 5% – < 6% | 3.5x |
| 6% – < 7% | 4x |
| 7% – < 8% | 4.5x |
| ≥ 8% | 5x (no upper cutoff) |

Dollars = `base_amount_usd (default $2) × multiplier` (hypothetical in
watchlist mode, actual order size in live mode).

## Budget ledger

`MorningLedger` is a standalone budget session (namespace `morning`) tracking
only morning notional. Configurable hooks:

- `morning_daily_cap_usd` (default `90.0`)
- `morning_weekly_cap_usd` (default `450.0`)
- per-mode roots `watchlist_run_state_root` / `live_run_state_root` (legacy
  `morning_run_state_root` mirrors the watchlist root)

Exceeding a cap adds a warning to the run artifact; it never touches the
afternoon daily/weekly ledger.

### Ledger location and bootstrap (added Jul 20 2026)

The **morning stream budget ledger lives under the 10:30 live output directory**
`/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/`, not under the
pre-flight root, the afternoon `ce16ba6b` tree, or any
`/home/user/workspace/cron_tracking/42173154` path. The 10:30 live task **may create**
`budget_ledger.json` there if it is absent.

The task **does not require a pre-existing ledger file**. If none exists, it
initializes the morning ledger from the configured `morning_daily_cap_usd`
(default `90.0`) and `morning_weekly_cap_usd` (default `450.0`) caps, then writes
the resulting budget calculations (caps, same-day morning spend, remaining daily
and weekly capacity) into the 10:30 `run_state.json`. A missing ledger file is
**not** an escalation condition; it is bootstrapped from the configured caps.

Bootstrapping an absent ledger does not weaken any cap. The daily/weekly caps,
the top-up guard, and all circuit breakers still apply exactly as before; only
the requirement of a pre-existing file on disk is removed. See
`config/morning_dca.json`'s `budget_ledger.bootstrap`.

> **Implemented (2026-07-21).** `load_morning_config()` loads
> `budget_ledger.ledger_files[0]` into `MorningConfig.budget_ledger_filename`
> (default `budget_ledger.json`), and `MorningDCAExecutor.write_artifacts()`
> always writes that file under the live run's own output directory as a
> snapshot of `artifacts["budget"]` — the same caps/spend/remaining-capacity
> object already written into `run_state.json`. No pre-existing file is ever
> required; each run overwrites its own dated slot's snapshot. See
> `config/morning_dca.json`'s `budget_ledger.bootstrap.implementation_status`.

## Artifacts

A scheduled morning run writes to its mode's root
(`.../cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/` for watchlist,
`.../cron_tracking/morning_dca/YYYYMMDD_1030ET/` for live):

- `run_state.json` — classification, ladder, budget, signal rows, warnings.
- `morning_signals.csv` — one row per ticker.
- `budget_ledger.json` — live mode only; a snapshot of the morning budget
  ledger (caps, spend, remaining capacity). See "Ledger location and
  bootstrap" above.

and updates that root's `latest_morning_run.json`. Manual runs write under a
distinct `manual_morning[_live]_*` run id and never update the pointer.

The **10:30 live** run's `write_artifacts()` **always** writes `run_state.json`
and `morning_signals.csv` (**not** `signals.csv`, and **not**
`run_summary.json`/`validation.csv` — those are written only in preflight mode)
under `.../cron_tracking/morning_dca/YYYYMMDD_1030ET/`, plus
`submitted_orders.csv` whenever at least one order was actually submitted;
`filled_orders.csv` is **never** written by this task (there is no
fill-confirmation step). Raw connector persistence under a `raw/` subdirectory
and `gfuz_supplemental_preflight.json` are both **not yet implemented** (see
**Raw data persistence** and **GFUZ special handling** above). See
`config/morning_dca.json`'s `artifacts.manifest_accuracy_note`. The live
`run_classification` block carries `run_origin='scheduled'`,
`run_type='morning_dca_live'`, `scheduled_for_et='10:30'`, `manual_run=false`,
`state_schema_version='morning-live-v2-preauthorized'`,
`run_id='morning_live_YYYYMMDD_1030ET'`, `cron_id='42173154'`,
`execution_broker='robinhood'`, `account_number='562057042'`,
`preauthorized_direct_place=true`, `valid_morning_evidence=true`, and
`valid_afternoon_evidence=false`.

Each signal row includes: `ticker`, `source`, `action`, `reason`,
`current_price`, `previous_close`, `drop_pct`, `multiplier`,
`hypothetical_amount_usd`, and `warnings`; live-mode rows also carry
`execution_status` and `trend_validated`.

## Run classification

Validated by
[`schemas/morning_run_classification.schema.json`](../schemas/morning_run_classification.schema.json).
A scheduled watchlist run:

```json
{
  "run_origin": "scheduled",
  "run_type": "morning_dca_watchlist_scan",
  "stream": "morning",
  "trades": false,
  "cron_id": "morning-dca-0945",
  "supersedes_cron_id": null,
  "scheduled_for_et": "09:45",
  "scheduled_window_start_et": "09:40",
  "scheduled_window_end_et": "09:50",
  "manual_run": false,
  "run_id": "morning_YYYYMMDD_0945ET",
  "state_schema_version": "morning-watchlist-v1",
  "valid_afternoon_evidence": false
}
```

A scheduled live run:

```json
{
  "run_origin": "scheduled",
  "run_type": "morning_dca_live",
  "stream": "morning",
  "trades": true,
  "cron_id": "42173154",
  "supersedes_cron_id": null,
  "scheduled_for_et": "10:30",
  "scheduled_window_start_et": "10:25",
  "scheduled_window_end_et": "10:35",
  "manual_run": false,
  "run_id": "morning_live_YYYYMMDD_1030ET",
  "state_schema_version": "morning-live-v2-preauthorized",
  "execution_broker": "robinhood",
  "account_number": "562057042",
  "preauthorized_direct_place": true,
  "valid_morning_evidence": true,
  "valid_afternoon_evidence": false
}
```

## Usage

```python
from datetime import datetime
from morning_dca import MorningConfig, MorningDCAExecutor
from near_close_dca import EASTERN

# Pre-flight validation (9:45 AM, read-only): emits eligible_to_execute per ticker.
preflight = MorningDCAExecutor(
    MorningConfig(tickers=("AAPL", "MSFT", "NVDA")),
    mode="preflight",
)

# Live (10:30 AM) execution mode: loads the same-day pre-flight and evaluates
# only eligible_to_execute=true tickers.
live = MorningDCAExecutor(
    MorningConfig(tickers=("AAPL", "MSFT", "NVDA")),
    mode="live",
)

now = datetime.now(tz=EASTERN)
result = live.run(now, robinhood_data_provider)
```

## Recovery record — 2026-07-10 10:30 AM ET morning live run

On **Fri Jul 10 2026** the morning 10:30 live execution was recovered and its
artifacts persisted under
`/home/user/workspace/cron_tracking/morning_dca/20260710_1030ET/`
(`run_state.json`, `run_summary.json`, `submitted_orders.csv`,
`filled_orders.csv`). The run placed **14 filled morning orders totaling $78.00**
on the shared $3-base 0x-to-5x ladder. These are **valid morning evidence only** —
they carry `stream='morning'` and `valid_afternoon_evidence=false`, and count
only as **prior-allocation context** for the afternoon same-day top-up guard
(never afternoon scheduled evidence, slippage, watchdog confirmation, scheduled
cost basis, or scheduled fill counts).

| Ticker | Amount (USD) | Avg fill price |
|---|---|---|
| DFTX | $13.50 | 45.1150 |
| LUNR | $9.00 | 16.0099 |
| NFLX | $7.50 | 72.9950 |
| MRVL | $6.00 | 234.6599 |
| SPCX | $6.00 | 148.3099 |
| QBTS | $6.00 | 20.0999 |
| GLW | $4.50 | 187.1988 |
| LITE | $4.50 | 772.5971 |
| STM | $4.50 | 70.3899 |
| GOOGL | $4.50 | 353.1899 |
| AAPL | $3.00 | 313.3850 |
| GOOG | $3.00 | 351.0700 |
| AMZN | $3.00 | 244.9499 |
| MA | $3.00 | 522.0085 |
| **Total** | **$78.00** | — |

The DFTX fill of **$13.50 = 4.5× $3** is only reachable on the 0x-to-5x ladder
(the former morning-only 0x-to-3x ladder capped at 3× = $9), confirming the
morning live stream now shares the canonical 0x-to-5x ladder.

## Recovery record — 2026-07-13 morning pre-flight repair + recovery batch

On **Mon Jul 13 2026** the scheduled 10:30 AM morning DCA live execution was
**blocked**: the 9:45 pre-flight artifact was missing the machine-readable gate
fields (top-level `overall_ok` and a per-ticker `eligible_to_execute` list), so
the live task could not gate. The fix is the machine-complete pre-flight
contract described above (schema `morning-preflight-v2-machine-complete`).

A same-day **recovery run at ~11:07 AM ET** repaired the 9:45 pre-flight artifact
(re-emitting the machine-complete fields) and placed a **manual recovery /
morning evidence batch of 13 filled orders totaling $66.00**. This is **valid
morning recovery context only** — not afternoon scheduled evidence. Afternoon /
midday tasks may use it **only as same-day top-up context** (like any morning
fill), never as afternoon scheduled evidence, slippage, watchdog confirmation,
scheduled cost basis, or scheduled fill counts.

| Ticker | Planned amount (USD) |
|---|---|
| DFTX | $10.50 |
| TRIN | $3.00 |
| AAAU | $6.00 |
| EVR | $3.00 |
| IRDM | $3.00 |
| ASML | $6.00 |
| STM | $6.00 |
| AVGO | $6.00 |
| NVDA | $4.50 |
| QBTS | $9.00 |
| GS | $3.00 |
| XLG | $3.00 |
| TOPT | $3.00 |
| **Total** | **$66.00** |
