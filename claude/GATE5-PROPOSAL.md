# Gate 5 proposal — seven-slot roster vs. the repo's three-stream engine

Status: **PROPOSAL ONLY. Not applied.** This document is committed for review; no live prompt was changed
by it. Written 2026-07-28.

See also `claude/GATE5-ALERT-INCIDENT-2026-07-29.md`: a 2026-07-29 alert
initially looked like a live session not following the disabled-Gate-5
contract this proposal's own recommendation put in place. On review that
conclusion didn't hold up — the prompt text being compared against was
committed *after* the alert fired, so there's no way to confirm what was
actually deployed at the time. That document now reads as inconclusive
rather than a confirmed deviation.

Gate 5 currently says: *"Run the repo's own signal generation for this slot ...
Use the repo entry point, never a reimplementation."* There is no such entry
point. This document says exactly why, proposes one, and flags the two things
that must be decided by a human before any of it goes live.

---

## 1. Findings

### 1.1 `run_dca.py` cannot produce a live buy list at all

`main()` refuses live operation twice, by design:

```python
if not config.dry_run:
    logger.error("Refusing to run: config.dry_run is False. The CLI is dry-run only; "
                 "drive live runs via run_pipeline().")
    return 2
...
if not args.demo:
    logger.error("No live data providers available to the CLI. Run with --demo for a "
                 "local dry-run, or drive a live run from the scheduler via "
                 "run_dca.run_pipeline() with wired MCP callables.")
    return 2
```

So the CLI is demo-only. The live path is `run_dca.run_pipeline(now, config, state,
finance, robinhood, broker, ...)`, called in-process with provider objects the
old Perplexity scheduler injected. A Claude executor cannot satisfy Gate 5 by
shelling out to anything. **Gate 5 has no runnable referent today.**

### 1.2 The repo has three streams, pinned to three specific times

`config/scheduled_tasks.json` → `same_day_top_up_policy.summary`:

> "One same-day top-up guard governs the morning (10:30), midday (12:30), and
> afternoon (3:20) live streams AND manual override same-day fills."

Streams found in config: `morning`, `midday`, `afternoon` (plus non-trading
`preflight`, `scan`, `monitor`, `watchdog`, `slippage`, `dashboard`).
`run_dca.py` hardcodes `AFTERNOON_STREAM = "afternoon"` and its comment ties it
to "the scheduled 3:20 PM ET final live run".

### 1.3 There is already a (stream, window) addressing scheme

`scripts/correlation.py`:

```python
build_correlation_id("2026-07-23", "midday", "1230ET") -> "dca-2026-07-23-midday-1230et"
```

and `scripts/scheduler_lease.py: window_key(trading_day, stream, window)`.
So the codebase already models "a run" as a **(stream, window)** pair. A seven-slot
roster is expressible without inventing a new concept. This is the good news.

### 1.4 THE RISK: the seven-slot roster silently breaks the same-day top-up guard

`same_day_top_up_policy` defines a prior-allocation **chain**:

- midday folds in morning-live + manual same-day fills + missed-morning recovery
- afternoon folds in morning-live + midday-live + manual reruns + recovery

Each later run is supposed to buy only `max(0, desired - prior_same_day)` and only
when its decision price is **strictly below** the prior same-day reference price.

Four of the seven slots (**9:35, 11:30, 1:30, 2:30**) exist nowhere in that chain.
Their fills are not folded in as prior context by anything. A ticker bought at
9:35 is invisible to the guard at 10:30, and a ticker bought at 2:30 is invisible
to the 3:30 run. The result is repeated full-size buys of the same ticker on the
same day, at successively *higher* prices — precisely the outcome the guard exists
to prevent.

The executor prompts do not compensate: Gate 7 only says "No ticker twice in one
batch". Per batch, not per day. Meanwhile `config/budgets.json` asserts
`one_order_per_ticker_per_day: true` **and** `one_order_per_ticker_per_stream: true`.
Nothing currently enforces the per-day form across seven slots.

### 1.5 Second silent-failure path: state roots point at a machine that no longer exists

```python
DEFAULT_MORNING_LIVE_STATE_ROOT = "/home/user/workspace/cron_tracking/morning_dca"
DEFAULT_MIDDAY_LIVE_STATE_ROOT  = "/home/user/workspace/cron_tracking/midday_dca"
DEFAULT_AFTERNOON_TASK_ROOT     = "/home/user/workspace/cron_tracking/ce16ba6b"
```

These are Perplexity-era Linux paths. On the Mac they do not exist. An absent
prior-allocation root "contributes nothing, so this is safe on days a stream did
not run" — but on the Mac it is absent *every* day, so the guard degrades to
"no prior allocation found" and every slot buys full size. It fails open, quietly.

**Resolved (flagged by review, 2026-07-29):** these constants are no longer
hardcoded Linux paths. All three (`morning_dca.DEFAULT_MORNING_LIVE_STATE_ROOT`,
`midday_dca.DEFAULT_MIDDAY_LIVE_STATE_ROOT`, `morning_dca.DEFAULT_AFTERNOON_TASK_ROOT`)
now derive from `dca_paths.stream_root(...)`, which honors
`$DCA_CRON_TRACKING_ROOT` and only falls back to the bundled Linux default
when that variable is unset. Machine-specific repointing is a matter of
setting that one environment variable in the Mac's cron/launchd
configuration, not a code or constant change (Codex review: "Remove the
resolved machine-specific state-root decision").

### 1.6 Stale per-stream caps

`DEFAULT_MORNING_DAILY_CAP_USD = 90.0 / weekly 450.0`, same for midday. These are
the old per-stream halves of a $180/$900 day. The seven-slot model treats
$180/$900 as a **shared** total. If `run_pipeline` enforces its own 90/450 the two
budget models will disagree.

---

## 2. Mapping I assumed

Stated explicitly because a wrong guess here places wrong trades.

| Slot (ET) | stream | window | Basis |
|---|---|---|---|
| 9:35 | morning | 0935ET | new slot, before the canonical 10:30 morning anchor |
| 10:30 | morning | 1030ET | **canonical** historical morning run |
| 11:30 | morning | 1130ET | new slot, still pre-noon |
| 12:30 | midday | 1230ET | **canonical** historical midday run |
| 1:30 | midday | 1330ET | new slot, post-noon, before the 2:30 afternoon boundary |
| 2:30 | afternoon | 1430ET | new slot; 2:30 PM ET is the afternoon boundary |
| 3:30 | afternoon | 1530ET | **canonical** historical afternoon run (was 3:20) |

The afternoon boundary is not a guess: `config/budgets.json` → `risk_guard`
counts deployment only from "scheduled-live afternoon evidence ... timestamp at/after
2:30 PM ET", and the 2:30/3:30 prompts already carry the kill-switch sentence.
The morning/midday split at noon **is** a judgement call and is the main thing to
confirm.

---

## 3. Proposed change to `run_dca.py` (not applied)

```diff
@@
+# Seven-slot roster -> (stream, window). See docs/safeguards.md#live-execution-roster.
+SLOT_MAP: dict[str, tuple[str, str]] = {
+    "0935ET": ("morning",   "0935ET"),
+    "1030ET": ("morning",   "1030ET"),
+    "1130ET": ("morning",   "1130ET"),
+    "1230ET": ("midday",    "1230ET"),
+    "1330ET": ("midday",    "1330ET"),
+    "1430ET": ("afternoon", "1430ET"),
+    "1530ET": ("afternoon", "1530ET"),
+}
+
+# Every slot at or before this one, in execution order, whose same-day fills must
+# be folded in as prior allocation context before sizing a top-up.
+SLOT_ORDER: tuple[str, ...] = (
+    "0935ET", "1030ET", "1130ET", "1230ET", "1330ET", "1430ET", "1530ET",
+)
+
+
+def prior_slots(slot: str) -> tuple[str, ...]:
+    """Slots earlier in the same trading day, for the same-day top-up guard."""
+    return SLOT_ORDER[: SLOT_ORDER.index(slot)]
+
@@ def main(argv: Optional[Sequence[str]] = None) -> int:
     parser.add_argument("--run-label", default="daily", help="Run label recorded in artifacts.")
+    parser.add_argument(
+        "--slot",
+        choices=sorted(SLOT_MAP),
+        required=True,
+        help="Which of the seven roster slots this invocation is. Determines the "
+             "stream, the correlation id window, and which earlier slots are folded "
+             "in as same-day prior allocation.",
+    )
```

and, for the live path, a thin wrapper so the executor has something to call:

```python
def run_slot(slot: str, *, now, config, state, finance, robinhood, broker,
             artifacts_dir=None, generate_full_reports: bool = False) -> dict[str, Any]:
    """Live entry point for one roster slot.

    Wraps run_pipeline() with the slot's stream/window identity and its
    same-day prior-allocation set, so the top-up guard sees every earlier
    slot's fills rather than only the three legacy streams.
    """
    stream, window = SLOT_MAP[slot]
    trading_day = now.astimezone(EASTERN).date().isoformat()
    correlation_id = build_correlation_id(trading_day, stream, window)
    ...
    return run_pipeline(
        now, config, state, finance, robinhood, broker,
        artifacts_dir=artifacts_dir,
        run_label=f"slot-{slot}",
        generate_full_reports=generate_full_reports,
        # NEW: prior allocation must span slots, not just legacy streams
        prior_allocation_slots=prior_slots(slot),
        correlation_id=correlation_id,
    )
```

`run_pipeline` does not currently accept `prior_allocation_slots` or
`correlation_id`; adding them is the real work, and it is where the top-up guard
gets fixed rather than papered over.

---

## 3.5 Two per-slot trend-filter overrides the real entry point must honor

Flagged by review while this document was still proposal-only (2026-07-29), so
they aren't lost once someone implements the entry point:

* **09:35 market-open slot:** only ~5 minutes of regular-session data exist at
  fire time, so a completed 60-minute downward-trend requirement can never be
  satisfied and would silently zero out every market-open candidate. The
  canonical task configuration's `require_trend_validation=false` /
  user-confirmed market-open exemption must be honored here.
* **15:30 final slot:** `config/scheduled_tasks.json`'s
  `final_buy_behavior.ignore_trend_filter=true` and
  `StrategyConfig.ignore_trend_filter` require the final scan to buy a ticker
  below its prior close regardless of the 60-minute trend. Reusing the
  standard trend requirement here would skip buys the final scan is
  specifically required to still consider.

Both are gaps in the *design* for the not-yet-built entry point, not in
anything currently executable: Gate 5 in all seven prompts is a hard abort
today (see the recommendation above), so no trend logic runs anywhere yet.

## 3.6 The 7-day deployment kill-switch must be applied before candidates reach `assess()`

Flagged by review (2026-07-29), same status as 3.5: a design requirement for
the not-yet-built entry point, not a currently-executable gap.

`dca_runner.assess()`/`plan_orders()` plan the candidate notionals they are
given as-is; neither accepts trailing deployment history, and neither applies
`docs/risk_management.md`'s 7-day, 40%-of-monthly-allocation kill-switch. Once
that guard is tripped, every ladder multiplier must be capped at 1x -- but
`assess()` has no way to know the guard is tripped, so a caller that has not
already scaled its candidates down would see them planned and placed at full
size regardless.

This is deliberately NOT implemented inside `assess()`/`plan_orders()`:
`assess()` receives `orders` (this account's own history) but not the
broader trailing-deployment figures the kill-switch needs, and bolting a
second, differently-scoped budget system onto the module that already owns
Gate 6 risked conflating two distinct guards under review-fix time pressure.
The real signal-generation entry point this document proposes must apply the
kill-switch (capping multipliers to 1x while tripped) BEFORE calling
`assess()`, the same way it must already honor the two trend-filter
overrides above. Until that entry point exists, this is not exploitable:
Gate 5 is a hard abort in every prompt, so no candidate ever reaches
`assess()` from a live run today.

## 3.7 One remaining placement-time gap `execute_plan()` cannot close by itself

Flagged by review (2026-07-29), same status as 3.5/3.6: a design requirement
for whatever eventually calls `execute_plan()` live, not a currently-executable
gap -- `execute_plan()` is never called from a live run today (Gate 5 is a
hard abort in every prompt).

This section originally flagged two gaps. The regular-hours recheck has since
been closed: `execute_plan()` now requires a live `now_fn` and samples it
immediately before every `place_fn` call, rerunning both the slot's
acceptance-window check and the order-history freshness check against that
fresh reading (`_now_fn_clock_failure()`), not just the pre-loop `now` passed
into `assess()`. A caller holding a `Plan` across a delay can no longer
silently submit after the window has closed. Only the second gap remains:

* **Atomic sole-executor check.** `gate_sole_executor()` is a read-only
  snapshot of broker history taken before submission; two concurrent
  executor instances for the same slot can both read an empty collision
  list before either has placed anything, then both proceed. This is a
  structural limitation of a read-then-write check, not a bug fixable inside
  `dca_runner.py`: true sole ownership needs an atomic lock (a run/slot lease
  acquired before Gate 4 evaluates, released after Gate 8 confirms) that this
  module has no mechanism to provide on its own. `claude/dca-executor-prompts/0-setup-card.md`
  already flags the overlapping old/new task window as the same class of
  risk during the cutover.

## 3.8 The 3:30 PM slot's acceptance window structurally conflicts with the 3:40 PM watchdog deadline

Flagged by review (2026-07-29): `docs/safeguards.md`'s Afternoon completion
rule requires all afternoon-window (2:30/3:30) trading and artifact-writing
to finish no later than 3:40 PM ET, when the read-only watchdog (`a46bf007`,
`config/scheduled_tasks.json`) begins its final verification. A jittered
3:30 PM firing that lands late in `SLOTS["1530"]`'s current 3:30-4:00 PM ET
acceptance window can pass Gate 1 and still be writing Gate 9 evidence after
3:40, making the watchdog report a valid fire as missing (Codex review: "End
the final slot before the watchdog starts").

This is not fixable by narrowing the window in isolation.
`test_every_slot_window_tolerates_observed_scheduler_jitter` pins every
slot's acceptance window to at least 15 minutes because observed scheduler
jitter has run to 594 seconds (9.9 minutes); a window narrower than that
would legitimately reject on-schedule jittered fires as scheduler faults.
But the 1530 slot's nominal fire time is 15:30 -- any window satisfying the
15-minute floor extends to at least 15:45, which is already past the 3:40
deadline the window is supposed to respect. The two constraints (>=15
minutes of jitter tolerance, and completion by 10 minutes after nominal
fire) cannot both hold for a slot whose nominal time is 15:30.

Closing this needs one of, none of which this module can decide on its own:

* Move the 1530 slot's nominal cron fire time earlier (e.g. to 15:15, so a
  15-minute window ends at 15:30, leaving a 10-minute buffer before 3:40) --
  an external scheduler config change (`config/scheduled_tasks.json` and
  whatever drives the actual cron dispatch), not a `dca_runner.py` change.
* Move the watchdog (`a46bf007`) later than 3:40 PM ET -- also an external
  scheduling change, and one referenced by name and exact timestamp across
  many historical incident records in `config/scheduled_tasks.json` and
  `config/run_classification.json`, so it carries real blast radius beyond
  this repo's tests.
* Accept the residual risk (an occasional false "missing" watchdog report on
  a late-jittered fire that actually completed correctly) as a known,
  documented limitation, and have the watchdog's own verification treat a
  same-day 1530 evidence write arriving shortly after 3:40 as a late-but-valid
  fire rather than a miss -- a change to the watchdog's own task text, not to
  this repo's code.

Until a human picks one of these, `SLOTS["1530"]`'s window stays at
3:30-4:00 PM ET (unchanged) and this conflict is a known, accepted gap
rather than a silently "fixed" one.

## 3.9 `budget_snapshot()` cannot prove the `orders` it receives cover a full week

Flagged by review (2026-07-29): `budget_snapshot(orders, day)` sums whatever
rows `orders` contains into both the daily and weekly totals. Every executor
prompt's Gate 6 correctly instructs a SEPARATE weekly query (`created_at_gte`
set to the ISO week's Monday 00:00 ET, paginated to completion) distinct from
Gate 4's today-only fetch -- but nothing in `budget_snapshot()`'s own inputs
lets it detect a caller who passed the today-only `orders` list (or a
week-scoped query that stopped paginating early) in the place a full week's
history belongs. A prior Monday buy would then silently disappear from
`week_notional_usd`, understating real weekly spend and leaving false
headroom for a batch that breaches the weekly ceiling.

This is not fixable from inside `budget_snapshot()` alone: `BrokerOrder` rows
carry no query-scope metadata (no start-of-range marker, no
pagination-completeness flag, no total-count field), so there is nothing in
the returned data that distinguishes "this is genuinely every order since
Monday" from "this happens to be every order this caller's query returned."
A caller who queries wrong produces output that is structurally
indistinguishable from correct output.

Two options for whoever wires the real caller:

* Thread the query's own `created_at_gte` value into `budget_snapshot()`
  (e.g. a `queried_since: datetime` parameter) and reject when it is later
  than the ISO week's Monday 00:00 ET. This catches the specific mistake of
  reusing Gate 4's today-only fetch for the weekly figure, but does NOT
  prove pagination actually ran to completion -- a caller could still pass a
  correct `queried_since` alongside a response silently truncated by an
  unfollowed pagination cursor.
* Accept that `budget_snapshot()`'s correctness for the weekly total is an
  input-discipline contract enforced by the caller (as the prompt text
  already instructs), not something the function itself can verify, and
  treat any drift as an operational bug to catch via reconciliation
  (`scripts/broker_reconciliation.py`) rather than a runtime check here.

No code change is made for this finding; it is recorded as a known,
structural limitation of validating derived data with no way to check the
completeness of what fed it (Codex review: "Require proof that weekly order
history is complete").

## 3.10 `execute_plan()`/`assess()` cannot verify a caller-supplied rollover reserve was actually earned

Flagged by review (2026-07-29), as a direct follow-up to the fix requiring
`weekly_rollover_reserve_usd`/`weekly_rollover_reserve_week_key` whenever
`tier.uses_rollover_reserve` is True (Codex review: "Bind Stress overrides to
verified reserve state"): requiring the two values closes the case where a
caller omits them or reuses the tier's own nominal cap, but does not prove
either value is *correct*. A wiring mistake (or a malicious caller) can still
pass `weekly_rollover_reserve_usd=900` with the current week's key even
though zero reserve was actually rolled forward; with $1,000 of real
current-week spend already on the books, the derived $1,800 effective cap
would authorize another Stress-tier order that should have been refused at
the true $900 (Normal-only) ceiling.

This is the same class of problem as §3.9: the reserve is not something
`assess()`/`execute_plan()` can independently recompute from the inputs they
already receive. `StrategyState.roll_weekly_reserve()` derives the reserve
from *prior*-week spend history persisted in `StrategyState` itself
(`near_close_dca.py:693-724`) -- a value neither function loads or owns.
`orders` (the broker history both functions already validate) is scoped to
the CURRENT day/week only, per every executor prompt's Gate 6 instructions,
and cannot by itself prove what happened in earlier weeks. Accepting a full
`StrategyState` object instead of the two raw scalars would not close the
gap either: `StrategyState` is public and constructible independently of
`load_strategy_state()`, exactly like `BudgetTier`, `Plan`, and `PlannedOrder`
already are, so a caller could fabricate one with the same forged values.

The only fix that actually proves the reserve is real is for `assess()`/
`execute_plan()` to load `StrategyState` from disk themselves (via
`load_strategy_state()`, which already fails closed on a corrupt file) rather
than accepting the reserve as a caller-supplied input at all. That is a
deliberate architecture change this module has not made: `assess()` is
designed as a pure(ish) function over caller-supplied inputs specifically so
it stays importable and testable without filesystem/path coupling (see the
module docstring's "Design notes"), and owning state I/O here would also
require deciding exactly when `roll_weekly_reserve()` runs relative to Gate 6
-- currently the live runner's own caller is responsible for that sequencing
(`near_close_dca.py:6063-6066`, "Roll the reserve forward now, before any
budget query").

No code change is made for this finding. It is recorded as a known,
structural limitation with the same shape as §3.9: the value cannot be
proven correct with the inputs these functions are designed to accept, and
closing it requires a human decision to change the module's I/O-ownership
boundary, not another local validation (Codex review: "Validate rollover
reserve against durable state").

## 3.11 `execute_plan()` verifies a caller-supplied tier against the plan's actual candidates

Flagged by review (2026-07-29), as a follow-up to validating a caller-supplied
`tier` against a canonical `BUDGET_TIERS` entry (Codex review: "Reject
noncanonical budget tier ceilings"): matching a tier's name and caps against
the configured table proves the tier is a *real* one, but not that it is the
*correct* one for this specific batch. `select_budget_tier()` picks a tier
from `planned_spend_usd` and `high_multiplier_signal_count` -- both derived
from the original candidates' ladder multipliers (`near_close_dca.py:551`,
`dca_runner.py`'s own `_estimated_multiplier`/`HIGH_MULTIPLIER_THRESHOLD`
logic). `PlannedOrder` itself still carries only `ticker`/`amount_usd`/`side`
(see its docstring) and does not carry multiplier information -- but this is
now resolved a different way than either option originally proposed below:
`execute_plan()` requires the same `candidates` its caller passed to
`plan_orders()` (Codex review: "Bind tier inputs to the plan's verified
candidates"), independently reparses and dedups them the same way
`plan_orders()` would, recomputes `planned_spend_usd`/
`high_multiplier_signal_count` from that verified set, and refuses to place
if `select_budget_tier()`'s recomputed tier does not match the caller-supplied
`tier` exactly. It goes further still: it rebuilds the expected `Plan` from
those same verified candidates and requires every entry in the caller's
`plan.to_place` to match it cent-exact (Codex review: "Rebuild the submitted
plan from verified candidates"). A caller with today's real spend history
qualifying only for Normal's $180 daily cap can no longer pass the canonical
`high_volatility` tier alongside a plan built from those candidates --
`execute_plan()` now recomputes the expected tier itself rather than trusting
the caller's claim.

This closes the gap for any caller that passes real `candidates` alongside
its `Plan`, which `docs/live_wiring.md` already documents as required. A
caller constructing `Plan`/`PlannedOrder` directly with no real candidates at
all is a documented separate failure mode already covered by `execute_plan()`
requiring `candidates` be present and matching, not a residual case this
section needs to track.

## 3.12 The weekly rollover reserve is derived from this strategy's own recorded spend, not the shared broker budget

Flagged by review (2026-07-29): `dca_runner.py`'s `budget_snapshot()` sums
**every** broker order into the shared day/week totals regardless of which
agent placed it -- "Policy (chosen 2026-07-28): count every order regardless
of `placed_agent`. Manual and automated buying draw on one shared pool." The
weekly rollover reserve does not honor that same policy: both
`near_close_dca.StrategyState.roll_weekly_reserve()` (the authoritative
derivation) and `dca_runner._expected_weekly_rollover_reserve()` (the
verification mirror this module cross-checks a caller-supplied reserve
against) compute "last week's unused Normal-cap capacity" from
`submitted_notional_by_week` alone -- a value only this strategy's own
`record_buy()` calls update. A finalized prior week with, say, $900 of real
manual or legacy-roster broker spend and zero runner-recorded spend earns a
false $900 reserve, letting a later Stress week use the full $1,800 ceiling
despite no unused shared Normal capacity actually existing (Codex review:
"Derive rollover reserve from the shared broker budget").

This is not a `dca_runner.py`-local fix. `_expected_weekly_rollover_reserve()`
is deliberately written to mirror `roll_weekly_reserve()`'s exact derivation
bit-for-bit, so that verifying a caller-supplied reserve against it actually
means something -- changing only the mirror to consult full prior-week broker
history (which neither `assess()` nor `execute_plan()` are given; every
executor prompt's Gate 6 only fetches the CURRENT day/week) would make a
persisted reserve that `roll_weekly_reserve()` legitimately computed fail
this module's own cross-check, turning correct operation into a permanent,
spurious `ABORT`. The actual root cause -- `roll_weekly_reserve()` itself --
lives in `near_close_dca.py`, the module still powering the currently-live
three-stream engine; changing its reserve derivation is a live-behavior
change to that separate, currently-functioning system, well outside this
seven-slot-roster proposal's scope, and requires deciding where prior-week
full-broker-history would even come from (a new query these prompts don't
run today, potentially spanning many weeks back).

No code change is made for this finding. It is recorded as a known,
structural limitation with the same shape as §3.9/§3.10/§3.11: the
authoritative value this module verifies against is itself derived from
incomplete data, and closing it requires a human decision to change
`near_close_dca.py`'s reserve derivation (and supply it with the broker
history it would then need), not a `dca_runner.py`-local validation (Codex
review: "Derive rollover reserve from the shared broker budget").

## 3.13 `assess()` hard-codes "first slot wins", not the guarded incremental top-up §4 item 2 leaves open

Flagged by review (2026-07-29): once a ticker has any same-day buy attempt,
`assess()`'s `bought_today` exclusion (`REFUSED_ALREADY_BOUGHT_TODAY`) removes
it from `cands` entirely before tier selection or `plan_orders()` ever sees
it -- no later slot can ever buy that ticker again the same day, regardless
of price. `config/budgets.json` is not unambiguous about this being the only
right answer: `order_rules.one_order_per_ticker_per_day: true` reads as
exactly the absolute rule this exclusion implements, but
`circuit_breakers.skip_conditions` in the SAME file also documents "skip a
top-up if the current price is not strictly below the prior same-day buy
reference price" -- language that only makes sense if a guarded top-up (a
SECOND same-day buy, conditioned on a strictly lower price) is meant to be
possible at all. `docs/strategy.md`/`docs/safeguards.md` describe that
guarded-top-up behavior for the OLD three-stream engine's
`same_day_top_up_policy`. §1.4 above already flags this exact tension as one
of the two unresolved per-ticker-per-day questions this proposal cannot
answer on its own (§4 item 2): "(a) genuinely one order per ticker per day,
first slot wins... (b) top-ups allowed at any later slot only at a strictly
lower price."

`assess()`'s current behavior silently picks option (a) -- the simpler,
strictly safer of the two named options, and the one that "materially
reduces deployment" rather than risks it -- but it is still a specific,
consequential choice among the two the human decision in §4 item 2 was
supposed to make. Implementing option (b) is not a small validation fix:
`Candidate` carries no price field at all (only `ticker`/`amount_usd`/
`multiplier`/`drop_pct`), so there is no "prior same-day buy reference
price" for `assess()`/`plan_orders()` to compare a later slot's decision
price against, and computing "the delta" between a prior partial buy and a
new, larger signal requires deciding exactly how that delta is sized (same
ladder tier? a fresh multiplier against the shortfall? capped by the
original tier's caps or a fresh one?) -- decisions with real, product-level
consequences for a live-money strategy, not something to guess at
opportunistically while fixing an unrelated review round.

No code change is made for this finding. `assess()`'s current all-or-nothing
exclusion is left in place as the documented, deliberately conservative
default until a human makes the §4 item 2 decision explicitly; this section
exists so that choice is not mistaken for having been made already just
because the code enforces something (Codex review: "Preserve incremental
top-ups across later slots").

## 4. What must be decided by a human before any of this ships

1. **Is the noon morning/midday boundary right?** Everything else follows from
   config; this one does not.
2. **What is the intended per-ticker-per-day rule across seven slots?** Options:
   (a) genuinely one order per ticker per day, first slot wins — simplest and
   safest, materially reduces deployment; (b) top-ups allowed at any later slot
   only at a strictly lower price, which is what the existing guard intends but
   which requires 1.4 and 1.5 to be fixed first. **Until one of these is chosen
   and enforced, running seven live slots against this engine can buy the same
   ticker up to seven times a day.**

   **Decided 2026-07-30: option (b), guarded top-up.** Applied by no longer
   forcing `one_order_per_ticker_per_day=True` in `run_dca.run_slot()`
   (`run_dca.py`), letting `near_close_dca._process_buy_candidate`'s existing,
   already-tested strictly-lower-price guard run for the three canonical slots
   -- their prior allocation is auto-discovered from the other two slots' own
   state roots, which the guard already understands (see item 3, resolved).
   This decision does NOT extend to the four `DISABLED_SLOTS` (9:35, 11:30,
   1:30, 2:30): §1.4's cross-slot prior-allocation gap for those slots is
   unresolved, so they remain hard-disabled in `run_dca.py` regardless of this
   choice. §3.13's `dca_runner.py`-local analysis is superseded by this: that
   module is not wired into any live path (no production caller ever
   constructs a `dca_runner.Candidate`), so the real fix belongs in
   `run_dca.run_slot()`/`near_close_dca.py`, not there.

   **Two P1 gaps found by review (2026-07-30) after the above landed, both now
   fixed:**
   - **Idempotency keys collided across canonical slots.** `near_close_dca.py`'s
     ref-id scheme (`graded-dca[-topup]-<day>-<ticker>`) carried no stream
     component, so a guarded top-up at 12:30/3:30 could match an earlier
     canonical slot's own real order via `_same_stream_order_exists` and get
     silently recorded as `duplicate_preserved` instead of actually placing the
     incremental order -- the feature would have no-op'd for live (non-dry-run)
     cross-slot top-ups. Fixed by adding a `stream` parameter threaded from
     `run_dca.run_slot()` through `near_close_dca.run_daily_compact` ->
     `run_once` -> `_process_buy_candidates_batch` -> `_process_buy_candidate`,
     used to scope both the ref-id written on placement and the prefixes
     `_same_stream_order_exists` checks (`_graded_dca_ref_id_prefix()`). The
     `"afternoon"` stream (this module's historical default) keeps the exact
     legacy, unprefixed convention byte-for-byte -- `docs/safeguards.md`'s Gate
     4 check and the real legacy `10ce362c` task's own order history depend on
     that literal string -- only `"morning"`/`"midday"` get a stream token.
   - **`evaluate_top_up_guard`'s KNOWN GAP (artifact-only prior) was newly
     live-reachable.** Previously the forced `one_order_per_ticker_per_day=True`
     blanket-blocked any same-day repeat regardless of what the artifact trail
     knew, masking this. Fixed by cross-checking the artifact-derived
     `prior.amount_usd` against a new `BrokerAdapter.same_day_ticker_notional_usd()`
     method (ground-truth sum of every live-state same-day order for the
     ticker, regardless of ref id/stream) before computing the incremental
     delta, and never trusting a figure lower than what the broker reports.
     Implemented for `RobinhoodBroker` (the real, selected backend);
     `AlpacaBroker` fails closed, matching its existing posture for the
     sibling ref-id check (`AlpacaBroker` is not a selected backend in any
     config).
3. **Where does run state live on the Mac?** Resolved (see §1.5): the roots
   now derive from `dca_paths.stream_root()`, which honors
   `$DCA_CRON_TRACKING_ROOT`. Setting that variable to the Mac's real artifact
   path is the remaining, purely-configuration step -- no repointing of
   hardcoded constants is needed.

## 5. Recommendation

Do not enable Gate 5 execution on seven slots yet. The safest interim posture is
the one currently in force: Gate 2 aborts, nothing trades. If slots must run
sooner, run **only the three canonical slots** (10:30, 12:30, 3:30), which map
1:1 onto streams the top-up guard already understands, and leave the other four
disabled until items 1-3 above are resolved.
