# Seven DCA executor tasks — corrected setup card

Create each one in the Claude desktop app: new Cowork task -> "Run this task"
picker, top right -> **On your computer** -> schedule it with the cron below
-> paste the matching `.txt` file as the prompt. Each `.txt` holds nothing but
prompt text. Select all, copy, paste. No trimming.

| # | Slot ET | Cron (UTC) | Prompt file |
| --- | --- | --- | --- |
| 1 | 9:35 AM market-open | `35 13 * * 1-5` | `1-executor-0935-cron_35_13.txt` |
| 2 | 10:30 AM morning | `30 14 * * 1-5` | `2-executor-1030-cron_30_14.txt` |
| 3 | 11:30 AM intraday | `30 15 * * 1-5` | `3-executor-1130-cron_30_15.txt` |
| 4 | 12:30 PM midday | `30 16 * * 1-5` | `4-executor-1230-cron_30_16.txt` |
| 5 | 1:30 PM intraday | `30 17 * * 1-5` | `5-executor-1330-cron_30_17.txt` |
| 6 | 2:30 PM intraday | `30 18 * * 1-5` | `6-executor-1430-cron_30_18.txt` |
| 7 | 3:30 PM final | `30 19 * * 1-5` | `7-executor-1530-cron_30_19.txt` |

**Do NOT retire the old schedule yet.** `config/scheduled_tasks.json` still lists five
live-execution tasks from the prior three(ish)-stream scheduler (`2ba19526` 9:35
market-open, `42173154` 10:30 morning, `7be82b97` 11:30/1:30/2:30 intraday, `a6239dae`
12:30 midday, `10ce362c` 3:30 final) -- these are the account's ONLY currently-functioning
scheduled DCA execution. Every one of the seven new prompt files below ends every fire at
Gate 5 with the `GATE5_DISABLED` verdict (no callable signal-generation entry point exists
yet; see `PRODUCTION_READINESS.md`), so none of them can place a single order today. As of
2026-07-30 this verdict alerts on every fire (see each file's Gate 5/Gate 9 and
`PRODUCTION_READINESS.md`) -- it no longer places nothing silently, only
places nothing. Retiring the five old tasks before Gate 5 is resolved leaves the account
with zero functioning scheduled execution for however long that takes (Codex review:
"Keep the old executors until replacements can pass Gate 5") -- create and schedule all
seven new tasks below ALONGSIDE the five old ones, not instead of them.

It is safe to run both schedules at once for now precisely because the new prompts
cannot place orders: each fires, checks Gate 4, and then ends at Gate 5 without ever
reaching placement. Gate 4 is not harmless noise, though -- every one of the seven new
slots shares its exact window with a real Perplexity-era task (9:35, 10:30, 11:30,
12:30, 1:30, 2:30, and 3:30 are the OLD roster's own five tasks' windows too), so Gate 4
is *expected* to see that task's real same-window order on every fire once it places
one. Rather than treating that as evidence of a rogue second executor and alerting (as
Gate 4 does everywhere else), the 10:30, 12:30, and 3:30 prompts inspect the found
order's ref_id against their own slot's known legacy prefix -- `morning-dca-` (`42173154`,
`morning_dca.py`), `midday-dca-` (`a6239dae`, `midday_dca.py`), and `graded-dca-`
(`10ce362c`, `near_close_dca.py`) respectively -- distinct from this roster's own
`dca-runner-<slot_id>-<trading_day>-<ticker>` (per `dca_runner.py`, once Gate 5 is
wired). Only when EVERY order in the window carries that slot's specific legacy prefix does the prompt
classify it `GATE4_EXPECTED_OVERLAP` -- which, like `GATE5_DISABLED` below, alerts on every
fire (2026-07-30). An order with no recognizable ref_id, or one carrying this
roster's OWN prefix, stays on the real abort-and-alert path: that's either an untrusted
collision or, worse, a genuine duplicate execution of this roster, and blanket-silencing
"any order in the window" (or trusting a single, roster-wide prefix that doesn't match
every slot's real legacy task) would have hidden exactly that (Codex review: "Recognize
the real legacy stream ref-id prefixes"; "Distinguish legacy orders before suppressing
Gate 4 alerts"; "Avoid expected Gate 4 alerts during roster overlap"). The 9:35, 11:30,
1:30, and 2:30 prompts have NO such exception at all: their real legacy counterparts
(`2ba19526` and `7be82b97`) carry `no_python_entry_point=true` in
`config/scheduled_tasks.json` -- an external scheduler/agent runs them directly, with no
committed, documented ref_id convention to verify against -- so those four prompts
cannot safely attribute a same-window order to them and their Gate 4 alerts on every
collision exactly as it always has, with no silent classification attempted. Where
applicable, `GATE4_EXPECTED_OVERLAP` alerts on every fire it triggers, on the same three
channels as any abort -- it is a known, expected outcome on every fire of the three prompts
that can reach it, not a failure to escalate, but known-and-expected no longer means silent
(Codex review: "Continue past expected overlap to report blocked Gate 5"). `GATE5_DISABLED`
used to be silent for the same reason (scheduling all seven alongside the old roster without
that exception meant up to seven expected abort alerts a day from Gate 5 alone -- 35/week,
burying any real safety failure under routine noise; Codex review, PR #232/#238: "Do not
schedule tasks that are guaranteed to alert"). **That was reversed 2026-07-30**: four of
seven slots fired silently the same day and a blocked entry point read as indistinguishable
from an evaluated, empty universe -- a silent executor is worse than an aborting one.
`GATE5_DISABLED` now alerts on every fire, on the same channels as any abort, in all seven
prompt files below, and `GATE4_EXPECTED_OVERLAP` was brought in line with it the same day for
the same reason, in the three prompts where it applies; **repaste all seven prompts from this
repo into their live Cowork tasks to pick up both changes** -- editing the committed `.txt`
files alone does not change what a task already pasted into Cowork does (see
`PRODUCTION_READINESS.md`). Accept the resulting alert volume (up to seven
alerts a day, every weekday, until a real entry point exists) as the cost of that trade-off.
The Gate 4 sole-executor-interlock
double-order risk this section used to warn about only becomes real once a human
wires a real signal-generation entry point into these prompts, and Gate 5 in all seven
must revert from its disabled `GATE5_DISABLED` classification to a real abort-and-alert
check on failure, the same as every other gate. `PRODUCTION_READINESS.md` is a point-in-
time (2026-07-26) repository-readiness snapshot, not an implementation plan -- it does
not carry the activation requirements, safety analysis, or producer decisions an operator
would need to actually do this wiring. That material used to live in `GATE5-PROPOSAL.md`
(removed 2026-08-11); the current source for it is `claude/SEVEN-SLOT-TRANSITION-PROPOSAL.md`
§4 ("Five-task → seven-slot transition sequencing"), which also documents this whole file's
own successor prompt generation (Codex review: "Point Gate 5 operators to the actual
implementation plan").

**Correction, 2026-08-02:** this section (and this whole file, which documents
the SUPERSEDED prompt generation — see `claude/dca-executor-prompts/README.md`)
used to say that wiring Gate 5 is also "the point to retire the five old
[Perplexity] tasks." Geoff has since directed a standing dual-executor design
instead: Perplexity and Claude coordinate first-come-first-serve per slot and
keep running alongside each other indefinitely, not a one-time cutover the
moment Gate 5 ships. See `claude/PERPLEXITY-CLAUDE-COORDINATION.md` and
`docs/safeguards.md`'s "Live execution roster" section for the current design.

**Truncation risk:** `config/scheduled_tasks.json`'s `live_task_text_compactness_policy`
documents that the scheduling service can silently truncate long task text --
the documented working precedent for a live task is ~5,850 characters. These
seven prompts are far longer than that (roughly 16,000-19,000 characters
each), and before this fix each prompt's own `GATE5_DISABLED` instruction did
not begin until roughly character 7,900-9,300, with Gate 9's evidence-writing
rules starting after character 12,700 -- both well past a boundary that has
already truncated task text once before. A truncated fire could therefore
lose the very instruction that keeps it from placing a real order, or the
evidence-writing requirement, or both. Each prompt now opens with a short,
self-contained "STANDING GATE 5 OVERRIDE" paragraph -- before anything else,
including the `LIVE EXECUTOR` line -- stating unconditionally that no real
order may be placed today and, if the fuller gate sequence below is missing
or incomplete, to treat that as this task's own `GATE5_DISABLED` verdict and
write whatever evidence is possible (Codex review: "Move the disabled gate
ahead of the truncation boundary"). Since 2026-07-30 that same opening
paragraph also inlines the three Gate 9 alert channels in compact form
(in-app message center, repo alert file path, email draft) rather than only
referring to Gate 9 by name -- referring to a section that may itself be
past the truncation boundary would not actually tell a truncated fire how or
where to alert (Codex review: "Put the alert-channel instructions before
truncation"). This closes the immediate safety risk (an accidental live
placement surviving truncation, or a truncated fire that goes to place
nothing but also alert nothing) regardless of prompt length. It does NOT
address the underlying length problem: shrinking each
prompt down toward the ~5,850-character precedent -- replacing inlined
long-form explanations with compact references to `docs/safeguards.md`,
`PRODUCTION_READINESS.md`, and the other repo sources already cited
throughout -- is a larger rewrite left as a deliberate follow-up, not
attempted here, since a rushed compaction pass risks introducing new errors
into carefully-reviewed safety text. Until that follow-up lands, paste each
prompt in full as instructed above; do not trim it yourself.

**If you only create one tonight, create #1.**

**2026-07-29 alert review:** an ABORT alert (Slack + repo file + Gmail) fired
from what looks like the 10:30 slot the same day. A same-day review draft
concluded this contradicted this card and `2-executor-1030-cron_30_14.txt`,
but that draft compared the alert against prompt text first committed by the
PR #232 merge over ten hours *after* the alert fired, so it couldn't actually
confirm what was deployed at 10:30 AM. The fuller incident writeup
(`GATE5-ALERT-INCIDENT-2026-07-29.md`, revised twice on 2026-07-30 to reach
this weaker, hedged conclusion) was removed 2026-08-11 -- `PRODUCTION_READINESS.md`
is an unrelated, unchanged 2026-07-26 readiness snapshot that predates the
alert and never contained this chronology, so it is not a substitute source
(Codex review: "Retain a real link to the alert review"). This paragraph is
now the retained summary of that document's conclusion: no prompt or config
change resulted either way, and the alert-file-durability/legacy-`42173154`-task
findings from §4-§6 of the removed document remain informally noted but are
no longer independently retrievable from this repo.

**Crons are UTC and assume EDT.** On 2026-11-01 every one fires an hour early
in ET; each needs +1 hour then, and the live-test window in
`config/trading_window.json` already ends 2026-10-30 specifically to avoid
that DST edge before it's handled.

## What changed from the version you pasted earlier

- **Budget caps:** these use `config/budgets.json`'s existing tiered daily/weekly
  caps (Normal $180/$900 up to Stress $360/$1,800, selected per day by planned
  spend and signal count), not a flat $200/day or $1,200/week. They're shared
  across all seven slots, same effective "earlier slots have priority" behavior
  you described, just with the real numbers.
- **Ladder:** $2.00/$1.00 base times a 1.0x-5.0x multiplier from
  `config/sizing_ladder.json`, topping out at exactly $10/order — not the
  $1.50-based ladder that ran past its own stated $10 cap.
- **Notifications:** in-app message center + email to you, not Slack.
  `config/notifications.json` has Slack explicitly disabled for routine
  notifications; the only Slack paths in this repo are env-gated, off-by-default
  drift/failure webhooks.
- **Gate 3** no longer references the old "2026-08-28 vs 2026-10-30 unresolved
  dispute" framing — `config/trading_window.json` already resolved that,
  dated and reasoned, on 2026-07-26.
- Each prompt points at `docs/safeguards.md#live-execution-roster`, the section
  I added to the repo (branch `claude/dca-seven-slot-roster-docs`, commit
  `21b272d`, not yet pushed) documenting this seven-slot roster as the
  authoritative reference, since `docs/schedule_manifest.md` and
  `config/scheduled_tasks.json` still describe the old three-stream Perplexity
  model and haven't been reconciled yet.

## Before the first run

Confirm the Mac won't be asleep at 9:35 AM ET — `caffeinate` or a launchd
keep-awake job is worth setting up now that Perplexity's cloud scheduler isn't
a fallback.
