# Gate 5 / watchdog fixes — 2026-07-30

Status: **Two of three flagged items fixed in this repo (prompt text + docs),
revised once after a same-day PR review round caught real issues in the
first draft — see "Revision history" at the bottom. The third item is a
governance question, explicitly left unresolved here per operator
instruction — see §3.**

## 0. Context

**Dating caveat (added on review):** this document's own commits are
timestamped 2026-07-30 03:57-04:15 UTC — 2026-07-29 23:57 ET to 2026-07-30
00:15 ET — before the 9:30 AM ET market open, let alone before any of the
12:30-3:30 PM ET fires described below could have happened that same
calendar day. The four-silent-slots and down-tape observations below were
relayed as already-completed, same-day facts in the request this document
responds to; this repo/session has no independent way to confirm the
session's own clock against the calendar date those events actually
occurred on, the same limitation
`claude/GATE5-ALERT-INCIDENT-2026-07-29.md` §2 describes for confirming
deployed-vs-committed prompt text. If the described trading day was actually
2026-07-29, every "2026-07-30" reference below (including this file's name)
describes that day's events under the wrong date; this revision does not
resolve which is correct, only flags that the two are inconsistent as
written. The same "2026-07-30" dating is repeated in the rationale text
added to all seven executor prompts and to `0-setup-card.md`
(`claude/dca-executor-prompts/*`) for the same reversal decision; those are
not separately re-dated here, and carry the same caveat.

2026-07-30's tape was broadly down: roughly 36 of 45 active-universe tickers
closed below prior close, 16 of them at a 3x-or-greater ladder multiplier — a
heavy accumulation day by this strategy's own signal definition
(`config/sizing_ladder.json`, `high_multiplier_threshold` in
`config/budgets.json`). None of it was captured: Gate 5 has no callable
signal-generation entry point yet (`claude/GATE5-PROPOSAL.md`), so every one
of the seven Claude roster slots ended the day at `GATE5_DISABLED` and placed
nothing. Landing that entry point is the real fix; everything below is about
making the *current*, still-disabled state legible instead of landing that
entry point.

## 1. Four slots fired silently — Gate 5 wording tightened

12:30, 1:30, 2:30, and 3:30 all fired per their `lastRunAt` and posted
nothing. 12:30 was a known gap (no Slack step in that slot's task text at the
time). The other three had a Slack step and still posted nothing, because the
committed Gate 5 text collapsed two different states into one silent
behavior:

- **Blocked entry point** (today's actual, permanent state — no committed,
  callable signal-generation entry point exists for the seven-slot roster;
  see `claude/GATE5-PROPOSAL.md`): previously `GATE5_DISABLED`, silent, no
  alert on any channel.
- **Zero candidates after a real evaluation** (only possible once an entry
  point exists — not reachable today): also silent, by design, and correctly
  so.

Gate 5's prior text ended both cases the same way — "known, expected gap ...
do NOT alert" — which reads as "zero candidates, end silently" even though
the actual condition every fire hits is "blocked before evaluation," the
same distinction the 2026-07-29 10:30 alert drew explicitly (see
`claude/GATE5-ALERT-INCIDENT-2026-07-29.md` §3). A silent executor is worse
than an aborting one: a human watching `lastRunAt` alone cannot tell a
correctly-silent empty day from a slot that never got a chance to evaluate
anything.

**Fix applied:** all seven executor prompts
(`claude/dca-executor-prompts/1-executor-*.txt` through `7-executor-*.txt`)
now alert on every `GATE5_DISABLED` fire, on all three Gate 9 channels (in-app
message center, repo alert file, email draft) — no fourth, Slack channel:
none of the seven committed prompts has a Slack step, `config/notifications.json`
disables Slack for routine notifications, and `0-setup-card.md` is explicit
that these prompts use in-app messaging and email, not Slack. (Any Slack step
observed on the four silent slots, per §1's premise, exists only in
whatever text a human has separately pasted into the live Cowork tasks,
outside this repo's own channel list — this fix does not add, remove, or
depend on that.) The verdict
name (`GATE5_DISABLED`) is unchanged, so Gate 4's `GATE4_EXPECTED_OVERLAP`
cross-reference and the `run_state.json` schema contract are unaffected —
only the notification behavior changed. "Zero candidates" is now explicitly
labeled in Gate 9 as the future, evaluated-and-empty case, distinct from
`GATE5_DISABLED`, and stays silent once it becomes reachable.
`docs/safeguards.md`, `docs/schedule_manifest.md`, and
`claude/dca-executor-prompts/0-setup-card.md` are updated to match (all
three previously described the verdict as silent/non-alerting).
`config/scheduled_tasks.json`'s `connector_native_execution_policy` and its
intraday copy also asserted, in prose, that a missing-entry-point Gate 5
fire is "the silent, non-alerting `GATE5_DISABLED` verdict" — a
machine-readable policy record an executor or task-sync process could
consult independently of the prompts or docs, and it would have kept
describing exactly the notification this fix introduces as suppressed.
Both are corrected to note the verdict alerts as of 2026-07-30, without
changing what they actually govern (connector-native execution stays
disallowed either way). Each
prompt's truncation-safe opening paragraph (`claude/dca-executor-prompts/0-
setup-card.md`'s "STANDING GATE 5 OVERRIDE") now also inlines the three Gate
9 alert channels in compact form, not just a reference to Gate 9 by name —
these prompts run 16,000-19,000 characters long against a ~5,850-character
documented truncation precedent, and Gate 9's channel list previously began
past character 12,700, so a truncated fire could have reached the alerting
instruction added here and still had nowhere to point it. The inlined repo
alert file channel is labeled a fallback, not a guaranteed-durable one: per
`claude/GATE5-ALERT-INCIDENT-2026-07-29.md` §4, `dca_paths.py`'s default root
for an unconfigured checkout is a gitignored directory inside the repo
itself, which does not survive a reclaimed session — this channel is only
actually durable when `$DCA_CRON_TRACKING_ROOT` is confirmed set to storage
that outlives the session, which this fix does not itself verify.

The opening paragraph's truncation fallback also now checks Gate 0 first,
not just Gate 5: a first version of this fix jumped straight to
`GATE5_DISABLED` (and its new alert) whenever the fuller gate sequence was
unavailable, without accounting for a weekend or a configured market
holiday, which Gate 0 requires to `END_SILENTLY` with no alert. A truncated
fire on a holiday would otherwise have produced seven false
missing-entry-point alerts and recorded the wrong verdict. That first
correction still only covered two of Gate 0's branches; a further review
round caught the rest: an unreachable repo (Gate 2's own abort, "executor
ran without repo access, no buy list available" — not the missing-entry-
point reason), an unusable calendar file (Gate 0's own abort, "market
calendar unavailable, cannot confirm trading day"), and, for the 1:30/2:30/
3:30 slots specifically, an `early_close_1300` day (the market is already
closed by the time those three fire, so Gate 0 requires `END_SILENTLY`
there too, the same as a holiday). The opening paragraph now mirrors all of
Gate 0's branches in order — weekend, repo reachability, calendar validity,
holiday, and (for the three afternoon-side slots) early close — before
falling back to `GATE5_DISABLED`, so a truncated fire records the right
verdict and reason instead of always defaulting to the missing-entry-point
case. A further round found the same gap one gate later: Gate 1 (time
sanity — an off-schedule firing is a scheduler fault, not a missing entry
point) and Gate 3 (trading window — a fire outside
`config/trading_window.json`'s approved dates must abort with that specific
reason) both logically precede Gate 5 too, and weren't covered. Both were
inlined the same way, but two more issues surfaced on the next round: the
Gate 1 time check had been placed ahead of the rest of Gate 0
(`holidays_closed`/`early_close_1300`), so a truncated fire that was
simultaneously off-schedule and on a closed-market date would have wrongly
alerted for the time mismatch instead of ending silently for the closed
market — reordered so all of Gate 0 resolves before Gate 1 is ever checked,
matching the full gate sequence. And the header's repo-reachable/unreachable
branches had no way to actually locate the repo — Gate 2's real probe
sequence (`git rev-parse`, the `/sessions/*/mnt/...` globs, the
`mcp__remote-devices__device_bash` fallback, and the ambiguous-multiple-
match case) started past the documented truncation boundary, so a truncated
fire could never actually determine reachability to run any of the checks
above it. The compact Gate 2 probe (including the "more than one match"
abort, not previously covered at all) is now inlined too. Gate 4
(sole-executor interlock) is still not inlined — it requires a live broker
call, not a local date/time/config check, so replicating it compactly in a
truncation-safe header isn't practical the same way.

Separately, `config/notifications.json`'s `daily_compact_mode.actionable_events`
list — a second, independent machine-readable notification policy, distinct
from `config/scheduled_tasks.json`'s `connector_native_execution_policy` — did
not include anything matching a blocked entry point. An agent following
that policy literally, to suppress "no-op/heartbeat" notifications, could
still treat the new Gate 5 alert as non-actionable and drop it, reproducing
the exact problem this whole fix exists for. Added `GATE5_DISABLED` as an
explicit actionable event there too, in both the actual `actionable_events`
array and the duplicate plain-English list a follow-up round found still
stale in `routing_conditions.daily_compact_mode`. Marking the event
actionable was still not sufficient on its own: `routing.live_run_alerts`
authorized only `in_app_message_center`, one of the three channels Gate 9
requires for this verdict (in-app, repo alert file, email). A first attempt
widened `live_run_alerts` itself to add email — wrong, on review:
`live_run_alerts` is shared by every live-run alert category, including the
five currently-live legacy tasks, and `channels.email.scope`/
`docs/safeguards.md` are explicit that email goes out only where a task
explicitly requests it, so that change would have emailed on every legacy
live-run alert too, not just this one verdict. Added a separate
`gate5_disabled_alerts` route (in-app + email) instead, scoped to exactly
this verdict, and reverted `live_run_alerts` to its original
`in_app_message_center`-only value. The repo alert file still isn't part of
this config's channel table at all; it's written directly by the executor,
per Gate 9, and this file has no representation for it.

`claude/GATE5-ALERT-INCIDENT-2026-07-29.md` — the document §1 cites above —
itself describes Gate 5's `GATE5_DISABLED` path as silent throughout §3, a
true statement about the prompt text as it stood when that document was
written, now stale. Added a supersession note at its top pointing here,
rather than editing its own analysis: that document is a snapshot of an
investigation at a point in time, and rewriting its findings after the fact
would misrepresent what was actually known when it was written.

This reverses PR #232/#238's "do not schedule tasks that are guaranteed to
alert" guidance for this specific verdict. That guidance was correct given
what was known then; it produced the outcome flagged here once observed in
practice. Trade-off, stated plainly: this now means an alert on all three
channels every single fire, every weekday, until a real entry point exists
(`claude/GATE5-PROPOSAL.md`). That is the accepted cost of the fix.

**This is a task-text change, not a deployed-behavior change, until someone
repastes it.** Per `claude/dca-executor-prompts/0-setup-card.md`, the seven
live Cowork tasks are configured by manually pasting each `.txt` file's
content into the scheduled task; nothing in this repo automatically pushes
committed text into a live task. Editing the files here fixes what the next
paste will contain, not what the already-scheduled tasks do until that paste
happens — the same distinction `claude/GATE5-ALERT-INCIDENT-2026-07-29.md`
§2 draws between "committed to this repo" and "deployed." Repaste all seven
before expecting the new alerting behavior on the next fire.

## 2. Cap mismatch — watchdog vs. tier table

The watchdog was reporting against a flat $200 daily cap. $200/day (and
$1,200/week) is a stale figure from an earlier pasted task revision —
`claude/dca-executor-prompts/0-setup-card.md` already documents this exact
correction for the seven executor prompts themselves ("not a flat $200/day
or $1,200/week") — but it does not match *any* of this repo's current specs,
including the corrected ones, so the first draft of this section was itself
wrong about what to replace it with.

**What the watchdog actually verifies today (corrected):** `a46bf007`
watches the currently-live five-task chain
(`2ba19526`/`42173154`/`7be82b97`/`a6239dae`/`10ce362c`), not the seven-slot
Claude roster — that roster places no orders yet (§1). Per
`config/scheduled_tasks.json`'s `circuit_breakers.budget_caps`, the caps to
check today are per-stream, not one shared total: market-open, morning, and
midday each against their own $90/day, $450/week ledger; the three intraday
slots (11:30/1:30/2:30) against one shared $90/day, $450/week ledger across
all three combined; and only the afternoon final run against
`config/budgets.json`'s tiered table — Normal $180/day, $900/week up to
Stress $360/day, $1,800/week, selected by planned spend and 3x-or-greater
signal count. Even there, Stress's $1,800/week is nominal: per
`docs/live_wiring.md`, it's usable only up to the current week's
actually-verified, unused rollover reserve (capped at $900), so a watchdog
check against the bare $1,800 figure can pass a batch beyond the real,
reserve-backed limit. **The first draft of this section incorrectly told the
watchdog to check a single $180/day, $900/week total shared across all seven
Claude slots — that shared framing is the seven-slot roster's *target*
state once Gate 5 ships and the five old tasks retire
(`docs/safeguards.md#live-execution-roster`), not what the watchdog, which
verifies the currently-live chain, should check today.** Repasting that
shared-table framing into the live watchdog now would have made it miss
breaches of the real per-stream caps, or report against a ceiling that
doesn't apply yet — worse than the original $200 figure it was meant to fix.

Two more gaps surfaced on review. First, the afternoon final run's own cap
check isn't against its tier cap in isolation: `final_buy_budget_tier_fix`
(`config/scheduled_tasks.json`) counts same-day strategy allocation already
placed as usage against the tier's *corrected daily cap* before any final-run
order is evaluated. Its 2026-07-17 text names only manual recovery, morning,
and midday, but the live code is broader — `run_dca.py`'s
`_CROSS_STREAM_PRIOR_ALLOCATION_ROOTS` also folds in market-open and
intraday, feeding `near_close_dca.py`'s `_seed_prior_allocations` into
`day_notional()`/`week_notional()` for absolute-cap enforcement. A watchdog
following only the narrower three-source list, or checking the final run's
own total in isolation, can pass a batch that's over the real corrected cap
once all five same-day sources are counted. Second, every weekly figure
above needs its own query: `a46bf007`'s documented query contract
(`all_orders_reconciliation` in `config/scheduled_tasks.json`) only fetches
same-day orders; comparing that same-day total against any weekly cap
silently omits Monday-through-yesterday spend without a separate,
fully-paginated query from the current ISO week's Monday 00:00 ET.

**Fix applied:** `docs/schedule_manifest.md`'s `a46bf007` (live-run watchdog)
section now states the correct, currently-applicable budget-cap contract
(per-stream ledgers plus the afternoon tiered table, with the Stress/reserve
caveat, the full five-source final-run prior-allocation list, and the
week-scoped query requirement), not the seven-slot shared table, so there is
one authoritative, unambiguous place to sync the watchdog's task text
against. This repo cannot push that fix to the live watchdog task itself:
`a46bf007` has no in-repo
executor, the same limitation already noted for
`2ba19526`/`7be82b97`/`bb4f6ef1` — though unlike those three it carries no
`no_python_entry_point` config flag of its own. It does have its own
dedicated task object in `config/scheduled_tasks.json` (`id: "a46bf007"`,
`name: "Live-run watchdog"`, with its own `cron_utc`, `run_classification`,
and `notes` fields) — an earlier draft of this document wrongly said it had
none at all; only that one specific machine-readable flag is absent from it,
and the "no in-repo executor" fact is currently recorded only in that
object's prose `notes`, not as a typed field the way
`2ba19526`/`7be82b97`/`bb4f6ef1` record it.
Repasting the watchdog's task text against
`docs/schedule_manifest.md#a46bf007-live-run-watchdog` (or directly against
`config/scheduled_tasks.json`/`config/budgets.json`) is a human step outside
this repo.

## 3. Window dispute — unresolved, not fixed here

The watchdog states the original approval date as 2026-08-28; this repo's
config states 2026-10-30 (`config/trading_window.json`, extended 2026-07-26).
Both are real: `config/trading_window.json`'s own `end_date` was extended
from 2026-08-28 to 2026-10-30 on 2026-07-26 "to avoid the approved-window
hard stop while Phase 2's real market calendar is pending." The extension
landed *before* the checkpoint that was supposed to authorize it: the
one-time reminder task `f7411889` was scheduled for **2026-08-15 11:45 AM
ET**, specifically "to evaluate continuing live testing past 2026-08-28" —
and the config extension happened three weeks earlier, on 2026-07-26. This
is already flagged as stale in `docs/schedule_manifest.md` (`f7411889`'s own
entry) and in `PRODUCTION_READINESS.md`; this document adds nothing new to
that finding, only reconfirms it is still open.

**No code or config change is made for this.** Whether the 2026-07-26
extension stands as a retroactively-ratified decision, gets rolled back
pending the (now-stale) 2026-08-15 checkpoint, or the checkpoint itself gets
rescheduled and re-run against the current window, is a decision for a
human, not something to resolve unilaterally in a same-day fix pass.

## Revision history

- **2026-07-30 (fourteenth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the thirteenth revision found four more
  issues, all addressed above: (1) P2 — the thirteenth revision's shared
  truncation-safe channel block wrote the compact repo alert file to a fixed
  `<slot>-gate5.txt` name regardless of which branch actually triggered it,
  mislabeling a Gate 0/1/2/3 abort as a Gate 5 incident and letting a later
  alert for the same slot/day silently overwrite the earlier one's evidence.
  Changed the filename to `<slot>-<gate>.txt`, using whichever gate actually
  failed, matching the full Gate 9 contract's own naming. (2) P1 — the
  thirteenth revision made `GATE4_EXPECTED_OVERLAP` alert in the prompts but
  did not update `config/notifications.json` to match: `daily_compact_mode`'s
  actionable-events allowlist and `routing_conditions.daily_compact_mode`
  still omitted it, and no routing entry authorized the email channel the
  prompts now require. Added the event to both allowlist copies and a
  dedicated `gate4_expected_overlap_alerts` route (in-app + email), mirroring
  `gate5_disabled_alerts` exactly, including why it's a separate route rather
  than a widened `live_run_alerts`. (3) P1 — `config/scheduled_tasks.json`'s
  `operating_mode.actionable_notification_events` (the five-legacy-task
  daily-compact policy, separate from `config/notifications.json`'s
  executor-facing copy) still had no entry for a missing/blocked repository
  entry point, even though `connector_native_execution_policy`'s removal
  (already on `main`, prior to this branch) now requires those five tasks to
  fail closed and alert on exactly that condition. Added the event there too,
  so an agent honoring "notify only for actionable events" doesn't suppress
  it as a no-op. (4) P1 — `docs/schedule_manifest.md`'s Stress weekly-cap
  bullet said capacity is usable "up to" the rollover reserve without stating
  the actual formula, which reads as "the reserve itself is the cap." Added
  the live calculation from `near_close_dca.py`'s `effective_weekly_cap()`
  (lines 738-750): `min($1,800, $900 + verified_rollover_reserve_usd)`, with
  a worked example ($300 reserve -> $1,200 ceiling, not $300 or $1,800).
- **2026-07-30 (thirteenth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the twelfth revision found two more issues,
  both addressed above: (1) P1 — each prompt's truncation-safe opening
  paragraph defined the three Gate 9 alert channels only in its final
  sentence, after every Gate 0/1/2/3 abort branch that precedes it in the
  same paragraph; a truncated fire that aborted at one of those earlier
  branches (an unreachable or ambiguous repo, an unusable calendar, an
  off-schedule firing, or a fire outside the trading window) reached
  "abort and alert" text with no channel list yet in view. Moved the
  three-channel definition ahead of every Gate 0/1/2/3 branch in all seven
  prompts' opening paragraph, and had each "abort and alert" reference it
  explicitly, so every alerting branch in the truncation-safe prefix — not
  only the final `GATE5_DISABLED` fallback — owes all three channels. (2)
  P1 — the 10:30, 12:30, and 3:30 prompts' `GATE4_EXPECTED_OVERLAP`
  classification (Gate 4 recognizing the legacy stream's own same-window
  order) still ended those fires silently, the same silent-by-design
  behavior this document exists to remove from `GATE5_DISABLED`. Because
  Gate 5 stays disabled regardless of how Gate 4 classifies the window, an
  expected-overlap fire and a blocked-entry-point fire both end in "nothing
  placed, Gate 5 permanently disabled" — so three of seven slots could still
  go silent on exactly the normal overlap day this classification exists
  for, contradicting the claim that every blocked-entry-point fire alerts.
  `GATE4_EXPECTED_OVERLAP` now alerts on all three Gate 9 channels in those
  three prompts, `0-setup-card.md`, and `docs/safeguards.md`, the same as
  `GATE5_DISABLED` — it remains a known, expected classification, not a
  failure to escalate, but known-and-expected no longer means silent for
  either verdict.
- **2026-07-30 (twelfth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the eleventh revision found one more issue,
  addressed above: P2 — the eleventh revision's fix widened the *shared*
  `live_run_alerts` route to add email, which would have emailed on every
  live-run alert category (including the five currently-live legacy tasks'
  own alerts), contradicting `channels.email.scope`/`docs/safeguards.md`'s
  "email only where a task explicitly requests it." Reverted
  `live_run_alerts` to `in_app_message_center`-only and added a separate,
  narrowly-scoped `gate5_disabled_alerts` route instead.
- **2026-07-30 (eleventh revision):** a further PR review round
  (`chatgpt-codex-connector`) on the tenth revision found one more issue,
  addressed above: P2 — even with `GATE5_DISABLED` marked actionable in both
  `daily_compact_mode` copies, `config/notifications.json`'s
  `routing.live_run_alerts` still authorized only `in_app_message_center`,
  one of the three channels Gate 9 requires for this verdict. Expanded the
  route to include email, and documented that the repo alert file isn't a
  channel this config table represents at all — it's written directly by
  the executor per Gate 9.
- **2026-07-30 (tenth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the ninth revision found two more issues,
  both addressed above: (1) P2 — the ninth revision's inlined Gate 1 (time)
  check was ordered ahead of the rest of Gate 0 (`holidays_closed`/
  `early_close_1300`), so a truncated fire that was both off-schedule and on
  a closed-market date would have alerted for the time mismatch instead of
  ending silently for the closed market, contradicting the full gate
  sequence's own order. Reordered so all of Gate 0 resolves first. (2) P2 —
  the header's repo-reachable/unreachable branches had no way to actually
  locate the repo; Gate 2's real probe (the ordered `git rev-parse`/
  `/sessions/*/mnt/...`/`mcp__remote-devices__device_bash` sequence, plus its
  own "more than one match" abort, not previously covered at all) started
  past the documented truncation boundary. Inlined a compact version of that
  probe, including the ambiguous-match case, so the checks above it can
  actually run on a truncated fire.
- **2026-07-30 (ninth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the eighth revision found two more issues,
  both addressed above: (1) P2 — the truncation-safe header's Gate 0
  coverage still didn't reach Gate 1 (time sanity) or Gate 3 (trading
  window), both of which logically precede Gate 5; a truncated off-schedule
  fire or a fire outside the approved trading window would have been
  misreported as the routine missing-entry-point case instead of its own
  more specific abort. Inlined both checks, with each gate's own abort
  reason, ahead of the `GATE5_DISABLED` fallback. (2) P2 —
  `claude/GATE5-ALERT-INCIDENT-2026-07-29.md` still described Gate 5's
  `GATE5_DISABLED` path as silent throughout its §3, true when written, now
  stale. Added a supersession note there rather than rewriting that
  document's own historical analysis.
- **2026-07-30 (eighth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the seventh revision found two more issues,
  both addressed above: (1) P1 — the seventh revision added `GATE5_DISABLED`
  to `config/notifications.json`'s `daily_compact_mode.actionable_events`
  array but left the duplicate, plain-English parenthetical list in
  `routing_conditions.daily_compact_mode` stale; a consumer of that second
  copy could still treat the alert as a non-actionable no-op. Added the
  event there too. (2) P2 — this document claimed the Gate 5 fix alerts "on
  all three Gate 9 channels ... plus whatever Slack step is wired per-slot,"
  but none of the seven committed prompts has a Slack step, and
  `config/notifications.json`/`0-setup-card.md` are explicit that these
  prompts use in-app messaging and email, not Slack. Removed the claim and
  clarified that any Slack step on the live tasks exists only in
  separately-pasted text outside this repo's own channel list.
- **2026-07-30 (seventh revision):** a further PR review round
  (`chatgpt-codex-connector`) on the sixth revision found two more issues,
  both addressed above: (1) P1 — `config/notifications.json`'s
  `daily_compact_mode.actionable_events` list, a second machine-readable
  notification policy independent of `connector_native_execution_policy`,
  did not include the blocked-entry-point case; an agent following it
  literally could still suppress the new Gate 5 alert as non-actionable.
  Added `GATE5_DISABLED` to that list. (2) P2 — the sixth revision's Gate-0
  priority fix only covered the weekend and `holidays_closed` branches;
  Gate 0 also requires an unreachable repo and an unusable calendar file to
  abort (with their own distinct reasons, not the missing-entry-point one),
  and requires the 1:30/2:30/3:30 slots specifically to end silently on an
  `early_close_1300` day. Extended the truncation-safe header in all seven
  prompts to mirror every Gate 0 branch, in order, before falling back to
  `GATE5_DISABLED`.
- **2026-07-30 (sixth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the fifth revision found two more issues,
  both addressed above: (1) P1 — `config/scheduled_tasks.json`'s
  `connector_native_execution_policy` (and its intraday-slot copy) asserted
  in prose that a missing-entry-point Gate 5 fire is "the silent,
  non-alerting `GATE5_DISABLED` verdict" — a machine-readable policy record
  independent of the prompts and docs, left unsynced by every earlier
  revision of this fix. Corrected both to note the verdict now alerts,
  without changing what the policy actually governs (connector-native
  execution stays disallowed). (2) P2 — the truncation-safe opening
  paragraph jumped straight to `GATE5_DISABLED` (and its new alert) whenever
  the fuller gate sequence was unavailable, without first checking Gate 0's
  weekend/holiday `END_SILENTLY` case; a truncated fire on a market holiday
  would have produced seven false missing-entry-point alerts. Added a
  compact day-of-week and (repo-permitting) `holidays_closed` check ahead of
  the `GATE5_DISABLED` fallback in all seven prompts, matching Gate 0's own
  priority order.
- **2026-07-30 (fifth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the fourth revision found three more issues,
  all addressed above: (1) P2 — the newly-inlined repo alert file channel in
  each executor prompt's truncation-safe header was labeled "durable
  fallback" without the caveat `claude/GATE5-ALERT-INCIDENT-2026-07-29.md` §4
  already established: an unconfigured checkout's default artifact root is a
  gitignored directory inside the repo (`dca_paths.py`) that does not survive
  a reclaimed session. Relabeled "fallback" with an explicit durability
  caveat in all seven prompts and here. (2) P1 — the final-run prior-
  allocation description in §2 (mirroring the 2026-07-17 config text) named
  only manual recovery, morning, and midday, but `run_dca.py`'s
  `_CROSS_STREAM_PRIOR_ALLOCATION_ROOTS` and `near_close_dca.py`'s
  `_seed_prior_allocations` also fold market-open and intraday allocation
  into absolute-cap enforcement; a watchdog following the narrower list could
  pass a final batch the live code would consider over cap. Broadened to all
  five same-day sources. (3) P1 — no weekly cap comparison in §2 or
  `docs/schedule_manifest.md` noted that the watchdog's own documented query
  (`all_orders_reconciliation`) is same-day only; comparing a same-day total
  against any weekly figure needs its own separate, fully-paginated,
  Monday-scoped query. Added.
- **2026-07-30 (fourth revision):** a further PR review round
  (`chatgpt-codex-connector`) on the third revision found one more issue: P2
  — the third revision's correction to §2 (and the matching bullet in
  `docs/schedule_manifest.md`) said `a46bf007` "has no dedicated task object
  in `config/scheduled_tasks.json` at all, only schedule-array entries and
  prose references." That's wrong: `config/scheduled_tasks.json:3464-3513`
  is exactly such an object (`id: "a46bf007"`, `name`, `type`, `cron_utc`,
  `run_classification`, `notes`). The only accurate claim was narrower — it
  doesn't carry the specific `no_python_entry_point` flag that
  `2ba19526`/`7be82b97`/`bb4f6ef1` do, and records "no in-repo executor" only
  in that object's prose `notes` field. Corrected above and in
  `docs/schedule_manifest.md`.
- **2026-07-30 (third revision):** a further PR review round
  (`chatgpt-codex-connector`) on the second revision found two more issues:
  (1) P1 — §2's corrected watchdog cap contract described the afternoon
  stream's tiered cap in isolation, but the live `final_buy_budget_tier_fix`
  contract (`config/scheduled_tasks.json`) requires same-day strategy
  allocation already placed via manual recovery, morning, and midday to be
  counted against that tier's *corrected daily cap* before any final-run
  order is evaluated — a watchdog checking only the final run's own total
  against the nominal tier figure could pass a batch that, combined with
  prior same-day allocation, breaches the real cap; added to
  `docs/schedule_manifest.md`. (2) P2 — this document's own commits are
  timestamped before the 2026-07-30 market open, contradicting its claim to
  describe that day's completed 12:30-3:30 PM ET outcomes; added an explicit
  dating caveat in §0 rather than guessing at the correct date.
- **2026-07-30 (second revision):** a same-day PR review round
  (`chatgpt-codex-connector`) on the original draft found five issues, all
  addressed above: (1) P1 — §2's corrected cap table asserted a single
  $180/day, $900/week total shared across all seven Claude slots, but the
  watchdog verifies the currently-live five-task chain, which uses per-stream
  $90/$450 ledgers (market-open/morning/midday), one shared $90/$450 ledger
  across the three intraday slots, and the tiered table for the afternoon
  stream only — the shared-across-seven framing is the target state after
  Gate 5 ships, not what applies today; corrected to the real, currently-live
  cap structure. (2) P1 — even the afternoon stream's Stress tier was stated
  as a flat $1,800/week cap without noting it's nominal, not the effective
  ceiling, which is bounded by the actually-verified weekly rollover reserve
  (capped at $900); added. (3) P1 — this document claimed the fix to the
  seven executor prompts was already in effect, without noting that Cowork
  tasks are configured by manual paste and editing the committed `.txt`
  files alone does not change already-deployed task behavior; added an
  explicit repaste-required callout here and in `0-setup-card.md`. (4) P1 —
  the truncation-safe opening paragraph referred to Gate 9 by name for its
  alert-channel instructions, but Gate 9 itself is past the documented
  truncation boundary in these prompts, so a truncated fire would have had
  nowhere to point the new alerting instruction; the three channels are now
  inlined in compact form directly in that opening paragraph. (5) P2 — §2
  asserted `a46bf007` carries `no_python_entry_point=true` in config; no
  such flag exists on it (it has no dedicated task object at all, unlike
  `2ba19526`/`7be82b97`/`bb4f6ef1`, which do carry that flag) — corrected to
  state only what's actually in the repo (no in-repo executor, prose-only).
- **2026-07-30 (original draft, superseded):** see above for what it got
  wrong.
