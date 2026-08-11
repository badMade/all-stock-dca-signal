# Live wiring & orchestrator

How the strategy library becomes a running system, and where the safety gates are.

## Modules

| Module | Role |
|---|---|
| `near_close_dca.py` | Pure strategy library: signals, budgets, safeguards, artifacts. Standard library only. |
| `dca_config.py` | Loads `config/*.json` onto `StrategyConfig` defaults (tickers, budgets, sizing ladder, approved trading window). |
| `dca_live_adapters.py` | `RobinhoodDataProvider`, `FinanceDataProvider`, `RobinhoodBroker` — conform to the `MarketDataProvider` / `BrokerAdapter` protocols via **injected callables**. No credentials, no network in-module. |
| `run_dca.py` | Orchestrator + CLI: config → providers/broker → `run_daily_compact` → post-trade reports. Also hosts `run_slot()`, the seven-slot roster entry point. |
| `dca_runner.py` | Committed live-runner entry point for the seven slots (gate sequence, durable state, budget arithmetic, order planning) via **injected callables**. Closes gap #1 in `PRODUCTION_READINESS.md`. See "Three entry points" below. |
| `morning_dca.py` | `MorningDCAExecutor` — class-based per-stream pipeline. Subclassed by the four below. |
| `market_open_dca.py`, `midday_dca.py`, `intraday_dca.py` | Thin `MorningDCAExecutor` subclasses (9:35 / 12:30 / 11:30·1:30·2:30). Override identity + a few flags only. |

### Three entry points coexist — know which one you are editing

This repo contains **three structurally different ways to execute a slot**.
None is dead, but they are not interchangeable, and a fix applied to one does
**not** propagate to the others. Verified 2026-08-03:

| Entry point | Shape | Invoked by | Notes |
|---|---|---|---|
| `run_dca.run_slot()` | Function pipeline over `near_close_dca` primitives | `run_dca.py` itself; the 3:30 PM live path | The only path with a real `__main__` (via `near_close_dca.py`). Re-implements the morning preflight gate rather than calling `MorningDCAExecutor`'s. |
| `MorningDCAExecutor` + 3 subclasses | Class hierarchy, one class per stream | **External agent/prompt** constructing the class and calling `.run(now, data, broker=…)` | **No in-repo caller and no CLI.** Nothing in this repository runs them; `grep` for `MiddayDCAExecutor(` etc. hits only their own modules and tests. |
| `dca_runner.py` | Gate sequence + injected callables | **External agent session** holding MCP runtime access | Deliberate design, not abandoned: it exists so guardrails live in diffable code instead of truncatable prompt text. Covered by `test_live_reliability_contract.py` and pinned by `test_config_invariants.py`. |

**Consequences worth internalizing before changing anything:**

- **Duplicated logic drifts silently.** `run_slot()` carries its own copy of the
  morning preflight gate, with a comment acknowledging the duplication
  (`run_dca.py`: *"routed only through the generic pipeline, `run_slot("1030ET")`
  never called that gate at all"*). A fix to `_apply_preflight_gate()` will not
  reach `run_slot()`, or vice versa.
- **"No in-repo callers" ≠ dead code.** The executor classes, `dca_runner.py`,
  `dca_cost_tracker.py`, `dca_weekly_dashboard.py`, and
  `scripts/same_day_top_up_gate.py` all have zero in-repo callers **by design** —
  this repo is a library that external agent sessions drive. Deleting them
  because grep shows no caller would break live automation that this repository
  cannot see.
- **Consolidating is a real decision, not a cleanup.** Making `run_slot()`
  delegate to the executor classes would remove the duplication, but it changes
  the live 3:30 PM path and needs its own review and acceptance run.

## Dependency injection (no secrets in the repo)

The adapters never call Robinhood directly. The scheduler/agent — which holds MCP
runtime access — passes plain callables that map 1:1 to the Robinhood MCP tools and
return their raw response dicts; all parsing lives in `dca_live_adapters`.

```python
from datetime import datetime
from dca_config import load_strategy_config
from dca_live_adapters import RobinhoodDataProvider, FinanceDataProvider
from dca_runner import state_transaction
from near_close_dca import EASTERN, StrategyState, build_broker
from run_dca import run_pipeline

config = load_strategy_config()  # dry_run=True by default

# `rh_*` wrap the Robinhood MCP tools (get_equity_historicals, get_equity_quotes, …).
robinhood = RobinhoodDataProvider(historicals_fn=rh_historicals, quotes_fn=rh_quotes)
finance = FinanceDataProvider(bars_fn=finance_bars, previous_close_fn=finance_prev_close)
broker = build_broker(
    config,
    review_fn=rh_review,          # review_equity_order
    place_fn=rh_place,            # place_equity_order
    orders_fn=rh_orders,          # get_equity_orders
    tradability_fn=rh_tradability, # get_equity_tradability
    buying_power_fn=rh_buying_power,
    order_fill_fn=rh_order_fill,
    # enable_live_orders=True,    # <- the second gate; leave off until validated
)

# `state_transaction()` -- not `load_strategy_state()`/`save_strategy_state()` called
# separately -- holds an exclusive, cross-process lock for the whole load-mutate-save
# cycle; see "State persistence" below. The seven-slot roster is seven independent
# scheduled processes that can overlap, and loading/saving separately would let two
# overlapping invocations each load the same prior state, mutate their own in-memory
# copy, and then save -- the last writer silently erasing the other invocation's newly
# accepted buy counts and notional totals (Codex review: "Use the transactional wrapper
# in the live wiring example"). It saves on a clean return AND on an exception, since
# `state.record_buy()` mutates state in memory the instant the broker accepts each
# order, long before `run_pipeline()` (or a later artifact/reporting step) returns or
# raises; discarding those mutations because something downstream raised would let the
# next invocation reload pre-order totals and risk a duplicate buy or a later cap
# overrun for orders that already happened.
with state_transaction() as state:
    run_pipeline(
        datetime.now(tz=EASTERN), config, state,
        finance, robinhood, broker,
        artifacts_dir="artifacts/2026-07-06", generate_full_reports=False,
    )
```

Post-trade reporting re-queries `order_fill_fn` for any order still unfilled at trade
time. By default those lookups run sequentially, since `order_fill_fn` is usually a thin
wrapper around MCP calls and MCP client sessions are commonly not thread-safe. Only pass
`build_broker(config, ..., supports_concurrent_fill_lookup=True)` once the wired
`order_fill_fn` has been verified safe to call from multiple threads at once — e.g. it
opens an independent connection/session per call rather than sharing one.

## State persistence (required for the budget safeguards)

`StrategyState` holds the counters the daily/weekly/reserve caps depend on —
`submitted_notional_by_day`, `submitted_notional_by_week`, `weekly_rollover_reserve_usd`,
`reserve_week_key`, `finalized_budget_days`, and per-ticker buy counts. These live only in
memory, and `run_pipeline` does **not** load or save them. Each scheduled invocation is a
fresh process, so the caller **must** persist `StrategyState` across runs — passing a fresh
`StrategyState()` every day silently resets the reserve/weekly accounting and defeats the
documented budget caps. Persist it however the scheduler prefers (e.g. a JSON blob written
next to the daily artifacts and reloaded at the start of the next run); the `run_state.json`
artifact records the same figures for audit but is not auto-rehydrated into `StrategyState`.

`load_strategy_state` / `save_strategy_state` / `state_transaction` all now live in
`dca_runner.py`. Until 2026-07-28 the worked example above referenced them as though they
existed; they did not, anywhere in the repo. `load_strategy_state`/`save_strategy_state`
hand-roll serialisation because `StrategyState` carries JSON-hostile types (`date` dict
keys, `(iso_year, iso_week)` tuple keys, a `set` of dates, a nested `BudgetTier`) that
`json.dump` mangles silently. Writes are atomic (temp file plus `os.replace`), and a
corrupt file raises `StateCorruptError` rather than returning a fresh state, since a silent
reset is the exact failure this section warns about. A persisted document is also rejected
if any notional or the rollover reserve is non-finite/negative, since that would otherwise
let a tampered or malformed value flow straight into the weekly/rollover cap arithmetic.
`selected_tier` is handled differently: `_state_from_json()` never validates a persisted
value against `BUDGET_TIERS` at all -- it unconditionally discards whatever the document
carries and sets `state.selected_tier = None`, tolerating an old file that still has a real
tier object the same way it tolerates one that already carries `null`. This field is
run-scoped (`near_close_dca.StrategyState.select_daily_budget()` only (re)computes it when
`None`), and state is reloaded before every scheduled invocation, so round-tripping a real
value would carry a Stress day's tier into a later day whose candidates justify only Normal
(Codex review: "Do not persist a run-scoped budget tier across runs"; pinned by
`test_state_with_a_tampered_selected_tier_loads_with_it_discarded`). `path` is optional on all three functions: omitted,
it resolves to `dca_paths.stream_root('dca_runner', 'strategy_state.json')`, which is what
makes the worked example above (calling `state_transaction()` with no path) actually work.

`load_strategy_state`/`save_strategy_state` are each individually safe (atomic write,
corrupt-file detection), but nothing about calling them separately serializes the WHOLE
load-mutate-save cycle between them: the seven-slot roster is seven independent scheduled
processes that can overlap, and a `load_strategy_state()` / `try` / `finally:
save_strategy_state(state)` pattern -- calling the two functions directly instead of going
through the transaction -- lets two overlapping invocations each load the same prior state,
mutate their own in-memory copy independently, and then save -- the last writer silently
overwrites the other invocation's new buy counts and notional totals with a stale payload
that never saw them (Codex review: "Serialize concurrent durable-state updates";
"Use the transactional wrapper in the live wiring example"). `dca_runner.state_transaction(path=None)`
closes this: a context manager that holds an exclusive, cross-process `fcntl.flock` on a
sibling `.lock` file for the entire block, loads `StrategyState` on entry, and saves it on
exit -- on a clean return AND on an exception, since `state.record_buy()` mutates state in
memory as soon as the broker accepts each order, long before `run_pipeline()` (or a later
artifact/reporting step) returns or raises; discarding those mutations because something
downstream raised would let the next invocation reload pre-order totals and risk a
duplicate buy or a later cap overrun for orders that already happened (Codex review:
"Persist partial state before releasing the transaction"). The save runs before the lock is
released either way. This is exactly what the worked example above uses -- prefer it over
calling `load_strategy_state`/`save_strategy_state` directly wherever overlapping
invocations are possible.

`save_strategy_state` also now writes to a per-call unique temp filename (a random suffix,
not a fixed `f"{path.name}.tmp"`), so two overlapping direct `save_strategy_state` calls no
longer risk one process's `os.replace()`/cleanup pulling the temp file out from under the
other mid-write -- this alone does not serialize the whole load-mutate-save transaction,
which is what `state_transaction` is for.

## Gate sequence (`dca_runner.assess`)

`dca_runner.assess(slot_id, orders, candidates, now, *, weekly_rollover_reserve_usd=0.0,
weekly_rollover_reserve_week_key=None, buying_power_usd=None)` runs Gates 0, 1, 3, 4 and 6
and returns one of three verdicts:
`END_SILENTLY` (weekend, holiday, early-close afternoon), `ABORT` (alert, place nothing,
never retry) or `PROCEED` (with a costed plan attached). The returned `placement_enabled`
mirrors the verdict -- `True` only for `PROCEED`, `False` otherwise -- but reports this
assessment's own gate outcome only; it is not authorization to call `place_equity_order`
directly. Real placement must go through `execute_plan()`, which reruns the
clock/order-history/budget/account checks fresh immediately before submission, since a
delayed caller or an order placed elsewhere after this assessment ran can make a stale
`placement_enabled=True` no longer true. `execute_plan()` also requires a live `Plan`
object, not this call's JSON `plan` dict, so no caller reaches it by round-tripping
through `assess`'s output -- see "Not yet wired to the live prompts" below: none of the
seven scheduled prompts currently acts on this field at all. `candidates` are `(ticker,
amount_usd)` pairs, `(ticker, amount_usd, multiplier)` triples, or `(ticker, amount_usd,
multiplier, drop_pct)` quadruples; passing the real ladder multiplier and drop is
preferred so a reduced-base high-multiplier signal is not undercounted and the
allocation-priority tie-break sees the real drop instead of treating every candidate as a
0% drop. `weekly_rollover_reserve_usd` should be the raw
`StrategyState.weekly_rollover_reserve_usd` -- never a precomputed effective cap -- since
`assess` derives the effective weekly ceiling itself from the tier IT selects for today's
candidates; a precomputed cap from a caller's own (possibly stale) tier selection could
silently override the ceiling today's actual tier implies. Pass the paired
`StrategyState.reserve_week_key` as `weekly_rollover_reserve_week_key` so a reserve left
over from an earlier, un-rolled week (e.g. state loaded without crossing
`roll_weekly_reserve` first) is zeroed instead of inflating this week's Stress-tier
ceiling; omitting it does NOT skip that check -- `assess` compares the missing key
against the current ISO week the same way it compares any other value, and since
`None` never equals a real week key, a nonzero reserve is zeroed exactly as if a stale
key had been supplied. Supply the real, paired key whenever the reserve is nonzero, or
Stress-tier rollover capacity is lost and valid orders are skipped (Codex review:
"Correct the missing reserve-key behavior in the wiring guide"). `buying_power_usd` is effectively REQUIRED for a
`PROCEED` verdict, despite staying `None`-able in the signature: every executor prompt's
Gate 6 requires an abort when buying power cannot be confirmed, so `assess` returns `ABORT`
when it is omitted (checked after Gates 0/1/3/4, so a weekend/holiday still ends silently)
rather than treating `None` as unlimited headroom the way `plan_orders`' own, lower-level
default does. When given, it is enforced as an additional ceiling on top of the daily/weekly
caps. Neither
`assess` nor `plan_orders` applies the 7-day deployment kill-switch
(`docs/risk_management.md`) -- candidates must already be scaled down (multiplier capped
to 1x) by the caller before reaching `assess` whenever that guard is tripped; see
the now-removed GATE5 proposal doc's §3.6 (see PRODUCTION_READINESS.md gap #1). `dca_runner.execute_plan(plan, broker, *, run_id=None,
snapshot=None, tier=None, trading_day=None, orders=None, weekly_cap_usd=None,
weekly_rollover_reserve_usd=None, weekly_rollover_reserve_week_key=None,
finalized_budget_days=None, submitted_notional_by_week=None, buying_power_usd=None,
orders_account_number=None, now=None, slot_id=None, orders_fetched_at=None, now_fn=None,
candidates=None)` revalidates every order's ticker (whitelist) and amount (finite, positive, in-cap) before the first
placement, since `Plan`/`PlannedOrder` are public and constructible independently of
`plan_orders`. `snapshot`/`tier` are REQUIRED whenever placement is enabled -- pass the
same values used to build `plan` -- so the AGGREGATE daily/weekly caps (and, via
`buying_power_usd`, available buying power) are reapplied here too: per-order checks
alone would still let a caller-built `Plan` combine many individually-valid orders into a
batch that blows through the daily/weekly ceiling. `execute_plan` submits behind a
`_RunnerBroker` that carries both live gates (`dry_run`, mirroring
`StrategyConfig.dry_run`, and `enable_live_orders`) plus the pinned `account_number`; it
validates every placement response (raised exception, failure envelope, missing order id,
or terminal state) rather than trusting a non-raising return, and derives a durable
`ref_id` for each order as `dca-runner-<slot_id>-<trading_day>-<ticker>` -- NOT from
`run_id`, so two overlapping invocations of the same slot (each passing a different
`run_id` that merely contains today's date) derive the identical idempotency key and a
broker can actually deduplicate them. Before the first placement it also verifies
`account_number` itself -- refusing an empty/unset value, the unset-env-var sentinel, a
non-Robinhood-shaped value, and, most importantly, any value that is not an *exact* match
for the environment-configured `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` -- so a wiring mistake
can neither fail every submission silently nor place real buys against a different
accessible account. `buying_power_usd` is REQUIRED (not merely accepted) whenever
placement is enabled -- the absolute buying-power circuit breaker must never be silently
skippable by omitting it. `trading_day` is REQUIRED too, and must equal `snapshot.day`
exactly -- pass the day this batch is actually being submitted for, not necessarily the
day the snapshot happened to be fetched for, so a stale or mismatched snapshot (fetched
earlier and reused, or built for a different day) is refused instead of silently
authorizing a batch with the wrong day's spend already netted out. `orders` -- the same
broker history used to build `snapshot` -- is REQUIRED too: `BudgetSnapshot` carries only
totals, not per-ticker identities, so without it a caller-built `Plan` naming a ticker
already bought earlier that day would pass every other check and submit a second same-day
buy, violating one-order-per-ticker-per-day. The AGGREGATE daily/weekly comparison itself
uses a snapshot recomputed from `orders` (`budget_snapshot(orders, trading_day)`), never
`snapshot`'s own `day_notional_usd`/`week_notional_usd` directly -- `snapshot` is still
required so its `.day` can be checked against `trading_day`, but a caller could otherwise
pass fresh `orders` (for the bought-today check) alongside a stale `snapshot` whose totals
predate a same-day order for a *different* ticker and still authorize a batch beyond real
current spend. `orders_account_number` is REQUIRED too, and must equal `broker.account_number`
exactly: `orders` carries no account identity of its own, so pinning `broker.account_number`
proves only where placement WOULD go, never where the history behind every placement-time
cap check actually came from -- pass the account `orders` was fetched for, so a wiring bug
that fetches history from the wrong (or an empty) account is refused instead of letting the
recomputed snapshot silently understate real prior spend. `now` and `slot_id` are REQUIRED
too, and are used to rerun Gates 0, 1, 3, and 4 (market day, time sanity, approved trading
window, sole-executor interlock) immediately before the first submission: a real Python
caller can build `plan`/`snapshot`/`tier` itself and call `plan_orders()` then
`execute_plan()` WITHOUT ever calling `assess()` (see above), so this is the only place those
gates are guaranteed to run at all -- without them, a caller invoking `execute_plan` after
market close, on a holiday, or long after a stale prior assessment could submit a real order
outside the documented regular-hours-only contract, since a matching `trading_day` alone
satisfies every other check. `now` must be the actual placement-time clock, timezone-aware,
and must fall on the SAME ET calendar day as `trading_day`: `gate_time_sanity` alone only
checks the time-of-day portion of `now` against the slot window on `now`'s own date, never
against `trading_day` itself, so without this cross-check a caller could pass yesterday's
matching snapshot/run_id alongside a merely time-of-day-valid `now`, silently validating the
calendar/budget gates against the stale supplied day while today's real spend is excluded
from every recomputed total. `slot_id` must be one of `dca_runner.SLOTS`' keys, and Gate 4 is
rerun on the freshly materialized `orders`: an existing order for a *different* ticker inside
this slot's window signals a second live executor even though it would not itself collide
with the per-ticker dedup checks. `orders_fetched_at` is REQUIRED too, and must be a
timezone-aware timestamp of when the supplied `orders` was actually fetched, within
`dca_runner.MAX_ORDERS_FETCH_AGE_SECONDS` of `now` -- `orders` itself carries no fetch
timestamp, so a same-account list fetched earlier in the day and reused here after another
slot or a manual buy consumed the remaining cap would otherwise let the recomputed
`verified_snapshot` silently report a stale, too-low total. `run_id` must also contain
exactly one ISO-shaped date, matching `trading_day` exactly -- a run_id carrying more than
one date (e.g. reused across a multi-day batch label) would satisfy a naive substring check
on every one of those days, and a caller reusing a constant `run_id` across days would
otherwise pass this check too, both weakening the day-scoping guarantee `run_id` exists to
provide even though `ref_id` itself (derived from `slot_id`/`trading_day`, not `run_id`) is
unaffected either way.
Whenever `tier.uses_rollover_reserve` is True, `weekly_rollover_reserve_usd`
is REQUIRED: the tier's nominal weekly cap (e.g. Stress's $1,800) overstates the real
ceiling unless a verified rollover reserve is folded in, and a caller-supplied
`weekly_cap_usd` alone only proves a number was given, never that it reflects a real
reserve -- a caller could pass `weekly_cap_usd=1_800` (the tier's own nominal cap, which
already passes every other check) with zero reserve actually earned. `execute_plan` derives
the effective cap internally from `weekly_rollover_reserve_usd`, exactly the way `assess`
derives it, rather than trusting a precomputed override as proof of a real reserve. Pass the
raw `StrategyState.weekly_rollover_reserve_usd`, never a precomputed effective cap, alongside
`weekly_rollover_reserve_week_key` (the paired `StrategyState.reserve_week_key`) so a reserve
left over from an earlier, un-rolled week is zeroed instead of inflating this week's ceiling.
A caller-supplied `weekly_cap_usd` may still further restrict the derived cap, but can never
relax it above what the verified reserve actually supports, and for any tier -- not only ones
that use a rollover reserve -- it must not exceed `tier.weekly_cap_usd`. Neither
`weekly_rollover_reserve_usd` nor its paired week key, by themselves, prove the AMOUNT was
actually earned -- a caller could pass `weekly_rollover_reserve_usd=900` with the current week
key even when no prior week's activity justifies any reserve at all. Whenever
`tier.uses_rollover_reserve` is True and the (possibly week-freshness-zeroed)
`weekly_rollover_reserve_usd` is nonzero, `finalized_budget_days` and
`submitted_notional_by_week` (the same-named `StrategyState` fields) are REQUIRED too, and
`execute_plan` recomputes the expected reserve from them -- the same algorithm
`StrategyState.roll_weekly_reserve()` and `_state_from_json`'s own load-time cross-check use
-- refusing the batch if the supplied `weekly_rollover_reserve_usd` does not match. `run_id` must also literally contain
`trading_day.isoformat()`, not merely be nonempty -- a constant `run_id` reused across days
would otherwise pass this day-scoping check every day, even though `ref_id` itself (derived
from `slot_id`/`trading_day`, not `run_id`) is unaffected either way. A non-cent order amount (e.g. `$9.996`) is
refused outright: every cap/buying-power check uses the raw float, but placement sends the
cent-rounded string the broker actually receives, so an unrounded amount could pass every
check here while exceeding a confirmed ceiling by the rounding delta once submitted. The
placement response itself is checked for identity too, not just order id and state: when
the broker's response carries a `symbol`/`instrument_symbol`/`side`, a mismatch against the
order actually requested (e.g. an MSFT sell confirmation while AAPL was requested) halts the
batch rather than being recorded as a successful submission. `orders` is materialized to a
list internally before either traversal that consumes it (the bought-today check and the
recomputed snapshot), so a single-pass iterable (e.g. a generator) is not silently exhausted
by the first use, leaving the second with nothing. `tier` is validated against a configured
`BUDGET_TIERS` entry by name AND every cap/flag, not merely for finiteness: `BudgetTier` is
public and constructible independently of `select_budget_tier`, so a caller-built tier
naming a real tier but carrying inflated caps must not be trusted at face value. That check
proves the tier's DEFINITION is legitimate, never that it was the correct SELECTION for
today's actual candidates, though. A bare `planned_spend_usd`/`high_multiplier_signal_count`
scalar pair is not enough either: neither is tied to any actual candidate set, so a caller
could just as easily claim a large `planned_spend_usd` for a small real plan. `candidates` --
the SAME raw rows the caller used to select `tier` via `select_budget_tier()` in the first
place -- is therefore REQUIRED too, whenever placement is enabled. `execute_plan` independently
recomputes `planned_spend`/`high_multiplier_signal_count` from `candidates` the same way
`assess()` does (parsing the rows, excluding tickers already bought today per the freshly
materialized `orders`, then deduping/validating in allocation-priority order), calls
`select_budget_tier()` on the result, and refuses to place if it does not match `tier.name` --
so a stale or simply wrong (but otherwise canonical) tier -- e.g. Elevated passed for a run
whose actual candidates would only justify Normal -- is refused rather than trusted at face
value. `candidates` selecting the tier still does not by itself tie `plan.to_place` to those
same candidates, though -- a caller could supply a broad candidate set that escalates tier
selection (e.g. many small candidates whose aggregate spend crosses an escalation threshold)
while submitting only a hand-picked subset of them, riding on the richer tier's daily/weekly
caps for spend a real, full `plan_orders()` run on those same candidates would never have
authorized in isolation. `execute_plan` closes this by rebuilding, from the same deduped and
validated candidate winners, snapshot, tier, `weekly_cap_usd`, and `buying_power_usd` this call
has already verified, the exact `Plan` that `plan_orders()` itself would produce, and requiring
`plan.to_place` to match it exactly -- every submitted order must appear in that rebuilt plan
(checked once each order has already cleared every other per-order validation: ticker shape,
numeric type, finiteness, cap, cent-precision), and the two must contain the same NUMBER of
orders, so a hand-picked subset of an otherwise-legitimate rebuilt plan is refused too, not just
an order matching no candidate at all. A row already IS a `Candidate` (e.g. reusing the same
list passed to `plan_orders()`) is accepted directly rather than requiring a second,
parallel-maintained tuple list -- this is what lets a direct caller pass the exact same
`candidates` to both calls, and is also why that direct-caller pattern is now REQUIRED rather
than merely convenient: `plan` must be built by an actual `plan_orders()` call on the same
verified inputs `execute_plan` re-derives, not hand-assembled. `now_fn`, a REQUIRED zero-argument
callable
returning a fresh timezone-aware datetime, is sampled immediately before every single
placement and reruns the market-day/time-window/trading-window gates against it: a batch with
more than one ticker, or a slow placement call, can otherwise straddle the slot's acceptance
window, since the single pre-loop `now` check only proves the window was open when the batch
started -- making it required rather than merely accepted closes this for every live placement
rather than leaving it an opt-in a caller could forget. The window closing mid-batch halts the
remainder exactly like a placement failure does -- `result.submitted` still reports every
order already placed -- rather than raising, and `now_fn` itself raising is handled the same
way rather than propagating. Behaviour is pinned by `test_dca_runner.py`.

**Not yet wired to the live prompts.** None of the seven files under
`claude/dca-executor-prompts/` actually calls `assess()` or `execute_plan()` today -- each
still restates the same gate logic in prose for the executing LLM session to follow by
reading config files and calling MCP tools directly, and Gate 5 is a hard abort in every
prompt (see `PRODUCTION_READINESS.md`), so `execute_plan()` is never reached from a live
run at all. This module is the committed, tested gate/budget/placement logic gap #1 in
`PRODUCTION_READINESS.md` calls for -- the part that can no longer silently vanish to
prompt-text truncation -- but closing that gap fully still requires wiring each prompt to
call it verbatim instead of restating it, which has not happened yet (Codex review: "Remove
the false claim that prompts invoke the runner").

`assess` returns a JSON-serialisable dict deliberately, because the seven scheduled
prompts are LLM-driven tool-calling sessions with no way to pass a live Python object
between one tool call and the next -- the dict is their only channel. A real Python
caller (e.g. a future non-prompt-driven scheduler) is not bound by that constraint and
should not round-trip through `assess`'s dict to reach `execute_plan`: call `plan_orders`
directly with the same `snapshot`/`tier`/candidates, and pass those same `Plan`,
`BudgetSnapshot`, and `BudgetTier` objects straight into `execute_plan`. Reconstructing
them from `assess`'s JSON output would mean re-deriving `BudgetTier` by name lookup and
rebuilding `PlannedOrder`s field-by-field for no benefit, since the objects were already
available before `assess` serialised them away (Codex review: "Return execution context
from assess").

## Safety gates (both must be flipped to place real orders)

1. **`StrategyConfig.dry_run`** — True by default. While True, `run_once` never calls
   `place_market_buy` on a live broker; it records a simulated `DRY-RUN-SIM-…` order id.
2. **`RobinhoodBroker.enable_live_orders`** — False by default. Even with `dry_run=False`,
   `place_market_buy` returns an `RH-DRYRUN-…` sentinel unless this is True **and** a
   `place_fn` is wired in.

Additional invariants enforced by `run_once`: the broker account must equal
`config.target_account_number` (`<ROBINHOOD_ACCOUNT_NUMBER>`), the trading day must be in
`config.approved_test_dates` (the weekday window in `config/trading_window.json`), and
one order per ticker per stream per day within the daily/weekly/reserve caps and
the **$10 max-per-order** circuit breaker.

## Scheduled-live pre-authorization (approved Fri Jul 10 2026 11:20 AM ET)

Scheduled live DCA tasks are **pre-authorized** to place qualifying buys within
all caps and circuit breakers **without per-order human confirmation**:

- Scheduled live tasks **must not** call `review_equity_order` (it forces a
  preview + confirmation) and **must not** request per-order confirmation. They
  call `place_equity_order` directly **only after** every internal validation,
  signal, budget, spread, confidence/trend, and top-up check passes.
- **Manual / ad hoc runs still use the review + confirmation path**
  (`review_equity_order` first). The `review_fn`/`place_fn` wiring above is the
  manual path; a scheduled live run skips `review_fn`.

The morning 10:30 live run additionally loads the same-day 9:45 **pre-flight**
artifact and evaluates only `eligible_to_execute=true` tickers. See
[safeguards.md](safeguards.md), [morning_dca.md](morning_dca.md), and the
`execution_authorization_policy` / `circuit_breakers` blocks in
[`config/budgets.json`](../config/budgets.json).

## Afternoon placement contract (added Jul 22 2026)

The 3:30 PM ET final live task (`10ce362c`) must not stop at compute-only
planning. The Jul 22 2026 scheduled run produced 20 planned buy orders with
planned spend of $60, but the saved compute module did not call the placement
path and wrote `error_class='placement_not_implemented_in_compute_module'`.
That state is no longer an allowed terminal outcome when planned orders remain.

After `signals.csv` is written, every eligible row with `incremental_usd > 0`
or an equivalent planned order amount must enter the same-run placement phase.
If the saved compute module cannot submit orders, the scheduler must invoke the
main live placement runner or use the direct platform connector path itself.
Scheduled live placement still calls `place_equity_order` only after every
validation, budget, spread, data-confidence, regular-hours, whitelist,
buy-only, max-per-order, and same-day top-up check passes. It still must not
call `review_equity_order`.

If the placement phase is unreachable, the task writes
`order_submit_status='blocked_placement_phase_unreachable'` and
`error_class='placement_phase_unreachable'`, keeps the planned orders in
`signals.csv`, writes `submitted_orders.csv` with headers, notifies, and marks
completed order evidence false while still proving the task fired. It must not
write `order_submit_status='not_submitted'` with
`error_class='placement_not_implemented_in_compute_module'` while planned
orders remain.

Both `run_state.json` and `run_summary.json` must contain the same non-null
`run_classification` object for the 3:30 task. A null
`run_summary.run_classification` is an artifact-contract failure.

## CLI (dry-run only)

```bash
python run_dca.py --demo --report            # deterministic in-memory dry-run + reports
python run_dca.py --demo --now 2026-07-06T15:45 --artifacts-dir artifacts/demo
```

The CLI is dry-run only: a bare Python process has no MCP callables, so live trading is
driven from the scheduler via `run_pipeline`. The CLI refuses to run if
`config.dry_run` is False.

## Local agent quick reference

An agent working locally (or in any fresh session with no prior context loaded) should
reach for an existing entry point before writing new code or re-deriving logic from this
module. A few representative ones, beyond the CLI above:

```bash
python3 -m scripts.same_day_top_up_gate --account <acct> --trading-day <YYYY-MM-DD> \
  --orders-pages <file> --candidates <file>        # authoritative same-day top-up sizing
python scripts/drift_detection.py --run-dir <dir> [--slack]   # post-run drift detection
python -c "import scripts.run_classification"     # scheduled-vs-manual; library API, no CLI --
                                                    # see docs/run_classification.md
```

`scripts/agent_entry_points.py` is the live, authoritative index of these (`python
scripts/agent_entry_points.py --json` for a machine-readable form); the rest are indexed
per-topic in `docs/README.md`. `scripts/tests/test_agent_entry_points.py` keeps that index
from drifting stale.
