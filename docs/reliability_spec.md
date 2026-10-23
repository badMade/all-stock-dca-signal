# DCA Reliability Specification (run-state, reconciliation, artifact contracts)

This is the formal run-state and artifact-contract specification the reliability
plan named as the most valuable first design artifact. It covers the scheduled,
recovery, and zero-buy execution paths, and it doubles as the honest record of
**what is implemented in tracked code, what is a landed-but-not-yet-wired
engine, and what remains pending.** It deliberately does not claim the whole
plan is live: the safety-critical reconciliation and idempotency logic ships here
as tested building blocks, and wiring them into the live decision path is an
explicit, separately-reviewed follow-up.

## 1. Why this exists

The 2026-07-23 incident: a scheduled afternoon run computed a symbol's prior
same-day allocation from persisted artifacts only. A real 12:30 Robinhood fill
was missing from those artifacts, so the prior looked like `$0`, and the top-up
guard planned a full re-buy that collided with the live order. Root causes:

1. Prior allocation was never reconciled against a live, paginated, account-scoped
   broker order query at decision time.
2. When the artifacts showed nothing, the guard **failed open** to the full
   desired amount instead of failing closed.

The modules specified below close those gaps as pure, tested logic.

## 2. Run-state lifecycle

Each execution window is modeled as a state machine
(`scripts/run_state_machine.py`):

```
pending → preflight → reconciled → decided → submitted → verified → published
```

- **`partially_reconciled`** is a non-terminal caveat state entered when
  reconciliation could not be proven complete (see §4). It must resolve to
  `decided` (fail-closed, no buy) or a terminal block — never to `submitted`.
- **Terminal states:** `published`, `blocked`, `skipped`, `timed_out`, `failed`.
- A no-buy run goes `decided → verified → published` (nothing to submit); a buy
  run goes `decided → submitted → verified → published`.
- Illegal transitions (e.g. publishing before reconciling, or resuming a
  terminal window) raise `IllegalTransitionError`.
- **Window-level status** is derived here: a clean `published` run — including a
  successful **zero-buy** run — is distinct from a `skipped`, `blocked`,
  `timed_out`, `partially_reconciled`, or `failed` window.
- **Recovery** resumes from `last_safe_state()` — the last non-terminal state the
  window actually reached — rather than blindly rerunning from `pending`.

## 3. Broker snapshot contract

`scripts/broker_reconciliation.py` builds an account-scoped same-day snapshot
(`same_day_order_snapshot`) that MUST:

- **Pin the account.** `account_number` is required; an empty account raises
  (no reconciliation or submission against an unpinned account).
- **Page all same-day orders** via the `get_equity_orders` `cursor`, with **no
  `placed_agent` pre-filter** — page everything first, then classify.
- **Persist every raw page** (via the injected `raw_sink`) before any derived
  output, satisfying the raw-input contract.
- **Fall back per `order_id`.** For every order id an artifact recorded
  (`expected_order_ids`) that the paged set did not surface, run one bounded
  single-order lookup so an artifact fill is never silently dropped.
- **Fail closed.** An error-shaped page, an exception, a repeated cursor, hitting
  `max_pages`, or an unresolved expected order id all mark the snapshot
  `complete = False`.

## 4. Effective-prior allocation and the fail-closed decision

`reconcile_effective_prior` folds the deduped artifact prior allocation together
with same-day broker exposure into an `effective_prior_usd` per symbol:

- Broker orders whose `order_id` the artifact already counted are **not
  double-counted**.
- A symbol with broker exposure but no artifact record (the 2026-07-23 missed
  fill) surfaces with `broker_only = True`.
- The reference price is the minimum of the artifact reference and any newly-seen
  **confirmed-fill** broker price; any live-but-unconfirmed order **poisons** the
  reference to unknown (a price that isn't a real fill is never compared against).

`decide_effective_top_up` is the decision, and it **fails closed**:

- If coverage is not provably complete → `BROKER_HISTORY_INCOMPLETE`, `$0` buy.
  **Never** fall back to the full desired amount on an unproven-empty history.
- **Operator carve-out for the live prompts (2026-10-22).** The 9:35 AM run aborted at Gate 6
  because `get_equity_orders` returned 81 orders, no cursor, oldest Monday 09:59 ET. The
  `HISTORY COMPLETENESS IS LOG-AND-CONTINUE` paragraph in every
  `claude/dca-executor-prompts/live-mirror/` prompt now says unproven *completeness* of an
  otherwise successful fetch is logged as a WARNING and the run sizes from the history as
  returned. Known risk: an incomplete history can undercount weekly spend and exceed the
  $900 weekly cap. Still fail-closed: tool errors/refusals, an unfollowable cursor, malformed
  rows, rogue in-window orders. The engine above (`decide_effective_top_up`) is unchanged and
  is not live-wired, so this carve-out applies to the prompts only.
- Otherwise the strict-below top-up invariant is delegated to the existing,
  tested `run_classification.incremental_top_up_amount` (buy
  `max(0, desired − effective_prior)` only when `current_price` is strictly below
  the reference price). This spec does not re-implement that comparison.

## 5. Decision reason codes

`scripts/reason_codes.py` is the single vocabulary
(`FIRST_ALLOCATION`, `EFFECTIVE_TOP_UP`, `NO_LOWER_PRICE`,
`ALREADY_MET_OR_OVERFILLED`, `BROKER_HISTORY_INCOMPLETE`, `STALE_POINTER`,
`SAFETY_CAP`, …). It bridges the existing lower-level `top_up_guard_result` and
`pointer_status` codes onto the canonical set rather than replacing the on-disk
contracts, and classifies fail-closed blocks distinctly from benign no-buys.

## 6. Idempotency keys

`scripts/idempotency.py` keys an order by **trade date, execution window, symbol,
strategy version, and allocation tier** (plus a first/top-up `kind`), in a
readable string form and a deterministic UUIDv5 form for brokers requiring a
UUID client-order-id. Two runs (or a run and its recovery) computing the same
intended order produce the same key, so the broker's dedup rejects the duplicate.

## 7. Scheduler leases

`scripts/scheduler_lease.py` gives each window a file lease (atomic
`O_CREAT|O_EXCL`) so a scheduled run and a recovery run for the same window can
never execute concurrently. Leases carry a holder id, the correlation id, and a
TTL; a crashed holder's stale lease is reclaimable only after the TTL, and
release/refresh are holder-checked.

## 8. Run ledger and correlation IDs

- `scripts/run_ledger.py` — one append-only JSONL ledger of scheduled runs,
  recovery reruns, order attempts, fills, and artifact publication, with a
  monotonic sequence and the correlation id on every event.
- `scripts/correlation.py` — a stable incident id derived deterministically from
  `(trading_day, stream, window)` so the scheduler and runner reconstruct the
  same id, with recovery deriving an attributable child id.

## 9. Artifact contracts (existing, referenced)

The versioned-artifact validation, atomic pointer publication, non-null
`run_classification`, and always-written `signals.csv` / `submitted_orders.csv`
contracts are already implemented and tested in `scripts/scheduled_artifacts.py`
and enforced through `run_dca.py` (pointer replaced only after validation
passes). The freshness gate is `scripts/same_day_pointer.py`. This spec does not
restate them; see `docs/run_classification.md` and the schemas.

## 9a. Per-ticker error isolation (afternoon canonical path)

One symbol that raises must cost only itself. The afternoon run
(`near_close_dca._run_once_impl` / `run_dca.run_slot()`) evaluates the whole
universe in a single process, so an unhandled exception anywhere in a
per-ticker loop used to unwind the run: every ticker after the raising one was
never evaluated, and the run ended in `_write_afternoon_failure_artifacts`
rather than completing. Four loops now catch, log, and continue.

| Loop | On a raise | Fail-closed reading |
|---|---|---|
| `_evaluate_all_signals` → per-source provider call (`_safe_signal`) | Logs and degrades that source to `NO_DATA` with `provider_error=True`; `errors_count += 1` (pre-existing; per-ticker logging added 2026-08-24). A provider that *returns* a non-`SourceSignal` instead of raising is normalized to `NO_DATA` at the same boundary | `provider_error` blocks auto-buy via `reconcile_signals`. Normalizing rather than merely catching matters: a malformed value reaches `_signal_row`'s `signal.action.value` while BUILDING THE ARTIFACTS, long after other candidates placed, so a raise there costs the whole run its artifacts rather than one ticker its signal |
| `_evaluate_all_signals` → `reconcile_signals` | Logs; the ticker becomes a `MANUAL_REVIEW` decision; `errors_count += 1` | Never an auto-buy on a decision that could not be computed |
| `_reconcile_cross_day_whole_share_claims` → per unresolved CLAIM (pre-existing) | Logs, appends to the pass's `error_sink`, and moves to the next claim/ticker; the pre-roll pass adds `len(sink)` to `errors_count` | `settle()` is idempotent, so a later run retries it — but the run no longer reports itself healthy while a claim it could not read stays unreconciled |
| `_process_buy_candidates_batch` → `_process_buy_candidate` | Logs; the ticker gets a `"failed"` candidate row carrying `candidate_processing_error`; `errors_count += 1`. An order already accepted before the raise keeps its `submitted` row AND has its notional recovered into `running_deployed_7d` | `"failed"` counts as one consumed buy ATTEMPT (`_PRIOR_ALLOCATION_ATTEMPT_STATUSES`) — an exception can land on either side of a broker submission, so the day's order slot is treated as spent |

Two consequences worth stating explicitly:

- **A real order already accepted by the broker always wins.**
  `_process_buy_candidate` publishes an accepted order through
  `_publish_in_flight_outcome` before any fallible follow-up, so if the raise
  happens after placement the batch keeps that submitted row (annotated with
  `candidate_processing_error`) instead of replacing it with a failure row. The
  capital moved; a later same-day run must still see it.
- **The run is a partial success AND a trading failure, and records both.**
  `errors_count > 0` sets `usage_proxy.manual_recovery_required`, and the
  ticker's row carries `status: "failed"` plus a `candidate_processing_error`
  marker in `run_state.json`/`buy_candidates.csv`. Note that
  `order_submit_status` still reports `submitted` whenever *any* row submitted
  — `_derive_order_submit_status` checks that first — so it is accurate for
  what that field means (orders did reach the broker) but is **not** the
  partial-failure signal; read `manual_recovery_required` and the rows.
  Isolation makes the run *complete*; it never makes the failure *quiet*.

- **A raise after an accepted order fails the run.** `_publish_in_flight_outcome`
  fires the moment the broker accepts, so a published attempt in the isolation
  branch means real capital went out and then something after it broke — and
  this run's deployment accounting for that order is unknown, because
  `_process_buy_candidate` increments a *local* `running_deployed_7d` the raise
  stops it from returning. Reconstructing that figure means mirroring every
  branch's own condition and keeping the copies in step forever; two review
  rounds caught the guess wrong from both directions (under-counting a fresh
  placement, then double-counting a duplicate-preserved recovery). So the batch
  does not guess: it re-raises, the same fail-closed answer a whole-share
  booking failure gets. The published order stays in
  `_IN_FLIGHT_CANDIDATE_OUTCOMES`, which is what the failure writer reads, so
  it still reaches the artifact and no later same-day run re-allocates it. The
  incident this whole change exists for — a ticker the broker *refused* — is
  unaffected: nothing was placed, so it still isolates.

- **Failure reasons carry the exception TYPE, never its message.** An
  unexpected exception from a broker or provider helper can embed an account
  identifier, an access token, or request metadata, and `skip_reason` /
  `candidate_processing_error` are persisted verbatim into `run_state.json`
  and `buy_candidates.csv`, which AGENTS.md's secrets policy forbids. Both
  record `candidate processing raised <Type>`, mirroring `dca_runner`'s
  identical treatment of connector exceptions; the full traceback still
  reaches the operator through `logger.exception`, which is not a repo
  artifact.

- **An accepted whole-share order that never published also fails the run.**
  The isolation branch decides halt-vs-isolate by asking whether an outcome was
  published, and `_process_whole_share_candidate` does a substantial amount of
  fallible work between the broker accepting the order and its first
  `_publish_whole_share_outcome` — an order-history lookup, `int(float(...))`
  over a broker-supplied quantity, ledger settlement. A raise in that window
  reached the branch with nothing published, so it wrote a zero-notional
  `"failed"` row for an order that is live at the broker, leaving its spend out
  of deployment, cap, and attempt accounting. `_mark_whole_share_acceptance`
  records the missing post-broker evidence at the placement site itself (not on
  any return path, so no future branch can forget it); the branch publishes a
  `submitted` row carrying the real order id and then re-raises, exactly like
  the published case above. Deliberately kept out of `candidate_outcomes` until
  that point: a provisional row there would be picked up by the batch loop's own
  mid-call-recovery merge and aggregated with the same order's real outcome.

- **Within-run attempt counting needs no extra booking.**
  `_process_buy_candidate` calls `state.record_buy` immediately after
  `place_market_buy` returns, with nothing between them that can raise, so a
  raise at or after the broker boundary has already consumed its slot in
  `ctx.state` — and a raise before it consumed nothing. The day's
  `max_orders_per_day` accounting stays correct without the isolation branch
  booking a synthetic attempt.

- **Not everything gets a net, deliberately.** The whole-share cross-day
  pre-pass is isolated per *claim*, inside
  `_reconcile_cross_day_whole_share_claims`, and NOT per ticker on top of
  that. The one failure a per-ticker net would newly swallow is a raise from
  `_book_reconciled_whole_share_order`, which runs *after* the claim has been
  durably settled: that recovered order is real spend the ledger will never
  surface again, so continuing would size every later candidate against an
  understated `running_deployed_7d`. A deployment-accounting failure fails the
  run instead — nothing is placed against a total known to be wrong.

Scope: the table above is the afternoon canonical path. The other two entry
points — the executor classes (`morning_dca.py` and its subclasses) and
`dca_runner.py`, see `docs/live_wiring.md`'s "Three entry points" — are covered
in 9b.

## 9b. One rejected ticker never costs the rest of the batch

Two live incidents, same defect on a different entry point:

> `GS, EVR: never submitted — batch halted after RVII rejection`
>
> `RESULT: Batch stopped mid-way at Gate 9. 34 of 37 planned buys placed and
> filled. 3 tickers not bought.`

A ticker-scoped refusal — a symbol that cannot be bought the way the plan
assumed — was being treated as run-scoped evidence, and the tickers behind it
in the plan lost their buy for the day with no mechanism to make it up. All
three entry points now isolate a rejection to its own ticker. None of them
retries a failed order within the same run: isolation changes which OTHER
tickers get their chance, not how a failure itself is handled.

| Entry point | Where | Behavior |
|---|---|---|
| `dca_runner.execute_plan` (Gate 7, reported at Gate 9) | placement loop | **Changed 2026-08-24.** Was `break` on the first failure. Now records a `PlacementFailure` in `result.failures` and continues — for failures that PROVE nothing was accepted (see "Isolation is not unconditional" below) |
| `MorningDCAExecutor._place_broker_orders` (morning/midday/market-open/intraday) | placement loop | Already isolated: every duplicate-check, tradability, spread, review, and placement failure records a per-ticker outcome and `continue`s. Now pinned by `test_definitive_placement_rejection_continues_the_batch` / `..._a_tradability_refusal_...` so it stays that way |
| `near_close_dca._process_buy_candidates_batch` (3:30 PM canonical) | candidate loop | See 9a — including why a raise *after* an accepted order fails the run instead |

Isolation is not unconditional. `execute_plan` still stops the batch outright
for the cases where continuing is wrong rather than merely unlucky:

1. **The window closing mid-batch.** `now_fn` is resampled before every
   placement; placing outside the approved window is unsafe, not just
   unsuccessful.
1. **A failure whose response may hold spend the batch cannot account for.**
   `_placement_response_may_hold_a_live_order` classifies each failed
   response. Only two shapes *prove* nothing was accepted and isolate: no
   usable order id anywhere *and* no state meaning the broker took the order —
   the reported incident's shape, a broker refusing a ticker outright — or
   **every** distinct order the response names, at every level, in a terminal
   state and **explicitly reporting a zero fill**. Everything else halts:

   - A usable order id in a non-terminal state whose symbol, side, client
     reference, or notional does not match the request is an order that exists
     for an amount no pre-flight cap or buying-power check accounted for
     (canonically, `$100` confirmed for a requested `$2`).
   - A **terminal** order that partially filled before it died has already
     moved real money; those shares are in neither `submitted` nor the
     remaining orders' arithmetic. `BrokerOrder.counts_toward_budget` draws
     this distinction for broker history, and the classifier draws it here.
   - A terminal order that simply **omits** its fill figure establishes
     nothing. Absence is not a zero: `dca_live_adapters` documents the exact
     shape (`{"success": false, "data": {"id": …, "state": "cancelled",
     "cumulative_quantity": …}}`) of a partially filled order whose
     cancellation surfaced as the outer failure, and `near_close_dca`'s
     terminal-row collector already requires "an EXPLICIT numeric zero … not
     merely the absence of a positive quantity", noting a real fill's
     `cumulative_quantity` may not have been flushed yet. Requiring an
     explicit zero costs the reported incident nothing, because a refusal
     with no order id never reaches this branch.
   - A fill reported **outside the deepest mapping** counts the same as one
     inside it. Wrappers attach the executed quantity or notional wherever
     they please: on the outer envelope beside `success`, or beside
     `data.order` rather than inside it. `_fill_evidence` therefore runs over
     every distinct envelope level (`_response_envelopes`) and the verdicts
     are combined conservatively (`_combined_fill_evidence`): a positive or
     unreadable figure anywhere settles it, and an explicit zero is the
     verdict only when no level contradicts it. An envelope that reports
     nothing contributes nothing, so an ordinary refusal still isolates.
   - **Every mapping, not one branch.** `_response_envelopes` walks the whole
     response breadth-first rather than following
     `response` → `data` → `data.order`: a wrapper can hang the accepted order
     off a *sibling* of `data` (`{"success": false, "order": {…}, "data":
     {"error": "timeout"}}`) or inside an `orders` array, and the branch walk
     never visited those at all. Bounded by depth and node count, which a real
     response never approaches — and reaching either bound **halts**. Fewer
     envelopes is not the safe direction: an order-bearing branch left
     unvisited reads as no order at all, which isolates, so the walk reports
     truncation and the classifier treats it as unreadable.
   - **One envelope naming two different orders halts.** Its state and fill
     figures are single-valued, so nothing says which of the two they
     describe, and the position-based attribution rule gives no answer.
     `_usable_order_ids` collects both (round 18 made each key *validate*
     independently; this makes each key *count*), and the classifier refuses
     the shape rather than letting the first id's evidence stand for the
     second. One id echoed under both `id` and `order_id` is a single order.
   - **A missing-id `cancelled` or `expired` needs an explicit zero**, exactly
     as it would with an id. Those states mean an order existed and was then
     killed — an order must exist to be cancelled, and be working to expire —
     so it may have partially filled first, and with no id that spend is less
     accountable rather than more. `rejected`/`failed`/`voided` are
     deliberately excluded (`_POST_ACCEPTANCE_TERMINAL_STATES`): those say the
     broker declined and no order was created, which is the reported
     incident's own shape, and including them would halt the first ordinary
     refusal.
   - **More than one order named in one response.** A wrapper can report its
     own request identity on the outer envelope and the real broker order
     inside it, so `_order_identity_groups` collects every level that names an
     order and each order's states come from *its own* envelopes — an id and a
     state identify one order, and reading them off a level naming a different
     order would describe an order that does not exist. Isolating requires all
     of them to be provably dead: one live, state-less, or unreadable order
     among several terminal ones is still a live order.
   - **…but distinct orders, not envelope levels.** A wrapper often echoes one
     id at two levels; that is one order described twice. The levels are
     grouped by id (matched exactly, never case-folded — folding could merge
     two genuinely distinct orders, and merging is the unsafe direction) and
     each order is judged from all of its own levels together, so an ordinary
     rejected order whose zero fill appears on only one of them isolates.
     Two levels of one order that *disagree* — `rejected` outside, `queued`
     inside — still halt.
   - **A working state with no order id at all.** An id is the usual proof
     that the broker accepted something, not the only one:
     `_accepted_state_reported` halts a missing-id response that reports
     `new`/`queued`/`confirmed`/`unconfirmed`/`partially_filled`/`filled`
     (the shared `_LIVE_ORDER_STATES` vocabulary). Deliberately scoped to
     those: unlike the with-an-id branch, where anything unreadable halts, an
     absent, null, terminal, or simply unrecognized state here is the ordinary
     shape of a refusal, and halting on it would restore
     halt-on-first-rejection.
   - **…and that acceptance check is read level by level, not only when the
     response names no order at all.** The per-identity loop below inspects
     only envelopes that *carry* an id, so once any order was named the
     acceptance check was skipped outright and
     `{"state": "queued", "data": {"id": "ORD", "state": "rejected",
     "cumulative_quantity": 0}}` isolated. `_accepted_state_reported` now runs
     over the envelopes that name no order too. Both readings of that shape
     demand a halt, so nothing has to be disambiguated: either the outer level
     describes a second, unnamed order that is live and in nobody's
     arithmetic, or it describes `ORD` in a state contradicting `rejected` —
     the disagreeing-levels case above, which already halts when both states
     sit on id-bearing levels. Scoped to the accepting states only: the
     post-acceptance terminal ones stay with
     `_terminal_order_may_have_filled`, which already reads unattributed
     envelopes and knows when a single named order's explicit zero vouches for
     them — but only as far as genuine *restatements* go, which is narrower
     than the rule first written for it. An unattributed `cancelled`/`expired`
     that **no named order reports** is not restating anything:
     `{"state": "cancelled", "data": {"id": "ORD", "state": "rejected",
     "cumulative_quantity": 0}}` names only a refused order, so that outer
     level describes something else, which may have partially filled before it
     died. Those halt; a state some named order does report stays an ordinary
     echo, governed by the per-order zero-fill rule, so a plain cancellation
     reported at two levels still isolates. `rejected`/`failed`/`voided` are
     excluded here for the usual reason.
   - **A bridge failure halts, exactly as a raise does.** An envelope
     reporting `isError` as the literal boolean `True` — the connector's own
     marker for "this call did not complete" — says the transport failed, not
     whether it failed before the request reached the broker or while carrying
     the broker's acceptance back. `execute_plan` already halts
     unconditionally on a raised `place_fn` on exactly that reasoning, so the
     response-shaped twin gets the same verdict; an explicit zero does not
     rescue it. This does not touch the reported incident's shape: a broker
     refusing a ticker reports `success: false` with an error string and
     carries no `isError` at all.
   - **Arrays are walked all the way down.** `_response_envelopes` iterated a
     list one level and skipped any non-dict item, so an order nested in an
     array *inside* an array — `{"success": false, "results": [[{"id": "ORD",
     "state": "queued"}]]}` — was never visited and, worse, never reported as
     truncated: the classifier saw no identity and no acceptance evidence and
     isolated a live order. Nested sequences are now flattened under the same
     node cap, and a sequence dropped for that cap sets `truncated` exactly as
     a dropped mapping does. The flatten loop is bounded on three axes —
     processed nodes, sequence depth, and a visited-sequence set — because
     queue length alone bounds neither runaway shape: a chain of singleton
     arrays keeps the queue at one element while descending without limit, and
     a self-referential list re-appends itself and never terminates. `place_fn`
     is injected Python and may return either, and an unbounded walk here hangs
     the whole batch before the placement breaker or the window check can run.
     Skipping an already-visited sequence hides nothing: everything reachable
     through a cycle was enumerated on its first visit.
   - **An unusable id does not name an order.** `_is_usable_order_id` applies
     the same validity rules `_placement_failure_reason` uses — rejecting
     falsy, boolean, non-scalar, whitespace-only, non-finite and non-positive
     values — because the two had diverged: this side accepted `"   "`, `-1`
     and `NaN` as identifiers while the failure reader called them no id at
     all. That divergence is not the harmless direction the independence
     argument assumes. Naming an order is what routes a response *past* the
     no-identity branch, where the acceptance-state and success-flag checks
     live, so `{"success": true, "id": "   ", "state": "rejected",
     "cumulative_quantity": 0}` isolated. The traversals stay independent by
     design; a test enforces that their verdicts agree.
   - **…and asking whether an id is usable must never *raise*.**
     `math.isfinite()` converts to a float first, so an arbitrarily large JSON
     integer (`10**1000`) made both validators raise `OverflowError` instead of
     returning a verdict — the same trap `_candidate_is_sortable` and
     `budget_snapshot()` already document and catch for candidate amounts and
     fill figures (Codex review, PR #786: "Treat oversized numeric order IDs as
     unusable"). Where it lands is what makes it severe:
     `_placement_failure_reason` runs *inside* `execute_plan`'s placement `try`,
     so its raise became an ordinary failure, but
     `_placement_response_may_hold_a_live_order` is consulted *outside* that
     `try`, where the same value aborted `execute_plan` and destroyed its
     `ExecutionResult` — losing the record of every order already submitted
     earlier in the batch. `_numeric_value_is_unusable` answers for both, and
     the same `OverflowError` now joins the returned-notional parse. Swept to
     every other best-effort numeric parse a JSON payload can reach —
     `near_close_dca._alpaca_float_or_nan` / `_csv_float` /
     `_usable_positive_float`, `morning_dca._finite_float_or_none` (whose
     docstring said "Never raises"), and `dca_live_adapters._to_float` — each
     of which promises a sentinel and delivered an `OverflowError` instead.
   - **…but an id we cannot USE is not the same as no id at all.** Rejecting
     the oversized id is right; reading that rejection as "the broker named no
     order" is not. The no-identity branch did exactly that and *isolated*
     `{"id": 10**1000}`, while the identical shape carrying an ordinary id is a
     clean **submission** — one response shape cannot mean both "order placed"
     and "no order exists" depending only on how large the number is (Codex
     review, PR #786: "Preserve acceptance evidence from oversized positive
     IDs"). `_untrackable_positive_order_id_reported` treats an unrepresentable
     **positive** numeric id as ambiguity and halts, which is the doctrine
     everywhere else here. Confined to positive values on purpose: `0`/`-1` are
     the documented wrapper sentinels for "no id was assigned" and must keep
     isolating, or every ordinary refusal from such a wrapper would halt.
     `NaN` is not positive either, and is the shape of a failed parse rather
     than an overflowed identifier. All 14 prompts carry the same distinction.

   **And the same question is asked on the success path.** Every rule above is
   consulted only once `_placement_failure_reason` has returned a reason. That
   function validates the ONE order its `response` → `data` → `data.order`
   descent selects, so returning `None` says that order matches the request and
   nothing about the rest of the payload —
   `{"data": {…valid…}, "orders": [{"id": "B", "state": "queued"}]}` was
   recorded as one clean submission while `B` sat live at the broker, absent
   from cap and buying-power arithmetic and from `submitted` (Codex review,
   PR #786: "Inspect sibling orders before accepting a successful response").
   `_accepted_response_names_unaccounted_orders` halts on a second distinct
   identity, an envelope naming two orders, a bridge `isError`, or a truncated
   walk — and deliberately on nothing else, since a response naming exactly one
   order is the ordinary successful shape and must never halt. All 14 prompts
   carry it.

   **A sibling does not need an ID to be an order, though.** The first version
   of that check counted only usable identities and `isError`, so
   `orders: [{"symbol": "MSFT", "state": "filled",
   "executed_dollar_amount": 100}]` beside a validated AAPL order passed
   (Codex review, PR #786: "Inspect ID-less sibling orders on successful
   responses"). Its own justification — skipping states and fill figures
   because the validated order may be `queued` — was true of *that order's*
   envelopes and not a reason to ignore an envelope describing something else.
   What makes the widened check safe is telling **ancestors from siblings**:
   the `response` → `data` → `data.order` chain is the validated order's own
   nesting, so a figure or state there describes that order
   (`{"executed_dollar_amount": 2.00, "data": {…validated…}}` is an ordinary
   success), while anything the breadth-first walk found beside that chain and
   reporting acceptance, an unnamed symbol, or spend is a different order.
   - **An explicit success flag, where no order is named.**
     `_success_flag_reported` halts a response whose `success`/`ok`/`approved`
     is the literal boolean `True`. This is the one signal that reaches the
     classifier having *already* been read by `_placement_failure_reason` —
     which rejected the response only for want of an order id, not because
     anything reported a failure — so `{"success": true}` with no id, no state
     and no fill isolated while the wrapper was saying the order went through.
     Strict booleans, matching that function's own reading of these keys: a
     truthy non-bool (`"true"`, `1`) is arbitrary connector data, and treating
     it as acceptance would halt ordinary refusals. Confined to the
     no-identity branch, because once a response names an order a success flag
     ordinarily describes the API call rather than that order's fate.
   - **The zero-fill question is asked once per named order**, not once for
     the response. Attribution follows position: a figure on an envelope that
     *names* an order describes that order, and a figure on an envelope naming
     none is unattributed. An unattributed *positive* or unreadable figure
     poisons every identity — that is spend nothing accounts for. An
     unattributed *zero* vouches for one order only: with a single named order
     it plainly describes it, but with two it cannot say which of them (or
     whether it is their sum), so past one identity each order needs its own
     explicit zero. So
     `{"id": "REQ", "state": "rejected", "cumulative_quantity": 0, "data":
     {"order": {"id": "ORD", "state": "cancelled"}}}` halts — `REQ`'s zero
     says nothing about `ORD`, an accepted order silent about what it executed
     — while a lone order reported deep whose zero sits on an outer envelope
     that names nothing still isolates. A wrapper that reports the inner
     order's fill on its own identity-bearing envelope halts, which is the
     fail-safe direction for a shape the classifier cannot confidently
     attribute.

   The classifier is deliberately conservative and independent of
   `_placement_failure_reason`'s own traversal: any shape it cannot confidently
   read halts, so divergence can only ever cause an extra halt, never an unsafe
   continue.

   **A raised `place_fn` halts without reaching the classifier at all.** There
   is no response to inspect: a connector that timed out while returning the
   broker's acceptance is indistinguishable from one that failed before the
   request left. This was briefly exempt on the grounds that any order a raise
   leaves is for the amount already validated, so the caps still hold — true,
   and not enough, because an accepted order the caller never learns about is
   also missing from `submitted` and everything downstream reads off it.
   `near_close_dca` already fails its run for this event class once an order
   has been published, isolating only raises it can *prove* came before the
   broker boundary — a distinction this loop cannot make. A broker **refusing**
   a ticker returns a response with no order id and still isolates, so the
   reported incident is unaffected.

   **This rule lives in the prompts too, not only in `dca_runner`.** No prompt
   calls `execute_plan()` yet (`docs/live_wiring.md`), so a Gate 7 instruction
   that said "isolate every rejection and continue" was the version that
   actually ran. All 13 placing prompts now carry the full halt-vs-isolate
   rule, pinned by
   `test_every_placing_prompt_isolates_only_proven_no_order_failures`.
2. **`circuit_breakers.max_consecutive_placement_failures` consecutive
   failures** (`config/budgets.json`, currently 3). ONE knob, **three live
   paths**, so they cannot disagree about when a batch stops:
   `dca_runner.MAX_CONSECUTIVE_PLACEMENT_FAILURES`,
   `StrategyConfig.max_consecutive_placement_failures` (the afternoon
   candidate batch), and `MorningConfig.max_consecutive_placement_failures`
   (the morning/midday/market-open/intraday executors, which inherit
   `MorningDCAExecutor._place_broker_orders`). Wiring it only into
   `dca_runner` — which no prompt calls today — and into prompt prose would
   have meant the breaker never actually fired in a scheduled Python run
   (Codex review, PR #786: "Enforce the streak breaker in every live
   executor"). **Five separate config loaders** reach those loops
   (`load_strategy_config`, `load_morning_config`, `load_stream_config` for
   market-open/intraday, and midday's own `load_midday_config`), so the knob
   has to be added to each; `test_every_live_stream_config_loads_the_streak_breaker`
   pins all five. The schema `require`s the key, because `dca_runner` reads it
   at import time with no fallback.

   What counts is an explicit **`broker_placement_failed`** marker, not the
   outcome status. Status cannot carry this: `"failed"` is also what the
   batch's own last-resort net assigns to a candidate that raised *before*
   reaching the broker (bad quote arithmetic, a ledger error), and three of
   those would trip a broker-health breaker with no placement having failed —
   while `OrderConfirmationRequiredError`, a genuine broker-facing failure that
   produced no order, carries a different status entirely and would never be
   counted (Codex review, PR #786: "Base the breaker on actual placement
   failures", "Count confirmation-required placement failures"). A
   blocked/skipped candidate is a policy decision made before any submission
   and never counts. The whole-share path carries the marker too, on each of
   its no-order branches (a raised placement, a confirmation hold, a confirmed
   terminal zero-fill, a failed reconciliation lookup) — an earlier revision
   left it unmarked on the theory that at most two whole-share tickers cannot
   drive a streak alone, which missed the mixed case: one whole-share failure
   plus two fractional ones is still three real broker failures
   (`test_every_whole_share_no_order_branch_marks_a_broker_failure` guards the
   marking). Recovered orders and claims resolved elsewhere stay unmarked —
   the broker holds a real order in those cases.

   The reset is equally explicit: **`broker_placement_succeeded`**, set only
   where this invocation's own placement is proven accepted by the broker —
   a clean return, or a placement that raised and was then reconciled to a
   live/filled order. A same-stream *duplicate* recovery also reports
   `"submitted"` while surfacing an EARLIER attempt's order and placing
   nothing now, so resetting on status let failure→duplicate→failure→failure
   never trip (Codex review, PR #786: "Reset the breaker only after a fresh
   placement succeeds"), while excluding the reconciled ones made
   failure→proven-accepted→failure→failure trip at three with only two
   consecutive placement failures ("Reset the streak after reconciled fresh
   submissions"). Tripping early is not the safe direction here: unbought
   tickers are the harm this whole change exists to prevent. An unbroken run of
   failures is the systemic case the old blanket halt was reaching for — a
   broker outage, an expired session, an account block, exhausted buying
   power — and every further placement would be a doomed live call. Counted
   **consecutively**, not cumulatively: a submitted order resets the streak,
   so a batch that keeps succeeding between isolated bad symbols runs to
   completion. Setting it to 1 restores the old halt-on-first-rejection
   behavior.

**When the breaker trips**, every candidate it leaves unattempted gets an
explicit `skipped` row naming the breaker (`placement_breaker_halted`) — on the
afternoon path *and* in `MorningDCAExecutor._place_broker_orders` — and, on the
afternoon path, the trip itself counts once toward `errors_count` so
`manual_recovery_required` is set; otherwise a streak made entirely of
confirmation-required holds would leave no signal anywhere, since those
contribute nothing to `errors_count` on their own.

Recording the remainder is not merely cosmetic in the executor. Its
threshold-crossing credit-preservation pass skips a ticker precisely when
`outcomes.get(ticker)` is not a dict, and that pass is the **only** place such a
credit is durably written (`_compute_top_up` records only credits that stay
fully pending). A candidate the breaker skipped was therefore absent from
`outcomes`, its crossed credit never reached `preserve_pending`, and a later
slot's order came up short by that amount (Codex review, PR #786: "Preserve
credits for candidates skipped by the breaker").

The reporting contract matters as much as the placement one. `halted_on` means
the batch actually stopped; it is deliberately `None` for an isolated
rejection, so a caller reading it as "did anything go wrong?" would miss every
failure the batch survived. **Read `failures`.** Gate 9 must name every failed
ticker with its own reason whether or not the batch finished, and a batch that
did stop must additionally name the tickers never attempted — a completed
batch with isolated failures is still a trading failure.

The live executor prompts under `claude/dca-executor-prompts/` carry the same
rule in prose (Gate 7), and today that prose is what actually runs: per
`docs/live_wiring.md`, no prompt calls `execute_plan()` yet, so the prompt text
— not this module — is the live behavior. Both were changed together so they
cannot drift.

## 10. Implementation status (honest matrix)

| Area | Status | Where |
|---|---|---|
| Freshness gate / stale-pointer rejection | **Implemented (pre-existing)** | `scripts/same_day_pointer.py` |
| Versioned artifact validation + atomic pointer | **Implemented (pre-existing)** | `scripts/scheduled_artifacts.py`, `run_dca.py` |
| Effective-prior from artifacts + persisted fills | **Implemented (pre-existing)** | `scripts/run_classification.py` |
| Fail-closed broker reconciliation engine (paging, raw persist, id fallback) | **Implemented, tested — engine only** | `scripts/broker_reconciliation.py` |
| Effective-prior + fail-closed decision (`BROKER_HISTORY_INCOMPLETE`) | **Implemented, tested — engine only** | `scripts/broker_reconciliation.py` |
| Unified decision reason codes | **Implemented, tested — engine only** (corrected 2026-08-03) | `scripts/reason_codes.py` |
| Run state machine (`pending→…→published`) | **Implemented, tested — engine only** | `scripts/run_state_machine.py` |
| Chronological run ledger | **Implemented, tested — engine only** | `scripts/run_ledger.py` |
| Incident correlation IDs | **Partially WIRED — `build_correlation_id` is live; child/parent helpers engine only** (corrected 2026-08-03) | `scripts/correlation.py`, `run_dca.py` |
| Scheduler leases | **Implemented, tested — engine only** | `scripts/scheduler_lease.py` |
| Rich idempotency keys (date, window, symbol, strategy-ver, tier) | **Implemented, tested — engine only** | `scripts/idempotency.py` |
| Per-ticker error isolation in the afternoon candidate/signal loops | **Implemented, tested — live** (added 2026-08-24) | `near_close_dca.py` (§9a) |
| Rejection isolated to its own ticker + consecutive-failure breaker | **Implemented, tested** — live in the executor prompts; engine-only in `dca_runner` until the prompts call it (added 2026-08-24) | `dca_runner.py`, `claude/dca-executor-prompts/` (§9b) |
| **Wiring the above into the live runner decision path** | **Pending (behavior change; separate review)** | `run_dca.py`, `near_close_dca.py`, `dca_live_adapters.py` |
| Manifest single source of truth (generate task text from config) | **Pending** | — |
| Tighten run_state schema to require schema_version/window/strategy-ver/account | **Pending (backward-compat risk)** | `schemas/run_state.schema.json` |
| 5-way classification enum on disk | **Pending (backward-compat risk)** | `schemas/run_classification.schema.json` |
| Reliability dashboard / anomaly alerts (P2) | **Pending** | — |

**"Engine only"** means the module is implemented and unit-tested but is
intentionally NOT yet called from the live trading decision path. Wiring it in
changes live-trading behavior and must be done deliberately, with its own review
and a live-run acceptance checklist — not as a side effect of adding the engine.

> **Two rows corrected 2026-08-03 after a grep-verified wiring audit.**
> 1. **`reason_codes.py` was listed as plain "Implemented, tested"** with no
>    "engine only" qualifier, implying it was live. It is not: its only
>    non-test importers are `broker_reconciliation.py` and
>    `same_day_top_up_gate.py`, *both of which are themselves unwired*, making it
>    **transitively dead**. It is now tagged "engine only" like its siblings.
> 2. **`correlation.py` was listed as "engine only" but is partly live** — the
>    opposite error. `build_correlation_id` is a module-level import in
>    `run_dca.py` and is called in the live slot path, with the resulting
>    `correlation_id` written into `run_state.json`. The remaining helpers
>    (`child_correlation_id`, `new_correlation_id`, `parent_of`, `is_child` —
>    the recovery-attribution half) have no callers and stay engine-only.
>
> Independently verified as **genuinely unwired** (zero non-test callers):
> `broker_reconciliation.py`, `run_state_machine.py`, `run_ledger.py`,
> `scheduler_lease.py`, `idempotency.py`.
>
> **Note on `idempotency.py`:** the live executors do *not* use it. They each
> implement their own simpler `_idempotency_stream()` string-based ref_id
> scheme (`morning_dca.py`, `intraday_dca.py`, `near_close_dca.py`). Two
> non-integrated idempotency implementations therefore coexist, and the rich
> keyed scheme documented above is **not** what protects live runs against
> duplicate orders today. Worth consolidating before relying on either.
