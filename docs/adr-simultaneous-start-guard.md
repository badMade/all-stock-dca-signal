# ADR: simultaneous-start guard

Status: APPROVED by the operator 2026-10-17 (B + C + A, one PR, operator runs the ref_id acceptance test). See "Implementation status" at the end.
Keys: `duplicate_fills`, `competing_executor`. (2026-11-24 audit: the mechanism is delivered and off by default;
no caller outside the tests passes `refetch_orders_fn` or `slot_claim_root` to `dca_runner.execute_plan` yet.)

## Problem

Two executors that start at the same moment (primary + Perplexity backup, or two engines) can each
pull an empty same-window order history and each place the same buys. Sequential protections work
(backup re-pulls the full day, same-window ticker exclusion, same-day top-up gate, partial-batch
completion). Nothing closes the race itself; `docs/adr-plan-first-backup-executor.md` and
`claude/PERPLEXITY-CLAUDE-COORDINATION.md` section 1 record it as accepted. The operator has now
decided it must be fixed.

## What already exists (verified in code)

| Piece | Covers | Gap |
| --- | --- | --- |
| `scripts/local_heartbeat.py` `start`/`claim`/`confirm` over `scheduler_lease` + `file_lock` (atomic, `O_EXCL`, cross-process lock) | Mac-reachable backup vs. local primary: `claim` and `start` share one lock, exactly one wins; a late primary is refused after a live claim (`fallback_claimed`) | Only works when both sides share the Mac filesystem. Prompts must call it (external paste); **no in-repo code path takes it** (`grep` of `SchedulerLease`: only `local_heartbeat.py`) |
| Heartbeat `claim` when no Mac | - | **Skipped** (ADR design gap 2). Cloud-computed backup relies on broker history only |
| `dca_runner.execute_plan` per-order loop | Re-checks clock + that the history snapshot is not stale before EVERY order | It checks the snapshot's *age*; it does **not re-pull** history, so an order landed by another executor mid-batch is invisible until the age limit trips |
| `ref_id = dca-runner-<slot>-<day>-<TICKER>` (deterministic) | Same string from every invocation of the same slot | Not a UUID. The place_equity_order tool describes `ref_id` as a UUID idempotency key and says "the upstream deduplicates by ref_id". Dedup is **documented but never observed** (D6) |
| Three entry points (`run_slot`, executor classes, `dca_runner`) | - | No shared claim; each seeds state differently (CLAUDE.md) |

## Candidate mechanisms

**A. Shared claim/lease before Gate 7.** Strong when a shared medium exists. Impossible across the
Mac and the Perplexity cloud sandbox without one (no shared filesystem, and the Perplexity task
cannot read this repo). Fail-closed variant: "cannot prove the claim, do not submit" is safe but
means the cloud backup never acts when the Mac is down, which defeats its purpose. Therefore A is
right for in-Mac races (primary vs. primary re-run, entry point vs. entry point, Mac backup) and
insufficient alone.

**B. Re-pull broker history before EACH order.** Shrinks the window from "whole batch" to "one
round trip" (seconds) and works on every path including cloud. Does not reach zero: two engines
inside the same few hundred ms still both see empty. Cost: one `get_equity_orders` call per order
(7 slots x up to ~20 tickers); acceptable, and it also catches manual orders. Must fail closed: a
failed or unprovable re-pull halts the batch with the remainder unplaced.

**C. Deterministic broker `ref_id` per (day, slot, ticker).** The only mechanism that is atomic at
the broker and needs no shared medium. Two engines racing to the same (day, slot, ticker) send the
same key, and the broker collapses them. Changes required: derive a UUID5 (stdlib `uuid.uuid5`)
from the current readable string, because the tool contract says UUID; keep the readable string as
the repo-side idempotency key for run artifacts. Risks: (1) dedup is documented only; a wrong
assumption is silent. (2) If dedup returns the *original* order for a replayed key, a legitimate
same-slot, same-ticker re-run after a rejection/cancel could be swallowed (acceptable; matches
"one order per ticker per slot"). (3) Existing ref_id readers (`dca_live_adapters`
`strategy_order_with_ref_id`, crash recovery, `dca_order_plan` plan `ref_id`, order-plan schema)
use the readable form, so the broker-facing UUID must be a derived mapping, not a replacement.
Unverified until an acceptance run (below).

**D. Make the backup wait for primary completion.** Already effectively the policy (backup runs
20 minutes after the primary; Mac heartbeat `in_progress` blocks it). Waiting longer shrinks overlap
but cannot remove it, delays the backup past its useful window (15:40 has little slack before 16:00; it was 15:45),
and does nothing for two primaries. Rejected as a mechanism; keep the existing schedule offset.

## Recommendation: defense in depth, B + C everywhere, A where a medium exists

1. **C (primary guard)**: broker-facing deterministic UUID5 `ref_id`, shared by all three entry
   points via one helper (e.g. `dca_order_ref.broker_ref_id(slot, day, ticker)`), so a fix cannot
   drift between them. Ship first, verified by acceptance run before anyone relies on it.
2. **B (secondary guard, independent of C)**: before each order, re-pull same-window history and
   skip a ticker already ordered (any executor); halt, fail closed, if the pull cannot be proven
   complete. Implemented in `dca_runner.execute_plan` and the executor/`run_slot` placement loops
   and the backup `finalize`/placement path (backup already repulls per order per the ADR; the test
   pins it).
3. **A (in-repo)**: add a small `dca_slot_claim` helper over `scheduler_lease` that
   `run_slot`/executors/`dca_runner` acquire before Gate 7 and release in `finally`; unprovable
   lock => no submit. This covers two local engines and makes the claim a code property instead of
   prompt prose. The cloud backup keeps `claim` when a Mac is reachable and otherwise relies on B + C.
4. D unchanged.

Residual risk after 1-3: two cloud-only executors with no Mac, identical ticker, inside one broker
round trip, **and** broker dedup not actually honoring `ref_id`. The acceptance run exists to
retire that last conjunct.

## Fail-closed rules (all mechanisms)

Lock/claim not provable, re-pull errored/truncated/stale, ref_id derivation failure: no submit, a
structured reason code, alert. No retry or bypass of a permission denial or classifier block.

## Test-first plan

- Race harness: two executors against one fake broker with a barrier so both read empty history;
  assert one order per (slot, ticker). Variants per entry point, and for the backup.
- B: fake history that gains the other engine's order after order k; assert order k+1 for that
  ticker is skipped; assert an unprovable re-pull halts with the remainder unplaced.
- C: UUID5 is stable across processes/run_ids, differs per day/slot/ticker, valid UUID; readable
  key unchanged in artifacts; fake broker that dedups by ref_id.
- A: second claimant refused; lock failure => no submit; lease released on exception; stale lease
  reclaim only after TTL.

## Acceptance-run plan (operator-run, live, small)

1. Verify C once, outside any slot, manual run class (never updates a pointer): with explicit
   operator approval, submit ONE minimum-size order with a fixed UUID `ref_id`, then re-send the
   same `ref_id`; confirm exactly one order in history. If two appear, C is not honored and the
   design relies on A + B only (record in D6).
2. One shadow slot per entry point with B enabled and placement as normal; confirm a per-order
   re-pull count equals orders placed + 1 and no duplicate fills.
3. Two-engine start within the same second on a real slot only after 1 and 2 pass; operator
   watches the order history.

## Open questions for the operator

1. OK to run step 1 (a real minimum-size paired order) for the D6 test?
2. Scope: implement B + C + A in one PR, or C + B first and A as a follow-up?
3. A in `run_slot` changes the live 3:30 PM path; accept that it needs its own acceptance run?
4. Should an unprovable re-pull halt the whole remaining batch (recommended) or only that ticker?

## Implementation status (2026-10-17) -- what implementation found

Reading the code again while implementing corrected parts of the table above:

- **C already exists on the live native path.** `scripts/validate_native_placement.native_ref_id(slot, day, ticker)`
  is a deterministic UUIDv5 (`scripts.idempotency.deterministic_ref_id` of the readable key). The live-mirror
  prompts and `backup_planner` already send it, so a primary and a backup for the same slot/day/ticker send the
  SAME broker key. What is missing is proof the broker collapses duplicates (D6), not code. No ref_id code
  changed in this PR; the readable `dca-runner-...` key stays repo-side only. Step 1 of the acceptance run below
  retires D6.
- **A already exists for the executor classes.** `MorningDCAExecutor.run` (and the three subclasses) holds
  `cross_process_lock(<run_state_dir>/scheduled-slot.lock)` across evaluation, placement and publication of a
  scheduled live slot. The Mac heartbeat `claim`/`start` pair covers Mac-reachable backup vs. primary. The gap
  was `dca_runner.execute_plan`, which had no claim and no re-pull.
- **B was missing everywhere `execute_plan` is used**: it checked the snapshot's age before each order, never
  its content.

### Delivered

- `dca_slot_guard.slot_claim` -- per-(day, slot) claim on `scheduler_lease`; held or unprovable => no submit.
- `dca_runner.execute_plan(..., refetch_orders_fn=, slot_claim_root=, slot_claim_holder=)`:
  fresh history pull before EVERY order; a non-dead (or partially filled) buy for the ticker inside this slot's
  window by any writer skips that ticker (`ExecutionResult.skipped_already_ordered`); a pull that raises or
  returns something unreadable halts the remainder. Exception text is never persisted.
- `scripts/order_refetch.make_refetch_orders_fn(fetch_page_fn)`: the ready-made `refetch_orders_fn` for external
  callers. Pages `get_equity_orders` (cursor), de-duplicates by order id, maps rows with the backup planner's own
  `_to_broker_order`, and raises `OrderRefetchError` on a failure envelope, empty page, bad cursor, endless
  pagination or unreadable row (which `execute_plan` turns into a halt). Tests: `tests/test_order_refetch.py`.
- `dca_runner.REQUIRE_SIMULTANEOUS_START_GUARD` (default **False**): when True a live batch without
  `refetch_orders_fn` is refused. Flip only after the acceptance run; external agents/prompts must pass
  `refetch_orders_fn` (a `get_equity_orders` pull mapped to `BrokerOrder`) first.

### Not delivered / residual

- Cloud-only backup with no Mac still has no shared medium; it relies on B and, once verified, C.
- The 3:30 PM `run_slot` path was not modified: it runs under the documented strategy-state lock around
  `run_pipeline`, and its per-order broker lookups already hit the broker. Wiring a claim there remains a
  separate, acceptance-gated decision (CLAUDE.md: consolidating is a decision, not a cleanup).
- Defaults are off: nothing changes in live behaviour until callers pass the new arguments.

### Acceptance-run plan

1. **D6 (operator-run, manual run class).** One minimum-size buy with a fixed UUID `ref_id`, then the same
   `ref_id` resent. Exactly one order in `get_equity_orders` means the broker dedups; two means C is not honored
   and the design rests on A + B. Record the result in D6.
2. **Shadow slot per caller.** Wire `refetch_orders_fn` in one external caller; confirm pulls == orders + 0 and
   no `skipped_already_ordered` on a quiet slot.
3. **Overlap drill.** Start two runs of one slot within the same second; expect one claim refusal (A) or one
   skip per already-ordered ticker (B), never two buys. Only then flip `REQUIRE_SIMULTANEOUS_START_GUARD`.
