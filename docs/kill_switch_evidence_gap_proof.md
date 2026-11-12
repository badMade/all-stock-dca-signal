# Kill-switch evidence gap proof (12:30 Gate 5) — design note

Status: **approved by the operator 2026-11-10; implemented in a draft PR, not merged.** Needs its own
review and acceptance run, then a repaste of the 12:30 prompt.
DCA Issues key: `kill_switch_evidence_incomplete`.

## Problem

The 12:30 midday prompt's Gate 5 computes `--deployed-last-7d-usd` from afternoon `run_state.json`
artifacts and requires one validated artifact for every `(full market day, 14:30|15:30)` pair in the
trailing `dca_risk.KILL_SWITCH_WINDOW_DAYS`. A missing pair raises
`incomplete trailing-7-day afternoon evidence` and the slot aborts.

On 2026-11-09 the Claude app was down; every slot fired at 19:24 ET, aborted `GATE1_OFF_WINDOW`
and wrote only an evidence-free abort marker (`cron_tracking/abort_markers/20261109/<slot>.jsonl`).
No `run_state.json` exists for 11-09 14:30 or 15:30, so 12:30 aborts every day until 11-09 leaves
the window (11-10 through 11-13). One outage costs four 12:30 slots.

## Why the marker alone is not enough

The pair check exists so the figure cannot be **understated**. To excuse a pair we must prove the
afternoon-scheduled deployment for it was **$0**. A Gate-1 marker proves less than that:

1. **It covers only the fire that wrote it.** Gate 1 runs before Gate 4's broker read and long before
   Gate 7 placement, so the run that wrote a `gate1` marker placed nothing. But a marker says nothing
   about a *different* fire for the same pair — e.g. an earlier in-window fire (or a manual "Run now"
   later reclassified) that placed orders and then crashed before `finalize_run_evidence` wrote
   `run_state.json`. That case also has "no run_state.json", and adding a marker from a second,
   off-window fire would wrongly excuse it.
2. **It is self-reported.** The prompt writes it through a shell script; nothing ties it to the
   brokerage. It is good evidence of *why* a gap exists, not of *what the account did*.

So the marker stays what `scripts/abort_markers.py` says it is: never run evidence, never a dollar
figure. It becomes a **precondition** for asking the question, and the broker answers it.

## Proposed proof: marker + broker absence, both required

A missing pair `(D, S)` is excused as **$0** only when **all** of these hold; otherwise it stays
missing and the slot aborts exactly as today:

1. **No `run_state.json` claims the pair at all** — not even a rejected one. A rejected/mismatched
   artifact is a different defect (`rejected_reasons` today) and keeps blocking.
2. **A marker file exists for `(D, S)`** and **every** line in it is a well-formed
   `evidence_free_abort` marker (`read_abort_markers` rules) whose `gate` is `gate1` and
   `reason_code` is `GATE1_OFF_WINDOW`. Any other gate (`gate2`, a later gate, a typo), any other
   reason, or a malformed line in that file ⇒ not excused (ambiguous ⇒ block). An empty or missing
   file ⇒ not excused.
3. **Broker order history proves no order could belong to that afternoon.** The prompt fetches
   `get_equity_orders` with `created_at_gte` at or before `D 00:00 ET`, following pagination to
   completion (same discipline as Gate 4; the 2026-10-22 completeness carve-out does **not** apply
   here, because this is a proof of absence — unproven completeness ⇒ not excused). In that file,
   **no order of any side or state** has `created_at` inside `[D 14:00 ET, next market day 09:30 ET)`.
   - The window is wider than both acceptance windows on purpose: it covers a late in-window fire,
     the 19:24-style off-window fire if it had got past Gate 1, after-hours queued orders, and an
     order stamped just after midnight and queued for the next session (review 2026-11-11: the first
     draft ended at D+1 00:00 and let that through). It ends at the next market day's 09:30 ET open,
     from Gate 0's own calendar, so a Friday's window runs to Monday's open; that session's own
     orders belong to its own slots and do not block.
   - It ignores `placed_agent` and side on purpose, same reasoning as `gate_sole_executor`: we
     cannot attribute rows to "afternoon-scheduled", so any row at all is ambiguous ⇒ block. A
     manual buy that afternoon therefore keeps 12:30 blocked; that is the safe direction.
   - A row with a missing or offset-less `created_at` ⇒ block.
   - The file must record its own `created_at_gte` boundary; a boundary later than `D 00:00 ET`
     ⇒ not excused.

Condition 3 is the actual $0 proof; it would suffice on its own logically. Condition 2 is kept so
that an *unexplained* gap (no marker: the slot may never have fired, or crashed mid-run) still
blocks and still reaches the operator, and so that this path stays narrow: only "the scheduler
fired, Gate 1 refused it, and the account shows nothing" is excused.

Today's afternoon is never in the window (unchanged), so the only orders the proof reads are
historical and settled in time.

## What the excused pair contributes

Exactly `$0` to `seven_day_deployed`, and it counts as coverage. The command still prints one scalar;
each excused pair is also printed to **stderr** as
`KILL_SWITCH_GAP_EXCUSED <D> <S> gate1/GATE1_OFF_WINDOW … broker_orders_in_window=0; …` so the run's
evidence and alert text record it. It is never written into any artifact that sizes or caps.

## Implementation

- `scripts/kill_switch_gap_proof.py` (stdlib, reads only): `classify_missing_pairs` returns one
  verdict per missing pair — `excused`, `needs_broker_proof` or `blocked` — and only `excused`
  counts as coverage.
- **Marker check is stricter than the triage reader.** `scripts.abort_markers.read_abort_markers`
  skips bad lines so the watchdog degrades toward `slot_did_not_fire`; here a skipped line could hide
  a later-gate marker, so any malformed, foreign, duplicate-key or non-`gate1/GATE1_OFF_WINDOW` line
  blocks the pair. `gate2/GATE2_ABORT_NO_PYTHON311` is pre-broker too but is left out on purpose.
- **Broker proof is a `scripts.assemble_order_pages` envelope, re-replayed.** The prompt builds it with
  the existing assembler (`--trading-day <D>`, `created_at_gte` = D 00:00 ET). The module replays the
  pages again through `broker_reconciliation.same_day_order_snapshot` + `_PageReplayer` (the same
  chain `same_day_top_up_gate` trusts), so a hand-built or truncated file is refused rather than
  trusted for having the right keys. It must be pinned to `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` and its
  `trading_day` must be on or before D.
- **Provenance (review 2026-11-11).** The replay proves the chain's shape, not its origin: a
  hand-built `{"pages": [{"orders": []}]}` replays as complete. So the envelope's `ingest` block must
  be there and still match: `tool` is `scripts.assemble_order_pages`, `payload_sha256` recomputes
  from the canonical pages, `order_count` equals the replayed count, and `pages_sha256` has one
  sha256 per page (it hashes the raw page bytes the assembler read, which the envelope no longer
  holds, so only its shape can be checked). This catches a hand-built file and any edit after
  assembly (a row removed, a timestamp moved). It does **not** stop a forger who recomputes the
  hashes; that trust stays with the session, as below.
- **Fetch boundary (review 2026-11-11).** `trading_day` is self-reported, and the request's own
  `created_at_gte` is not visible in the pages, so a fetch actually started on D+1 would read as an
  empty D afternoon. The block therefore passes every broker order id the validated afternoon
  run_states recorded (`buy_candidates[].order_id`) on a day after D and before today, and each one
  must be in the envelope; a missing one blocks with `orders file does not contain known order <id>;
  fetch boundary unproven`. When there is no later known order to check, the proof still passes but
  says so in its reason (`no known later order to cross-check the fetch boundary`). In that case the
  residual trust is the same Gate 4 and Gate 6.5 already place in the session's fetch parameters.
- 12:30 prompt, Gate 5 block: after `missing_pairs`, any `blocked` verdict raises the existing
  `incomplete trailing-7-day afternoon evidence` SystemExit (exit 1), naming each reason. If the only
  gaps need broker proof, it prints `KILL_SWITCH_GAP_NEEDS_BROKER_PROOF <earliest D>` and exits 3; the
  agent fetches, assembles `$ROOT/kill_switch_gap_orders.json`, and reruns with
  `KILL_SWITCH_GAP_ORDERS` set. Exit 0 prints the same single figure plus one
  `KILL_SWITCH_GAP_EXCUSED` stderr line per excused pair. No extra broker call on an ordinary day.
- Tests: `scripts/tests/test_kill_switch_gap_proof.py`, including four that execute the prompt's
  real block (extracted from the mirror) against a recreated 2026-11-10 cron tree with a frozen clock.

## Rollout

1. Draft PR, not merged; caps/kill-switch logic needs its own review and acceptance run.
2. After merge, the **operator must repaste** the 12:30 prompt into the Claude scheduled task;
   `scripts/check_live_prompt_drift.py` must pass on the mirror.
3. Acceptance: a dry run against a fixture tree reproducing 11-09 (two `gate1` markers, no
   afternoon artifacts, empty broker window) prints the same figure as a tree with real $0
   artifacts; adding one order at 14:31 ET on that day makes it abort.
