# Morning DCA Stream

The **morning** DCA stream grades each ticker's morning price weakness against
**yesterday's regular-session close** on the shared **$2-base 0x-to-5x** live
ladder. As of the **Fri Jul 10 2026** live-automation update every live stream
(market open 9:35, morning 10:30, intraday 11:30/1:30/2:30, midday 12:30, final
3:30) shares one ladder (bound to
`near_close_dca.StrategyConfig.sizing_tiers`); the former morning-only 0x-to-3x
ladder is **historical/superseded**. It is implemented by
[`morning_dca.MorningDCAExecutor`](../morning_dca.py) and governed by
[`config/morning_dca.json`](../config/morning_dca.json).

The stream runs in these modes:

| Mode | Task | ET | Trades | run_type | schema | Artifact root |
|---|---|---|---|---|---|---|
| `live` | `42173154` | 10:30 AM | **yes** | `morning_dca_live` | `morning-live-v2-preauthorized` | `.../cron_tracking/morning_dca/` |
| `watchlist` | `bb4f6ef1` (canonical 9:45 AM slot, read-only) | 9:45 AM | **no** | `morning_dca_watchlist_scan` | `morning-watchlist-v1` | `.../cron_tracking/morning_dca_watchlist/` |

## 9:45 AM watchlist scan (read-only)

An optional **read-only / analytics-only** early-warning scan. It grades morning
weakness on the shared ladder, places **no orders**, runs **no Robinhood order
review**, and calls **no broker placement path**. It writes under
`.../cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/` with run_type
`morning_dca_watchlist_scan`; its dollar amounts are hypothetical/watchlist-only.
The 10:30 live run does not depend on it.

## Retired: the 9:45 AM pre-flight validation (2026-10)

The former 9:45 AM read-only pre-flight validation (`preflight` mode, per-ticker
`eligible_to_execute`, artifact tree `morning_dca_preflight/`) was removed in
October 2026 at the owner's direction. `MorningDCAExecutor(mode="preflight")` is no
longer a valid mode. The 10:30 live stream now scores the **full configured,
non-excluded universe** and reads no 9:45 artifact. The 9:45 watchlist scan
(`bb4f6ef1`) is unchanged.

## 10:30 AM live Robinhood execution

The live execution runs at **10:30 AM ET** (`cron_id=42173154`, cron
`30 14 * * 1-5` = **14:30 UTC weekdays** during EDT) so the trend read has a true
**60-minute intraday window**. It uses **Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>`** as the
**only execution broker** (Alpaca, Finance, and Yahoo stay **read-only** for
validation/account context), the shared **$2 base 0x-to-5x live ladder**, and the
`morning` budget ledger.

It scores the **full configured, non-excluded universe**, **recomputing a fresh
price, prior close, spread, 60-minute downtrend, signal, and budget before
placing**; if the trend cannot be validated it **skips / routes to manual review**
(never auto-buys).

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

The **12:30 midday** (`a6239dae`) and **3:30 final** (`10ce362c`) runs **must
scan** `manual_recovery/YYYYMMDD_*` for same-day manual recovery artifacts and
fold them into the top-up guard as prior allocation. See
`config/morning_dca.json`'s `manual_missed_run_recovery` and
[run_classification.md](run_classification.md).

### 10:30 runner self-containment and state-file guarantee (added Jul 20 2026)

After the Jul 20 2026 10:30 scheduled morning live run failed (it invoked a saved
runner without first generating the same-day raw inputs and escalated without
persisting a scheduled failure-state artifact), the scheduled 10:30 task must:

**Generate raw inputs first.** The task **must not** assume runner inputs already
exist. Before
invoking any saved runner or order module, it **creates**
`/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/raw/` and persists
all required raw inputs: portfolio / buying power, quotes, prior regular-session
close, tradability, `orders_since_midnight` (for same-day top-up context), and the
historical / trend bars needed for the 60-minute trend check. If a runner requires
a `RUN_DATE_ET` argument, pass the current America/New_York date as `YYYYMMDD`. Do
**not** reuse stale files. **This slot's Gate 5 signal generation
(`scripts/generate_morning_candidates.py`, `MORNING_SPEC.source`) is
Robinhood-primary**, not Finance-primary like the rest of the roster, and it has
**no automatic data-source fallback**: Finance and Alpaca are not accepted
`--market-data` keys for this slot at all (an unsupported-key error, not
validation/context), and `yfinance`, while an accepted key, cannot substitute
for a Robinhood outage the way it can for the Finance-primary slots -- a
`robinhood` entry in `source_errors` still aborts Gate 5 (see
[safeguards.md](safeguards.md#market-data-source-policy)'s 1030ET slot
exception and `docs/morning_signal_entry.md`). If raw input generation
fails once, perform **one** bounded recovery attempt on the alternate approved
data source path, then stop.

**Always write the state files.** The 10:30 scheduled run **always** writes
`run_state.json` and refreshes **both** `latest_morning_run.json` and
`latest_morning_live.json` under the morning root as part of the standard
artifact persistence flow, **even when execution is blocked or no orders are
placed** (`run_summary.json` is not written by this task; see
`config/morning_dca.json`'s `artifacts.manifest_accuracy_note`). When a
missing/invalid raw inputs, trend, budget,
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

### Same-day latest-pointer guard

A latest pointer is authoritative **only** for the America/New_York trading day
it names (see "Artifacts" above for what each scheduled run writes into it).
Consumers must validate a pointer before trusting it as current, via
[`scripts/same_day_pointer.py`](../scripts/same_day_pointer.py):
`validate_pointer(pointer, expected_stream=..., expected_root=...)` rejects a
pointer whose `trading_day` is not today, whose `stream` doesn't match, or
whose `run_state_path` resolves outside the expected root or fails to prove
(via its own `run_classification`) that it is a genuine same-day scheduled,
non-manual run. `select_same_day_run_state(...)` prefers a date-specific
`YYYYMMDD_<suffix>/run_state.json` over the pointer when both are present.
This prevents the recurrence of the stale-pointer incident where
`latest_morning_run.json`, `latest_morning_live.json`, and
`latest_midday_run.json` pointed at prior-day runs.

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
| Run type | `scheduled_live_test` @ `15:30` | `morning_dca_watchlist_scan` @ `09:45` / `morning_dca_live` @ `10:30` |
| Pointer | `latest_scheduled_run.json` | `latest_morning_run.json` (+ `latest_morning_live.json` in live mode) |
| Trades | **yes** | watchlist **no** / live **yes** (morning-only) |

Neither morning mode writes into the `ce16ba6b` tree, neither updates
`latest_scheduled_run.json`, and each `run_classification` carries
`valid_afternoon_evidence=false` plus `stream="morning"`. Afternoon consumers
(`67aa802b`, `041d9d90`, `0656657a`) glob only
`.../ce16ba6b/scheduled_runs/**`, so they never traverse either morning root.
(`a46bf007`, formerly also in this list, was removed from the schedule
entirely on 2026-08-02.)

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

`MorningLedger` is a budget session (namespace `morning`) tracking morning
notional. Configurable hooks:

- `morning_daily_cap_usd` (default `180.0`)
- `morning_weekly_cap_usd` (default `900.0`)
- `artifact_paths.cap_seed_roots` — the sibling-stream roots the ledger seeds
  same-day/same-week spend from
- per-mode roots `watchlist_run_state_root` / `live_run_state_root` (legacy
  `morning_run_state_root` mirrors the watchlist root)

Exceeding a cap adds a warning to the run artifact.

> **Corrected 2026-08-03 — the caps here were stale, and the isolation claim
> was wrong.** This section previously documented `90.0` / `450.0` defaults and
> stated the morning ledger "never touches the afternoon daily/weekly ledger."
> Both were superseded by the **2026-08-02 pooling fix** (Geoff: "caps aren't
> pooled the way the target design intends — fix it") and neither was flagged as
> outdated. Actual current behavior, verified in code and config:
> `DEFAULT_MORNING_DAILY_CAP_USD = 180.0` and
> `DEFAULT_MORNING_WEEKLY_CAP_USD = 900.0` (`morning_dca.py`), matching
> `config/morning_dca.json`'s `budget_ledger` block — the **same shared
> Normal-tier ceiling** (`near_close_dca.BUDGET_TIERS[0]`) every stream draws
> from, not an isolated half-share. The ledger now **seeds from every sibling
> stream's same-day/same-week artifacts** via
> `artifact_paths.cap_seed_roots` — `morning_dca`, `market_open_dca`,
> `midday_dca`, `intraday_dca`, the afternoon `ce16ba6b` root, and
> `final_manual_review` — so morning spend and afternoon spend now draw down one
> shared pool rather than two independent ones.
>
> **This is partial parity, not full parity.** Morning (and midday) check only
> against the flat shared **Normal** tier and never independently escalate to
> Elevated / High-volatility / Stress — only the 3:30 final run does that, from
> its fuller same-day view. See
> [safeguards.md](safeguards.md#live-execution-roster).

### Ledger location and bootstrap (added Jul 20 2026)

The **morning stream budget ledger lives under the 10:30 live output directory**
`/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/`, not under the
afternoon `ce16ba6b` tree, or any
`/home/user/workspace/cron_tracking/42173154` path. The 10:30 live task **may create**
`budget_ledger.json` there if it is absent.

The task **does not require a pre-existing ledger file**. If none exists, it
initializes the morning ledger from the configured `morning_daily_cap_usd`
(default `180.0`) and `morning_weekly_cap_usd` (default `900.0`) caps, then writes
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

and updates that root's `latest_morning_run.json`. The **10:30 live** run also
refreshes `latest_morning_live.json` in the same root, so both the run-level and
live-only consumers see the same-day scheduled state; both pointers carry the
same-day guard fields (`pointer_status`, `same_day_required`, `trading_day`,
`updated_at_utc`, and `valid_scheduled_evidence` derived from the run_state).
Manual runs write under a distinct `manual_morning[_live]_*` run id and never
update either pointer.

The **10:30 live** run's `write_artifacts()` **always** writes `run_state.json`
and `morning_signals.csv` (**not** `signals.csv`, and **not**
`run_summary.json`/`validation.csv`)
under `.../cron_tracking/morning_dca/YYYYMMDD_1030ET/`, plus
`submitted_orders.csv` whenever at least one order was actually submitted;
`filled_orders.csv` is **never** written by this task (there is no
fill-confirmation step). Raw connector persistence under a `raw/` subdirectory
is implemented in MorningDCAExecutor (see **Raw data persistence** above). See `config/morning_dca.json`'s `artifacts.manifest_accuracy_note`. The live
`run_classification` block carries `run_origin='scheduled'`,
`run_type='morning_dca_live'`, `scheduled_for_et='10:30'`, `manual_run=false`,
`state_schema_version='morning-live-v2-preauthorized'`,
`run_id='morning_live_YYYYMMDD_1030ET'`, `cron_id='42173154'`,
`execution_broker='robinhood'`, `account_number='<ROBINHOOD_ACCOUNT_NUMBER>'`,
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
  "cron_id": "bb4f6ef1",
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
  "account_number": "<ROBINHOOD_ACCOUNT_NUMBER>",
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

# Live (10:30 AM) execution mode: scores the full configured, non-excluded universe.
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

*Note: the 9:45 pre-flight validation described in this historical record was retired in 2026-10; see "Retired" above.*

On **Mon Jul 13 2026** the scheduled 10:30 AM morning DCA live execution was
**blocked**: the 9:45 pre-flight artifact was missing the machine-readable gate
fields (top-level `overall_ok` and a per-ticker `eligible_to_execute` list), so
the live task could not gate. The fix at the time was the machine-complete pre-flight
contract (schema `morning-preflight-v2-machine-complete`).

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
