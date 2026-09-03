# Inverse-Martingale deferred-accumulation sizing mode

Opt-in, off by default. Implemented in `dca_inverse_martingale.py`, configured via
`config/inverse_martingale.json` (schema: `schemas/inverse_martingale.schema.json`),
wired into the **canonical path only** (`near_close_dca.py` / `run_dca.run_slot()` /
`run_pipeline()`) — see "Scope" below.

## The idea

The normal graded ladder (`near_close_dca.graded_buy_multiplier`) buys progressively
*into* a decline: every newly crossed drop threshold places its own order immediately,
so a deep, sustained decline places more money at worse and worse prices, bounded only
by the daily/weekly budget caps.

This mode does the opposite for any ticker opted in: as price falls through the
ticker's configured tier thresholds, **no order is placed**. Instead, the dollar
amount each tier *would* have bought is tracked ("accumulated"). Only once price has
bottomed and recovered by a configured offset does the accumulated total fire — as a
single order, capped at `max_per_order_usd` per run, with any remainder carried
forward to a later run. If price never recovers to the offset, nothing is ever bought
for that descent.

Worked example (the default tier set): tiers at 1%/2%/4%/8% drop, $1/unit,
1×/2×/4×/8× units. A descent that reaches -4% accumulates $1 + $2 + $4 = $7 (the 8%
tier never crosses). If price then recovers to -3% (1 point off the -4% low, clearing
a 1-point recovery offset), the full $7 fires as one order at that price. The cycle
then resets and rearms for the next fresh dip.

## Enrolling a ticker

Add an entry to `config/inverse_martingale.json`'s `tickers` object:

```json
"AAPL": {
  "enabled": true,
  "basis": "pct",
  "tiers": [
    {"drop_pct": 1.0, "units": 1.0},
    {"drop_pct": 2.0, "units": 2.0},
    {"drop_pct": 4.0, "units": 4.0},
    {"drop_pct": 8.0, "units": 8.0}
  ],
  "recovery_offset_pct": 1.0
}
```

`basis` is either `"pct"` (thresholds in **whole percentage points** against the prior
close — `"drop_pct": 1.0` means a 1% drop, not a 0.01% drop, matching the example
above) or `"stddev"` (thresholds as multiples of a rolling stddev of daily returns,
snapshotted once at cycle-start — see `dca_inverse_martingale.stddev_reference_for_ticker`).
A `"stddev"` entry additionally requires `stddev_window_days` and
`recovery_offset_sigma` instead of `recovery_offset_pct`.

The stddev basis reads the ticker's own OHLCV archive (`trailing_daily_returns`), fully
fail-closed: it refuses (returns no reference — the cycle simply isn't armed that
evaluation) when the archive is absent/unreadable, has too few usable rows for the
configured window, **or** when the archive's single most recent row is more than 7
calendar days older than the evaluation's `as_of` — an archive whose refresh has silently
stopped otherwise still has plenty of old rows to satisfy the window, which would freeze
an obsolete volatility estimate into a new cycle instead of refusing outright.

A row whose `Adj Close` is unusable is never treated identically regardless of where
the gap falls — `_interpolated_adjusted_closes` distinguishes an **interior** gap
(a valid `Adj Close` exists on both sides) from an **edge** gap (a leading or trailing
run with only one side available), and fills each differently:

- **Interior gap:** pure linear interpolation *within* the adjusted series between the
  nearest valid `Adj Close` on either side — never touches raw `Close` or any ratio at
  all (the same class of fix as `ml-forecaster/features.py`'s identical scale-mixing
  problem, though that module's own fix is forward-fill-only, to respect its separate
  per-day no-lookahead training contract — this function has no such constraint, since
  every row it ever touches is already strictly before `as_of`, i.e. already historical
  relative to the live decision being made).
- **Edge gap** (no second anchor to interpolate against): scales the nearest known
  anchor's own adjustment ratio (`Adj Close / Close` at the anchor date) by *this* date's
  own raw `Close`, so real day-to-day price movement still shows up in the computed
  return instead of flattening it to 0% (holding the anchor's adjusted price flat would
  otherwise mask the exact staleness this function's staleness check is meant to catch).
  Falls back to the anchor's own flat adjusted value only when this date's raw `Close`
  is itself unusable.
- **No adjusted value anywhere in the series** (nothing before or after): falls back to
  that date's own raw `Close` directly — there is no adjusted anchor left to scale from.

Only the interior-gap case never touches raw `Close` or a computed ratio; both edge cases
deliberately do, by design — see `_interpolated_adjusted_closes`'s own docstring for the
split/dividend-transition reasoning behind each branch.

**Enrolling a ticker suppresses its normal ladder entirely** — `graded_buy_multiplier`
returns `0.0` for any ticker with an active `config.inverse_martingale` entry, so the
accumulator becomes that ticker's sole buying mechanism. This is deliberate: without
it, an enrolled ticker would be bought twice for the same dip (once immediately by the
ladder, again later by the accumulator's fire).

## Scope (read before assuming this is live everywhere)

This mode is wired into `near_close_dca.py`'s `_run_once_impl` only — the path used by
`run_dca.run_slot()` / `run_pipeline()`, which covers the market-open, midday, and
final 3:30 PM canonical-path streams. It is **not** wired into `MorningDCAExecutor`
(`morning_dca.py`) or `dca_runner.py`'s independent gate-sequence runner — neither
calls `graded_buy_multiplier` or this module at all. An enrolled ticker's dip on a
stream driven by one of those two entry points is bought normally by whatever signal
logic that entry point runs; the accumulator only sees and tracks dips evaluated
through the canonical path. Wiring either of those in is a separate, explicitly
deferred follow-up (`CLAUDE.md`: "consolidating is a decision, not a cleanup").

## Fire mechanics

- One fire chunk per ticker per run, capped at `config.max_per_order_usd` ($10 by
  default). A fire larger than the cap places the capped portion and carries the
  remainder forward (`status: "firing"`) to a later run — the remainder amount is
  frozen at the moment recovery is first detected, so further price movement during a
  multi-run fire never changes it.
- A fire candidate is skipped (deferred, not lost) when a normal-ladder BUY for the same
  ticker already exists this run (`_process_buy_candidates_batch`'s outcome tracking is
  keyed by ticker, so only one candidate per ticker can flow through a single run), or
  when the ticker has a same-day top-up-guarded prior allocation from a *different*
  stream (e.g. `MorningDCAExecutor`, which enrollment does not suppress). In the guarded
  case, only fire *injection* is skipped — tier tracking (accumulation, the cycle low)
  still runs and persists normally every day, guarded or not, so a deepening dip on a
  guarded day is never lost.
- Fired orders carry a `-imart-` ref-id segment (e.g.
  `graded-dca-2026-08-12-AAPL` → `graded-dca-imart-2026-08-12-AAPL`), distinguishing
  them from ordinary ladder buys in `orders.csv` and same-stream duplicate checks. The
  synthetic fire decision also replaces the ticker's original (enrollment-suppressed,
  non-BUY) decision in the run's own `decisions` list, so `buy_candidates.csv` and
  `signals.csv`'s `auto_buy_signal` reconciliation both correctly attribute a real fire —
  submitted OR blocked — to the accumulator rather than misclassifying it as an unrelated
  pending-credit flush with the wrong reason/amount.
- A cycle stuck for `max_cycle_days` (default 30) without firing, or `max_fire_attempts`
  (default 5) without exhausting its balance, is abandoned back to a clean slate — an
  explicit, documented cutoff, not infinite retry. A blocked/failed *live* fire attempt
  (placement never confirmed) counts toward `max_fire_attempts` too, not just a
  kill-switch-reduced partial placement. A **dry-run** outcome (`config.dry_run=True`) is the
  one exception: it never reaches a real broker at all, so it must not consume the same
  retry budget a genuinely blocked live attempt does — a scheduled dry-run/`--demo`
  invocation exercising a recovered cycle repeatedly would otherwise abandon the
  accumulated balance despite no order ever having been submitted. A candidate the
  **consecutive-failure breaker never reached** is the second exception, for exactly the
  same reason and by the same test (`near_close_dca._fire_attempt_reached_the_broker`):
  the batch halted *before* this ticker, so nothing about its own order was ever tried.
  Those rows are `status='skipped'` with `deliberate_skip` on the unattempted tail, or a
  recovered row annotated `today_order_not_attempted`. Counted as attempts, a broker
  outage that trips the breaker on a few consecutive mornings silently abandons every
  accumulated cycle behind it. The row that *tripped* the breaker is deliberately not
  exempt — its own placement really was attempted and really did fail, which is what the
  retry budget counts.
- A fire's dollar amount (and the kill-switch ceiling) is floored at $1.00 — the broker
  rejects a sub-dollar order — at the single source of truth in
  `evaluate_inverse_martingale_step()` itself, not only at candidate construction.
  Flooring only the candidate's order amount (an earlier, incomplete version of this fix)
  left the ledger's own claimed/settled amount understated relative to what was actually
  placed.
- **That floor never spends more than the cycle actually accumulated.** It is clamped to
  the tracked balance, so it only ever raises an amount back toward money the cycle really
  holds — the case where a sub-dollar `per_order_cap_usd` would otherwise squeeze a fire
  below the broker minimum. When the tracked balance is *itself* below $1.00 (a
  schema-valid `unit_usd` under a dollar, with a single-unit first tier), **nothing is
  placed at all**: the balance is held so a deeper later dip can grow it past the minimum,
  and `max_cycle_days` remains the backstop if it never does. Configuring a sub-dollar
  `unit_usd` therefore does *not* produce a $1 order on recovery — it produces a cycle
  that stays active, which is the accumulator's ordinary contract for a descent it cannot
  act on. Rounding a $0.30 balance up to a $1.00 order would spend more than three times
  what the ladder deferred.

## Corporate actions and the post-fire rearm latch

A completed fire leaves a durable `rearm_reference_close` latch that keeps suppressing new
cycles until price exits the descent that fire already paid for. Because it is an absolute
price and the marker is durable, it can outlive a stock split — after which post-split
prices sit on a different scale entirely and the stored reference is meaningless.

Rebasing that reference requires **confirmed** corporate-action evidence, never a price
heuristic. `archived_split_factor()` reads the `Stock Splits` column
`scripts/refresh_active_ticker_histories.py` writes — the data source's own reported ratio
on the split's effective date — and `near_close_dca` passes the result into
`evaluate_inverse_martingale_step()` the same way it passes `stddev_reference`. A confirmed
k-for-1 divides the reference down onto post-split prices; a confirmed 1-for-k divides it
up. Both directions matter: without the reverse case, a reverse split pushes both closes
above the pre-split reference, the latch reads that as recovery and clears, and the same
descent is credited a second time.

The obvious shortcut — treat any close-to-close move near a whole factor as a split — is
**not** safe and was twice tried and removed. An 80 → 120 news gap reads as a reverse
3-for-2 and a ~50% crash reads as a 2-for-1, and moves that violent are entirely ordinary
for the volatile tickers this mode targets.

**Operational consequence.** When the archive is missing, unreadable, or stale (its newest
row more than seven calendar days old), no rebase happens — deliberately, since an
unrebased latch only costs opportunity while a wrongly rebased one can authorize a
duplicate purchase. A ticker that splits while its archive refresh is broken therefore
stays in `awaiting_fresh_dip` until the archive recovers. That is visible as a ticker
which stops buying entirely despite real dips; check the archive's freshness first.

## Mid-cycle configuration changes reset the cycle

An active cycle's `basis`, tier table (thresholds, units), and `unit_usd` are all
snapshotted into the tracker at cycle-start and compared against the live config on every
later evaluation. Any mismatch resets the cycle outright (a clean `resolved_marker`, not an
attempted remap) rather than reinterpreting mismatched units or silently double-crediting an
already-counted tier under its new identity:

- **`basis` changed** (e.g. `pct` → `stddev`) — the persisted per-cycle fields
  (`lowest_drop_seen`, crossed thresholds, `stddev_reference`) were computed under the old
  basis and are meaningless, or actively misleading, under the new one.
- **Tier table changed** (a threshold or a tier's `units` edited) — the persisted
  `crossed_tier_thresholds` set is keyed by numeric threshold value alone, so an edited
  threshold would otherwise read as an uncrossed, brand-new tier.
- **`unit_usd` changed** — the dollar-per-unit multiplier applied to every newly-crossed
  tier's `units` count. A mid-cycle edit here doesn't touch the tier table's identity, but
  changes the *scale* every dollar in `accumulated_usd` is computed at: without this in the
  fingerprint, tiers crossed before the edit would keep dollars banked at the OLD rate while
  tiers crossed after bank dollars at the NEW rate, inside the very same `accumulated_usd`
  total, with no way to recover which portion used which rate.
- **Enrollment disabled** (a ticker's entry removed from `config.inverse_martingale`, or an
  explicit `enabled: false`) — the injection site skips a disabled ticker entirely, before
  ever reading its ledger, and `graded_buy_multiplier` stops suppressing its normal ladder
  path the moment it's disabled. Left alone, the disabled ticker's active cycle would sit
  completely frozen — not aged, not abandoned — while the ladder resumes buying its dips
  normally; a later re-enrollment under the SAME configuration would then wake the stale
  cycle back up and could fire the pre-disable balance on top of whatever the ladder already
  bought for the same descent in the meantime. Disabling enrollment therefore abandons any
  active cycle the same way the other mismatches above do — a future re-enrollment always
  starts genuinely fresh. This cleanup runs as its own pass, independent of whether
  `config.inverse_martingale` is non-empty overall (the tracking/firing block right below it
  is gated on exactly that) — disabling the *last* enrolled ticker (the mapping empties out
  entirely, or the feature is made inactive) must still reset that ticker's cycle, not skip
  past it because the mapping as a whole is now empty. The cleanup pass scans the on-disk
  ledger tree directly rather than `tradable_tickers`, so it also covers a ticker that left
  the active universe entirely, not just one that's still tradable but disabled.

A tracker created before one of these fields existed (e.g. no persisted `unit_usd` in the
fingerprint, or no `basis` at all) is not treated as a mismatch — backward compatible, so
this doesn't retroactively reset every pre-existing cycle the first time the code runs.

A ticker whose decision data is unusable for a session (a provider error, a missing source,
or a cross-source disagreement) is a *different* case — the cycle itself is left completely
untouched, but `trading_days_active` still advances for that session (via the ledger's own
compare-and-swap), so a ticker stuck without usable data for longer than `max_cycle_days`
still ages toward abandonment instead of the age clock silently freezing while data is
unavailable.

## Concurrency

Two overlapping scheduled-stream processes (e.g. a slow or retried earlier slot still running
when a later slot starts) that both read the same pre-fire tracker state could otherwise both
decide to fire and both reach broker placement for the same descent — neither the ledger's
per-write lock nor the same-stream duplicate check would catch it, since these synthetic
candidates are deliberately never `ticker_guarded`.
`InverseMartingaleLedger.claim_fire()` closes this with a compare-and-swap: it atomically writes
the fire only if the on-disk state still equals the exact prior state the caller evaluated from.
A losing claim means another process already claimed the ticker this run; the caller must skip
it entirely.

Deferred settlement (see "Fire mechanics" above) resolves through the *same* `claim_fire()`
compare-and-swap, not an unconditional write — settlement's expected prior state is the exact
state the injection-site claim wrote. When a fire only partially places (kill-switch cap,
`max_per_order_usd`), the claim does **not** write the reduced continuation to the tracker's top
level at all: it writes the untouched *pre-fire* state — the recoverable-lease layout described
in "Crash recovery" below — with the intended reduced continuation held only inside
`settlement_target_state` until placement is confirmed. That same atomic `claim_fire()` write
also sets `settlement_pending: true`.

A second, independent process reading the tracker inside this window therefore sees the SAME
pre-fire state the first claim evaluated from, plus `settlement_pending: true` —
`evaluate_inverse_martingale_step()` refuses to advance a tracker state at all while that flag is
set (`reason: "settlement_pending"`, state returned unchanged), so the second process never even
constructs a candidate state to claim (Codex review: "Serialize partial-fire claims through
settlement"). That closes the "overlapping processes + an ordinary, non-crash placement failure"
variant of the race outright, rather than merely detecting a collision after the fact.

Settlement is the flag's release point: on the common (fully-placed) path it strips
`settlement_pending` from the exact state the claim wrote before persisting it; on the adjustment
path (partial/failed/dry-run placement) the continuation is rebuilt from the pre-claim state,
which by construction predates the flag ever being set. Either way, the very next scheduled run
reads a tracker with `settlement_pending` already cleared, so a legitimate multi-run partial fire
continues normally — the flag only ever blocks a second evaluation that lands *inside* the same
run's still-open claim-to-settlement window.

### Crash recovery: the claim is a recoverable lease

A crash between a successful claim and the deferred settlement step used to have no automatic
recovery path: the claim wrote the already-debited *target* state, so a process that died before
broker placement permanently lost the accumulated balance, and a `settlement_pending` cycle that
nothing ever settled froze forever (the guard above deliberately does not advance
`trading_days_active` while pending, so it never self-heals via the `max_cycle_days` valve
either).

The claim now writes a **recoverable lease** instead: the untouched *pre-fire* state plus the
metadata needed to reconcile it later.

| Field | Purpose |
|---|---|
| `settlement_pending` | The lease flag itself (unchanged semantics) |
| `settlement_ref_id` | The deterministic broker ref the fire is about to be submitted under |
| `settlement_trading_day` | The claiming trading day — a lease may only be released *after* it |
| `settlement_claimed_usd` | How much the claim reserved, to compare against what the broker actually took |
| `settlement_target_state` | The state to apply once the claimed order is proven to have landed in full |
| `settlement_live` | Whether the claiming invocation could reach a real broker at all |
| `settlement_account` | Keyed HMAC identifying the account a **live** claim submits into (never the number, and never a bare digest). A live claim that cannot compute one records the sentinel `"unknown"` rather than omitting the field — an omitted field means a legacy lease, which recovery may still release |

Because the balance is still fully present on disk, a crash before submission loses nothing. A
crash *after* submission is resolved by `_inverse_martingale_recover_pending_leases()`, which runs
at the top of every scheduled `_run_once_impl` — before the enrollment cleanup pass and before the
per-ticker evaluation loop's enrollment/tradability/market-data gates, since broker-ref
reconciliation needs neither a current price nor an active enrollment. For each pending lease it
asks the broker for the claimed ref (`BrokerAdapter.strategy_order_with_ref_id`, which unlike the
live-duplicate guard returns terminal orders too) and then:

Only a **settled** order — filled, or terminal (`cancelled`/`canceled`/`rejected`/`failed`/
`voided`) — has a final executed amount, so only a settled order can resolve a lease:

- **settled for the full claimed amount** → apply `settlement_target_state`;
- **settled for a partial amount** (a kill-switch-capped order, or one cancelled after a partial
  fill) → rebuild a `STATUS_FIRING` continuation for the unfilled remainder, stamped with the
  same retry bookkeeping ordinary deferred partial settlement uses, so the evaluation later in
  the same run recognizes it as a retry rather than synthesizing a second fire under the same ref;
- **no order, or a settled one with no fill, and the claiming day has ended** → release the
  lease, leaving the pre-fire balance intact and claimable;
- **anything unproven** → leave the lease exactly as found. That covers: still the claiming day;
  a broker lookup that failed; a *live* lease seen from a `dry_run` invocation (no order history,
  so absence proves nothing); a recovery run wired to a **different account** than
  `settlement_account` (broker history is per-account, so absence there is meaningless — checked
  only for live leases, since a dry-run claim submitted nothing anywhere — and equally when the
  claim recorded `"unknown"`, or this run cannot compute its own fingerprint); a **history that
  cannot be queried** at all (no `orders_fn` wired) or a response that is not a recognizable
  orders envelope, both of which the recovery lookup raises on rather than reporting as an empty
  history (a single-order envelope counts as unprovable here — it is evidence about one order, not
  a list, and says nothing about whether the history ended); a
  **still-working order** (`partially_filled` and friends — it can still fill the very remainder a
  continuation would re-fire, and inverse-Martingale candidates bypass the ordinary
  duplicate-order guard); and an **unusable executed amount** (non-numeric, non-finite, or
  negative), which is indeterminate rather than a confirmed zero.

A settled order is valued from `executed_notional` when the broker reports it — the same
cent-precise field `scripts/broker_reconciliation._executed_dollars` prefers, in either the scalar
or `{"amount": …}` money shape — falling back to `cumulative_quantity * average_price`. An order
that reports a fill neither representation can value is indeterminate, not a zero. When a settled
partial consumes the whole pre-fire balance the cycle is written as a **resolved marker**, never as
an active cycle holding a zero balance — the ledger validator rejects that combination, so writing
it would corrupt the tracker on its next read.

A lease whose `settlement_live` is false is never looked up at all: it provably never reached a
broker, so it simply expires on the claim-day rule, even under an adapter that cannot query client
ref ids.

Preserving a lease costs one more session of a frozen balance; releasing one wrongly duplicates a
real purchase.

`settlement_account` is a **keyed** HMAC-SHA256, not a plain hash: an account number is 6–12
decimal digits, so a bare `sha256` persisted in a tracker could be brute-forced back to the number
in seconds. The key is a 256-bit random value at `<cron root>/.lease_fingerprint_key` (mode 0600,
created once with `O_EXCL`), deliberately beside the ledger tree rather than inside it, so a
tracker file shared or committed on its own carries nothing enumerable. It is a local secret, not
a managed one — copying the entire artifact root copies the key too.

Recovered capital is booked into `StrategyState` against the **claim** day. A recovery whose claim
day is *today* also gets a `buy_candidates` row — synthesized directly from the outcome when no
decision covers the ticker, which is the norm for the un-enrolled/removed tickers the ledger-wide
pass exists to reach. A **prior-day** recovery deliberately gets no such row: a `run_state`
artifact carries one top-level trading day and its rows carry no claim day, so
`load_prior_same_day_allocations` would read it as an allocation made today and charge it against
today's caps in every later slot. Those recoveries are audited in the accumulator ledger's own
step log instead, which correctly carries the older date.

Capital recovered this way is booked into `StrategyState` (against the **claim** day, not today)
and into the trailing-7-day deployment figure, and published as a candidate outcome — otherwise
the restarted run would compute its daily/weekly/7-day headroom as if the money had never been
spent, and the recovered purchase would be missing from the run artifacts entirely.

**Remaining limitations.**

1. *Reconciliation needs a history-capable broker.* `AlpacaBroker` cannot look up an order by
   client ref id (it sends none), and a `dry_run` invocation has no history at all; in both cases
   a live lease is preserved rather than resolved, and needs a live run with a history-capable
   broker to clear.

2. *Clearing the lease and booking its spend are not one atomic step.* Recovery uses the on-disk
   per-ticker artifact check as its durable idempotency signal, so a prior run that booked the
   spend, persisted its state and wrote its artifact before dying is not double-booked. That
   signal requires a `deployment_history_root` to be wired, and it cannot cover a crash between
   the state write and the artifact write — in those cases the guard falls back to the run-scoped
   `own_orders_recorded` set, which does not survive a restart. Recovery clears
   `settlement_pending` via the ledger's compare-and-swap, then books the recovered notional into
   `StrategyState`/the 7-day figure and finally into the run artifacts. A crash strictly between
   the CAS and the artifact write leaves an accepted broker order that no later run can rediscover
   — the lease that would have pointed at it is gone — so subsequent runs compute cap headroom
   without it. This is not fixable inside this feature: an idempotent replay marker would have to
   be cleared exactly when the artifacts land, which is the same two-phase-commit problem one
   level up, and re-booking from such a marker would double-count whenever the artifacts *did*
   land before the crash. Closing it properly is what this repo's reliability engines
   (`docs/reliability_spec.md`: `run_state_machine.py`, `run_ledger.py`) are for; they remain
   "implemented and unit-tested; wiring into the live decision path is a tracked follow-up" and
   are not leaned on here. The exposure is bounded: it needs a second crash inside the narrow
   window of a run that is already recovering from a first one, and it errs toward *over*-spending
   headroom rather than losing accumulator balance.

3. *A prior-day recovery is absent from deployment history.* Recovered capital is added to
   `deployed_last_7d_usd` for the recovering run, but that figure is rebuilt on every later run by
   `_apply_deployment_history` from `run_state` artifacts — and a prior-day recovery deliberately
   emits no `buy_candidates` row (see the claim-day scoping above), so the spend disappears from
   the kill-switch figure on the next run even while its claim day is still inside the window.
   The two constraints cannot both hold over the current artifact schema: one file carries one
   trading day, and the same-day and deployment loaders read the same rows. Closing it properly
   needs a separate claim-day-aware evidence artifact that the deployment loader consumes and the
   same-day loader ignores — a new cross-run contract, tracked as its own change rather than
   folded in here.

4. *A cross-stream recovery emits no `buy_candidates` row.* A lease outlives the run that claimed
   it, so the afternoon slot routinely recovers a morning or midday claim. Deployment is accounted
   *per stream* and never conflated (`dca_risk`'s "Stream hygiene" contract), and a `run_state`
   artifact carries one stream — every row inside it reads as that stream's deployment. Publishing
   a morning claim's recovery into an afternoon artifact would therefore make every future
   `deployed_by_day()` scan count morning capital as afternoon deployment, permanently, so the row
   is withheld whenever the claiming stream differs from the recovering one (a lease written before
   `settlement_stream` existed carries no provenance and is treated the same way). The cost is that
   the recovery's *attempt* is missing from that run's artifact, which is the accounting the
   zero-fill row exists to preserve. It is the lesser error — the accumulator ledger's own audit log
   still carries the amount, claim day and stream, whereas capital misfiled into the wrong risk
   stream corrupts the kill switch for seven days with nothing to distinguish it from real afternoon
   deployment. The shared daily and weekly caps are unaffected either way: those span the whole
   roster and are charged from `StrategyState` directly. Closing it properly needs the same
   claim-day-aware evidence artifact item 3 describes, extended to carry a stream.

5. *The history query has no upper time bound.* `get_equity_orders` accepts `created_at_gte` but
   no `created_at_lte`, so a lease recovered several trading days late scans everything since its
   claim. The lookup returns on the first matching ref, so a found order never depends on how much
   history sits behind it; only proving *absence* consumes the full history and can hit the
   20-page ceiling, which raises and preserves the lease (fail-closed, but stuck until an operator
   intervenes).

## State

Persisted per ticker (not per trading day — a cycle spans day boundaries) under
`stream_root("inverse_martingale_dca")/<TICKER>/tracker.json`, via
`dca_inverse_martingale.InverseMartingaleLedger`. No file exists for a ticker until its
first tier crossing. A fully resolved or abandoned cycle is **not** deleted outright — a
minimal retry-idempotency marker (`status: "none"` + `last_evaluated_trading_day`) is retained
so a same-day retry of the same run can't misread the reset state as a brand-new dip and
re-credit tiers for a descent already accounted for.

Each cycle persists the `basis` ("pct" or "stddev") and the full tier table (thresholds and
units) it started under; if an operator edits `config/inverse_martingale.json` to change either
while a cycle is active, the next evaluation resets the cycle rather than misinterpreting
mismatched units or double-crediting a tier whose threshold moved (e.g. `1.0` edited to `1.1`)
under its new identity.

Gated on `scheduled`: a manual, ad hoc, or `--demo` invocation never advances a real accumulator
cycle, and a dry-run placement never *settles* one either (mirrors `record_buy()`'s own dry-run
exclusion) — only a confirmed `"submitted"` outcome debits the ledger.

## Tests

- `test_dca_inverse_martingale.py` — pure decision-function and ledger unit tests.
- `test_inverse_martingale_wiring.py` — end-to-end tests through
  `run_daily_compact` → `_run_once_impl` → `_process_buy_candidate`, including the
  default-off regression guarantee (identical behavior when unconfigured).
