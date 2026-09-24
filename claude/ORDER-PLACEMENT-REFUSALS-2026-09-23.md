# Seven-for-seven: every Claude slot refuses because the live-mirror repaste never happened

Written 2026-09-23, mid-day, from a same-day report of four consecutive
refusals (0935, 1030, 1130, 1230) plus a 10:30 prediction that all seven slots
would end the same way. Findings only; no code was changed in response — see
"What this is NOT" below.

## What was reported

12:30 refused: four for four on the day, and like the other three it stopped
before fetching anything, since no path led to an order. The 10:30 prediction
was holding exactly, with all seven slots expected to stop the same way.
Nothing placed by the Claude executor all day; whatever bought this morning
was the standing Perplexity backup. Distinct from earlier in the week: the
refusals are now clean and consistent rather than an erratic mix — each one
stops early, writes evidence, and alerts properly.

## Root cause: Gate 5 has nothing to call, because the fix was never deployed

This is not a bug in `near_close_dca.py` or `dca_runner.py`. Each of the seven
slots now has a committed, tested Gate 5 signal-generation producer, and the
corresponding prompt text in `claude/dca-executor-prompts/live-mirror/`
correctly names it. But the **live** Cowork scheduled-task body — the text
that actually fires each day, stored outside this repo at
`~/Claude/Scheduled/<taskId>/SKILL.md` — was never repasted from those
mirrors via `update_scheduled_task`. Every slot's live Gate 5 is still running
an older capture that has no working entry point, so every fire is
structurally incapable of producing a candidate. That is "no path led to an
order": the run clears the market-day/time/window/sole-executor gates, and
for the 12:30 slot fetches the week's order history (Gate 4), then stops at
Gate 5 with nothing to evaluate — before it ever reaches a market-data fetch.

Primary evidence, all already committed in this repo:

- `scripts/generate_midday_candidates.py:8-13` — written specifically to
  describe this failure mode: *"Cron `a6239dae` (the 12:30 PM ET midday live
  buy) had no committed signal-generation entry point, so its Gate 5 aborted
  every weekday with 'no signal-generation entry point exists for this slot;
  nothing to buy' — the run cleared every read-only gate, fetched the week's
  order history, and then stopped because there was no candidate producer to
  call."*
- `claude/dca-executor-prompts/live-mirror/1230ET-1230-pm-midday-buying-run.txt:34`
  — *"Until a human applies this body through `update_scheduled_task`, the
  live 12:30 task still aborts at Gate 5 every weekday... the actual live task
  remains the older `dca_runner.assess()`-based version captured 2026-07-31."*
- `claude/dca-executor-prompts/README.md:137-156` — states the same gap for
  **all seven slots**, dated 2026-08-26: every mirror names its slot's real
  producer (`generate_market_open_candidates.py`, `generate_morning_candidates.py`,
  `generate_intraday_candidates.py --slot {1130,1330,1430}`,
  `generate_midday_candidates.py`, `generate_buy_candidates.py`), but *"a
  producer existing is not the same as a slot trading: the live task's prompt
  must actually name the command, and that text lives outside this repo...
  treat every Gate 5 claim in these files as proposed, not active."*
- `scripts/check_live_prompt_drift.py` (last touched 2026-09-22) is the
  repo's own tool for catching exactly this drift — it diffs each live
  `SKILL.md` against its mirror and exits 1 on any difference — but it has to
  run on the machine holding the live task text, which this sandboxed
  checkout cannot reach, so it could not be run live to confirm today's exact
  drift count.

## Why it now reads as clean/stable instead of flaky

Several genuine gate bugs from earlier in the week — dead-shell mishandling,
a timezone misread at Gate 1, repo-probe misses at Gate 2 — are documented
and fixed in the `live-mirror/` copies (`README.md:57-115`). Those *are*
committed fixes, just, like the Gate 5 producer wiring, not yet deployed to
the live task text. What is left unfixed and undeployed is the one
deterministic condition: Gate 5 has nothing to call. That fires identically
on every slot, every day, which is why today's pattern looks stable rather
than erratic — the intermittent failure modes are gone, and only the
structural one remains.

## Confirmed: "whatever bought this morning" is the standing Perplexity backup

`config/scheduled_tasks.json`'s `standing_perplexity_backup_policy` (added
2026-09-08, made permanent 2026-09-11) runs an independent Perplexity task 20
minutes after each Claude slot (09:55 for 09:35, 10:50 for 10:30, ... 12:50
for 12:30) that checks for same-day Claude buy evidence and, finding none,
evaluates and places orders itself under its own gate logic. That is the only
executor placing real orders today, which matches "nothing placed by your
executor all day."

## What this is NOT

No code in this repo caused today's refusals, and none was changed to write
this note. The producer scripts exist and import cleanly
(`scripts/generate_midday_candidates.py` and siblings), the mirrors correctly
reference them, and `check_live_prompt_drift.py` already exists to catch this
exact drift class. The one open item is a human action outside version
control: pasting each `live-mirror/<slot>.txt` body into its live scheduled
task.

One correction worth flagging against today's report: "execution needs to
live somewhere else" overstates it. The fix isn't a redesign — it's already
written and committed, just not deployed. Applying it is `update_scheduled_task`,
once per slot, not a change to where execution lives.

## What needs a human

1. **Repaste all seven live scheduled tasks** from
   `claude/dca-executor-prompts/live-mirror/` via `update_scheduled_task`,
   confirming the account-number placeholder is expanded to the real value on
   the live side (never the reverse).
2. **Re-run `python3 -m scripts.check_live_prompt_drift`** after each repaste,
   on the machine that holds `~/Claude/Scheduled/`, to confirm `drifted_slots`
   reaches 0 rather than assuming the paste took.
3. **Watch the first post-repaste fire per slot closely.** Gate 5 producing a
   candidate list for the first time in weeks is exactly the moment a latent
   Gate 6/6.5/6.7/7 issue (budget snapshot, same-day top-up, placement) would
   first surface live instead of in tests.
4. Confirm whether the remaining three slots today (1330, 1430, 1530) hold
   the predicted pattern; a slot that behaves differently would mean either a
   repaste already happened for it, or this diagnosis is incomplete for that
   slot specifically.
