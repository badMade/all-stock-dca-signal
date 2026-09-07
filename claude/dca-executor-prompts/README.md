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
* Live placement is authorized for all seven slots as of 2026-09-02, per
  Geoff's explicit direction that order placement be on for all scheduled
  buy runs. The 1230 slot was the one exception up to that point, and its
  `live-mirror/` copy is currently AHEAD of what was actually captured
  2026-07-31: PR #412 rewrote it so Gates 0/1/3/4/6 call `dca_runner.py`'s
  individual gate functions directly -- `market_day_status`,
  `gate_time_sanity`, `gate_trading_window`, `gate_sole_executor`,
  `budget_snapshot`, `plan_orders` -- rather than `dca_runner.assess()`,
  since `assess()` bakes in dynamic budget-tier escalation that
  `config/midday_dca.json` forbids for this slot. That PR's own placement
  (Gate 7) text stayed explicitly OFF pending validation of this slot's
  Gate 5 signal-generation producer; this repo's copy has since been
  updated to bring Gate 7 up to the same authorized standard as the other
  six slots, superseding that OFF text. Per this file's own policy above,
  editing the committed copy changes nothing about the live task: until a
  human applies this text via `update_scheduled_task` and the mirror is
  re-captured, the ACTUAL live 1230 task is still whatever was captured
  2026-07-31 (the `assess()`-based, placement-OFF version this bullet used
  to describe), not what `live-mirror/1230ET-1230-pm-midday-buying-run.txt`
  currently contains. Diff against a fresh capture before trusting this
  bullet as "what actually runs."
* Gate 5 tells the executor to run the repo's signal generation and to abort if
  it errors. As of 2026-08-26 all seven slots have their own committed,
  tested producer, and every live-mirror prompt in this directory already
  names its command: `0935ET` -> `scripts/generate_market_open_candidates.py`,
  `1030ET` -> `scripts/generate_morning_candidates.py`, `1130`/`1330`/`1430ET`
  -> `scripts/generate_intraday_candidates.py --slot {1130,1330,1430}`,
  `1230ET` -> `scripts/generate_midday_candidates.py`, `1530ET` ->
  `scripts/generate_buy_candidates.py` (see `../../PRODUCTION_READINESS.md`).
  Each producer is scoped to its own slot on purpose (the streams have
  different executors, decision anchors, and ladders), so pointing one slot's
  prompt at another slot's producer would silently score it against the wrong
  logic. None of these Gate 5 producers places an order or touches a broker --
  Gates 6/6.5/6.7/7 remain prompt-driven, so Gate 5 no longer aborts every
  day by default for any slot, but that says nothing about whether Gate 7
  is authorized to place what Gate 5 finds.
* A producer existing is not the same as a slot trading: the live task's prompt
  must actually name the command, and that text lives outside this repo. Until a
  human applies a rewritten body via `update_scheduled_task` and the mirror is
  re-captured, the live task still runs whatever was captured -- so treat every
  Gate 5 claim in these files as proposed, not active.
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
