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
  accumulated balance despite no order ever having been submitted.
- Every fire's dollar amount (and the kill-switch ceiling) is floored at $1.00, at the
  single source of truth in `evaluate_inverse_martingale_step()` itself — not only at
  candidate construction — since a schema-valid `unit_usd` below $1 could otherwise
  produce a sub-dollar order the broker rejects, and flooring only the candidate's order
  amount (an earlier, incomplete version of this fix) left the ledger's own claimed/settled
  amount understated relative to what was actually placed.

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
`max_per_order_usd`), the claim's own continuation state (`status: "firing"`, the reduced
remainder) is written immediately, before placement is confirmed. If a second, independent
process reads that exact state and successfully claims the remainder itself before this run
reaches settlement, settlement's own compare-and-swap detects the mismatch and skips writing
rather than clobbering the other process's claim (Codex review: "Serialize partial-fire claims
through settlement"). But a failed CAS is a detection, not a prevention — by the time settlement
discovers the collision, the second process has already committed to placing an order sized off
its own (smaller) `accumulated_before` snapshot; if the first process's placement then fails
outright and needs to roll back to the *full* original balance, that rollback's CAS no longer
matches the now-advanced on-disk state, and the difference between the two processes' snapshots
is silently discarded.

Closing that requires making the window itself unclaimable, not just detecting a claim that
happened inside it: every partial-fire continuation the claim site writes also sets a
`settlement_pending: true` flag, and `evaluate_inverse_martingale_step()` refuses to advance a
`STATUS_FIRING` tracker state at all while that flag is set (`reason: "settlement_pending"`, state
returned unchanged) — closing the "overlapping processes + an ordinary, non-crash placement
failure" variant of the race outright rather than merely detecting it after the fact. Settlement
is the flag's release point: on the common (fully-placed) path it strips `settlement_pending` from
the exact state the claim wrote before persisting it; on the adjustment path (partial/failed/
dry-run placement) the continuation is rebuilt from the pre-claim state, which by construction
predates the flag ever being set. Either way, the very next scheduled run reads a tracker with
`settlement_pending` already cleared, so a legitimate multi-run partial fire continues normally —
the flag only ever blocks a second evaluation that lands *inside* the same run's still-open
claim-to-settlement window.

**Known limitation:** this narrows, but does not eliminate, a real crash-recoverability gap, and
in one respect widens its blast radius. If the whole process crashes between a successful claim
and the deferred settlement step (after `_process_buy_candidates_batch` confirms the real
placement outcome), the claimed balance has no automatic recovery path within this feature — it is
not reconciled against the broker's own order history, and a settlement skipped by the
compare-and-swap above still leaves that run's own placed order unaccounted for in the ledger.
`settlement_pending` deliberately does *not* try to close this crash case: the guard above returns
the tracker state unchanged rather than advancing `trading_days_active`/`last_evaluated_trading_day`
while pending, specifically to avoid introducing a *new* CAS-race variant (a tracking-update write
racing the original claimer's own settlement write). The accepted consequence is that a cycle
stuck `settlement_pending` by a crash — as opposed to overlapping-process contention, which
resolves itself once the crashed process's replacement runs the settlement it never reached — will
not advance `trading_days_active` either, so it will not self-heal via the `max_cycle_days`
abandonment valve the way an ordinary stuck `STATUS_FIRING` cycle would. This is treated as a
natural extension of the limitation already described above, not a new independent gap: both stem
from the same missing piece (a crash between claim and settlement has no reconciliation path), and
this repo's reliability engines (`docs/reliability_spec.md`: `run_state_machine.py`,
`run_ledger.py`, etc.) are explicitly "implemented and unit-tested; wiring into the live decision
path is a tracked follow-up" — not yet available to lean on here. Building a full lease/
reconciliation architecture scoped only to this accumulator would be inconsistent with that state
of the rest of the strategy.

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
