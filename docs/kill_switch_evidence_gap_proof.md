# Kill-switch evidence gap proof (12:30 Gate 5) — design note

Status: **proposed, 2026-11-10, awaiting operator approval.** Nothing here is wired yet.
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
   **no order of any side or state** has `created_at` inside `[D 14:00 ET, D+1 00:00 ET)`.
   - The window is wider than both acceptance windows on purpose: it covers a late in-window fire,
     the 19:24-style off-window fire if it had got past Gate 1, and after-hours queued orders.
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
`KILL_SWITCH_GAP_EXCUSED <D> <S> gate1/GATE1_OFF_WINDOW broker_orders_in_window=0` so the run's
evidence and alert text record it. It is never written into any artifact that sizes or caps.

## Implementation shape (if approved)

- New stdlib module `scripts/kill_switch_gap_proof.py` with one pure function,
  `excuse_missing_pairs(missing_pairs, *, claimed_pairs, marker_root, orders_doc) -> (excused, reasons)`,
  using `scripts.abort_markers.read_abort_markers` plus a strict line-level reader (so a malformed
  line blocks instead of being skipped), and a `__main__` that is not used by the prompt.
- The 12:30 prompt's Gate 5 block: after computing `missing_pairs`, if every missing pair has a
  clean Gate-1 marker and no claiming artifact, print `KILL_SWITCH_GAP_NEEDS_BROKER_PROOF <earliest D>`
  and exit 3. The agent then fetches orders from that day, saves
  `$ROOT/kill_switch_gap_orders.json` as `{"created_at_gte": "<iso+offset>", "pagination_complete": true, "orders": [...]}`
  and reruns the same block with `KILL_SWITCH_GAP_ORDERS=$ROOT/kill_switch_gap_orders.json`. Any
  other exit stays the existing abort. No extra broker call on an ordinary day.
- `scripts/abort_markers.py` docstring updated: still never run evidence; the one reader outside
  triage is this gap proof, which uses it only as a precondition for a broker check.
- Tests (`scripts/tests/test_kill_switch_gap_proof.py`): Gate-1 marker + empty broker window ⇒
  excused as $0; later-gate (`gate2`/`gate7`) marker ⇒ rejected; mixed file (gate1 + later gate) ⇒
  rejected; malformed line ⇒ rejected; no marker ⇒ blocks; marker + any order in window (buy, sell,
  cancelled) ⇒ rejected; boundary after `D 00:00 ET` ⇒ rejected; `pagination_complete` false/missing
  ⇒ rejected; claiming-but-rejected `run_state.json` ⇒ rejected; prompt-block test that the
  printed scalar is unchanged when nothing is excused.
- Scope: the 12:30 prompt only — it is the only live-mirror prompt carrying this pair check
  (grep for `incomplete trailing-7-day`, 2026-11-10).

## Rollout

1. Draft PR, not merged; caps/kill-switch logic needs its own review and acceptance run.
2. After merge, the **operator must repaste** the 12:30 prompt into the Claude scheduled task;
   `scripts/check_live_prompt_drift.py` must pass on the mirror.
3. Acceptance: a dry run against a fixture tree reproducing 11-09 (two `gate1` markers, no
   afternoon artifacts, empty broker window) prints the same figure as a tree with real $0
   artifacts; adding one order at 14:31 ET on that day makes it abort.
