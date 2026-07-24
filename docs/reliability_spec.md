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

## 10. Implementation status (honest matrix)

| Area | Status | Where |
|---|---|---|
| Freshness gate / stale-pointer rejection | **Implemented (pre-existing)** | `scripts/same_day_pointer.py` |
| Versioned artifact validation + atomic pointer | **Implemented (pre-existing)** | `scripts/scheduled_artifacts.py`, `run_dca.py` |
| Effective-prior from artifacts + persisted fills | **Implemented (pre-existing)** | `scripts/run_classification.py` |
| Fail-closed broker reconciliation engine (paging, raw persist, id fallback) | **Implemented, tested — engine only** | `scripts/broker_reconciliation.py` |
| Effective-prior + fail-closed decision (`BROKER_HISTORY_INCOMPLETE`) | **Implemented, tested — engine only** | `scripts/broker_reconciliation.py` |
| Unified decision reason codes | **Implemented, tested** | `scripts/reason_codes.py` |
| Run state machine (`pending→…→published`) | **Implemented, tested — engine only** | `scripts/run_state_machine.py` |
| Chronological run ledger | **Implemented, tested — engine only** | `scripts/run_ledger.py` |
| Incident correlation IDs | **Implemented, tested — engine only** | `scripts/correlation.py` |
| Scheduler leases | **Implemented, tested — engine only** | `scripts/scheduler_lease.py` |
| Rich idempotency keys (date, window, symbol, strategy-ver, tier) | **Implemented, tested — engine only** | `scripts/idempotency.py` |
| **Wiring the above into the live runner decision path** | **Pending (behavior change; separate review)** | `run_dca.py`, `near_close_dca.py`, `dca_live_adapters.py` |
| Manifest single source of truth (generate task text from config) | **Pending** | — |
| Tighten run_state schema to require schema_version/window/strategy-ver/account | **Pending (backward-compat risk)** | `schemas/run_state.schema.json` |
| 5-way classification enum on disk | **Pending (backward-compat risk)** | `schemas/run_classification.schema.json` |
| Reliability dashboard / anomaly alerts (P2) | **Pending** | — |

**"Engine only"** means the module is implemented and unit-tested but is
intentionally NOT yet called from the live trading decision path. Wiring it in
changes live-trading behavior and must be done deliberately, with its own review
and a live-run acceptance checklist — not as a side effect of adding the engine.
