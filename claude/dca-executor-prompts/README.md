# Executor prompt copies in this directory — which generation is which

There are **two generations** of executor prompt text committed here, and they
disagree with each other. This file says which one is live, so a reader does not
patch the wrong one.

## `live-mirror/` — CANONICAL. Mirrors what actually runs.

Seven read-only copies of the live scheduled tasks, captured 2026-07-31 after
the same-day top-up guard fix. Source of truth is
`~/Claude/Scheduled/<taskId>/SKILL.md`; these are diffable copies, nothing more.

**Editing a file in `live-mirror/` changes nothing.** Live prompt edits go
through `update_scheduled_task`'s full-body replacement, then the mirror is
re-captured.

**These mirrors are not byte-identical to the live prompts.** The live prompts
carry the Robinhood account number as a literal; every occurrence is replaced
with `${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}` before committing, because this repo
is public. That substitution is the only permitted divergence — diff a mirror
against its live source with the placeholder expanded, or the comparison will
report false differences.

Characteristics of this generation:

* The account is addressed directly rather than through the environment: the
  live text hard-codes it at each call site (here shown redacted), instead of
  reading it from `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` at run time.
* Live placement is ON for six slots (the 1230 slot is the exception; its
  prompt drives `dca_runner.assess()` and its placement is explicitly OFF).
* Gate 5 tells the executor to run the repo's signal generation and to abort if
  it errors — which it does, every day, because no such entry point exists for
  the seven-slot roster (see `../GATE5-PROPOSAL.md`).
* `GATE 6.5 - SAME-DAY TOP-UP GUARD` (step 4.5 in the 1230 slot) invokes
  `scripts/same_day_top_up_gate.py`.

## `1-executor-*.txt` … `7-executor-*.txt` — SUPERSEDED. Do not patch these.

An earlier generation, last touched 2026-07-29, kept for history. It is **not**
what runs today and has NOT been updated with the same-day top-up guard,
deliberately: retrofitting a placement-time guard onto prompts whose standing
instruction is "must NEVER place a real equity order today" would be incoherent.

How to recognize it:

* A genuine `${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}` env-var reference — this
  generation never carried the literal, so unlike `live-mirror/` its committed
  text matches what ran. The placeholder alone no longer tells the two apart;
  use the markers below.
* A `STANDING GATE 5 OVERRIDE` block at the very top, ahead of the truncation
  boundary.
* Gate 5 ends in verdict `GATE5_DISABLED` — silent, no alert, no order, every
  fire.
* Much longer (18-20 KB vs ~12 KB), carrying per-review annotations and the
  `run_state.json` evidence contract that the live generation dropped.

The two generations differ in more than the top-up guard: Gate 2's repo probe,
Gate 3's date handling, Gate 4's ref-id overlap classification, and the whole
Gate 9 evidence-writing contract all diverge. **Reconciling them is an open
decision, not a merge.** The superseded generation contains safety machinery the
live one lacks (notably the run_state evidence contract and the ambiguous-mount
abort); the live one is the only one that actually runs.

## `0-setup-card.md`

Belongs to the superseded generation. Its warning about the overlapping old/new
task window during cutover is, as of 2026-07-31, still live and still unresolved
— see `../SECOND-EXECUTOR-FINDING.md`.
