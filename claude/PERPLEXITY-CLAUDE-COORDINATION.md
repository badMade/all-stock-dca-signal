# Claude / Perplexity first-come-first-serve coordination

Written 2026-08-02, per Geoff's direction: "I want Perplexity and Claude to be
able to run the program right now. First come, first serve. If one fails, it
notifies the other to run as a backup, independently per slot. Long term,
Claude is primary and Perplexity is backup; for now Claude hasn't been firing,
so it's only been backups."

**Status: design + partial wiring. One value below still needs to be
confirmed against real Robinhood order data before this is fully load-bearing
-- see §3.** Until then, treat this as the mechanism, not yet a finished,
verified fix.

**Update 2026-08-07 -- shipped as a temporary, unconfirmed carve-out, not a
finished fix.** The 3:30 PM final slot aborted today (Gate 4) on 8 orders
tagged `placed_agent='agentic'`, created 19:33:07-19:33:41Z -- squarely inside
the interlock window, but also squarely overlapping this same slot's own
`lastRunAt` (19:33:14Z per the scheduler), so the evidence is at least as
consistent with a duplicate/double-fire of this Claude task as with a genuine
Perplexity fill. The §3 empirical-confirmation step below was NOT completed:
no known Perplexity order was checked against a known timestamp. Geoff
directed proceeding anyway, accepting that residual risk, scoped to expire
2026-08-14 (re-review after that date). `PERPLEXITY_PLACED_AGENT_VALUE` below
is therefore ACCEPTED, not CONFIRMED. The §3 Gate 4 addition (ticker-level
exclusion, rogue-vs-compatible split) is now live in six of the seven
scheduled Claude tasks (935, 1030, 1130, 1230's midday text is UNCHANGED --
see next paragraph --, 330, 130, 230), each carrying its own copy of this
carve-out with the same 2026-08-14 expiry note. The §4 Perplexity-side mirror
below was NOT pasted into Perplexity's own prompts as part of this update
(Perplexity cannot read this repo -- see §4 -- so that remains a manual step
for Geoff).

The 1230 (12:30 PM) slot's Gate 4 is NOT covered by this update: per §5.5
below, it runs through `dca_runner.assess()`'s `gate_sole_executor()`, which
deliberately ignores `placed_agent` entirely, so the carve-out cannot be
applied there without a real code change to `dca_runner.py`. Low urgency in
practice, since that slot's live placement is still off pending validation.

## 1. What "first come, first serve" actually means here

There is still no medium both engines can read and write (see
`claude/PERPLEXITY-SAME-DAY-TOPUP-GUARD.md` §4 -- Slack is unconfirmed on the
Perplexity side, and this repo's `config/notifications.json` deliberately
keeps Slack off for anything routine anyway). What both engines already read
and write is the **same Robinhood account's order history**. That is the only
real shared medium, and it is enough to implement "first come, first serve"
without inventing a new channel:

- **Claiming a slot = placing a real order in that slot's window.** Whichever
  engine actually gets an order into the broker for a given slot's window has
  won that slot. There is nothing to arbitrate beforehand -- the broker's own
  timestamped order history is the single source of truth for "who got there
  first," after the fact, which is exactly what Gate 4 (sole-executor
  interlock) already reads.
- **"Notifies the other to run as backup" = the other engine's own check finds
  nothing and proceeds normally.** Neither engine can push a message to the
  other. What each engine CAN do is check the broker before it trades: if the
  window already has a qualifying order from the OTHER engine, stand down
  (first-come-first-serve, quietly); if the window is empty, proceed --
  which is exactly what "the other one is acting as backup" looks like from
  the outside, with no explicit signal required. A failed/aborted attempt
  looks identical to "no attempt yet" from the broker's point of view (no
  order landed), so the backup engine's ordinary per-slot check already
  covers the failure case for free.
- **This does not solve a genuinely simultaneous start.** If both engines
  check the broker before either has placed anything, both can see an empty
  window and both proceed. `claude/PERPLEXITY-SAME-DAY-TOPUP-GUARD.md` §4
  already accepts the identical residual risk for same-day top-up sizing;
  this document accepts it for slot ownership too, for the same reason: no
  shared medium exists to close it, and the two engines are not scheduled by
  a common clock either way.
- **FIXED 2026-08-02 (was: known limitation -- Codex review, P2: "Allow
  backup after partial-batch failures").** "Any order in the window" is
  coarser than "the batch completed": Gate 7 in every placing prompt submits
  sequentially and stops the ENTIRE batch on the first rejection, so a run
  can genuinely fail after placing SOME of its intended orders, not none.
  The original design stood a backup engine down ENTIRELY the moment it saw
  ANY qualifying order in the window, which would have left the REST of a
  partial batch permanently unexecuted rather than picked up. Fixed by
  ticker-level EXCLUSION instead of a blanket slot-level stand-down: Gate 4's
  compatible-executor branch no longer terminates the run. It notes the
  compatible executor's activity and continues into Gate 5, generating this
  slot's candidate list with every ticker the compatible executor already
  placed an order for IN THIS SAME WINDOW excluded entirely -- not passed
  through Gate 6.5's same-day top-up guard for a resized delta. Only the
  tickers the compatible executor never touched this window proceed through
  Gates 5-7 as normal (so a partial-batch failure IS picked up), with Gate
  6.5 still governing ordinary cross-slot repeats for those tickers exactly
  as before. **Correction, 2026-08-02 (Codex review: "Exclude same-window
  tickers from backup top-ups"):** the first version of this fix let Gate
  6.5's same-day top-up guard compute a resized delta for a ticker the
  compatible executor had already partially covered THIS slot, e.g. $2
  placed by the compatible executor against a $4 refreshed target producing
  a $2 delta here -- but `config/budgets.json`'s
  `one_order_per_ticker_per_stream` blocks a SECOND buy for the same ticker
  within the same slot/session regardless of size; that same-day top-up
  guard exists to reconcile a legitimate repeat across DIFFERENT
  slots/sessions on the same day, not a same-window duplicate. Sizing a
  same-window delta there was the second-buy-same-session case the policy
  blocks, not a permitted resized top-up. Excluding the ticker from this
  slot's candidate list entirely (rather than letting Gate 6.5 size it)
  closes that gap. See the current Gate 4 text in any of the six live-mirror
  prompts for the authoritative wording. **Correction, same day:** the first
  version of this fix only applied ticker-level reconciliation in the
  Claude-backs-up-Perplexity direction; §4's Perplexity-side mirror still
  stood down entirely the moment it saw any Claude order, so a partial
  CLAUDE batch failure was not recoverable by Perplexity (Codex review:
  "Reconcile partial Claude batches on the Perplexity side"). §4 below now
  mirrors the same per-ticker exclusion in that direction too, so the fix is
  genuinely symmetric.

## 2. The current live mechanism this must plug into

The six live-mirror prompts that place orders (0935/1030/1130/1330/1430/1530;
`claude/dca-executor-prompts/live-mirror/*.txt`) already have a Gate 4,
"SOLE-EXECUTOR INTERLOCK": it fetches today's orders in the slot's window and
classifies each by `placed_agent`:

- `placed_agent` is `'user'`, `'recurring'`, or `'drip'` -> Geoff's own manual
  activity, noted but never aborts the run.
- `placed_agent` is `'agentic'`, missing, or unrecognized -> treated as a
  competing executor, and the run **aborts and alerts** ("executor-placed
  orders already present ... a second executor may be live").

**This has no "recognized, expected, compatible executor" branch at all.**
Every one of Perplexity's own orders in a slot's window -- assuming its
`placed_agent` is anything other than `'user'`/`'recurring'`/`'drip'`, which
is the likely case for an automated executor -- currently reads as an
unrecognized/agentic competing executor, so **Gate 4 alone is enough to abort
every Claude fire before Gate 5 (the still-missing signal-generation entry
point) is ever reached**, on every slot where Perplexity has already traded.
That is a second, independent reason Claude hasn't been firing, on top of
Gate 5's own missing entry point -- and it means every one of those fires is
also generating a same-three-channel ABORT alert as routine noise, every
slot, every day.

## 3. The fix, and the one thing that must be confirmed first

Add a third classification to Gate 4: a **recognized compatible executor**.
When every qualifying order in the window is attributable to a recognized
compatible executor, stand down quietly (log it, do not run the
three-channel ABORT alert) -- this slot has already been served by the other
half of the standing first-come-first-serve pair, which is expected, not an
anomaly. Continue to abort-and-alert on anything else unrecognized, exactly
as today: an order that is NOT explainable as Geoff's own manual activity
and NOT explainable as the recognized compatible executor is still a real,
alertable "a second executor may be live" event.

**What "recognized compatible executor" resolves to is not yet filled in.**
Doing that requires reading a handful of Perplexity's actual recent orders'
`placed_agent` field (or whatever field Robinhood's `get_equity_orders`
response actually populates for them) via `get_equity_orders` against known
Perplexity-placed fill timestamps, and confirming the exact literal value(s).
Guessing here is actively worse than leaving Gate 4 as it is today:

- Guess a value Perplexity doesn't actually use -> nothing changes, Claude
  keeps alerting every fire, no harm done, but also no fix.
- Guess a value that turns out to ALSO cover a genuine rogue/unexpected
  executor -> Gate 4 goes quiet on exactly the case it exists to catch.

Until that value is confirmed, `PERPLEXITY_PLACED_AGENT_VALUE` below is a
placeholder, not a shipped constant. Whoever confirms it should record the
real value (and how it was confirmed -- which order IDs, which timestamps)
directly in this section.

```
PERPLEXITY_PLACED_AGENT_VALUE = "agentic"  # ACCEPTED 2026-08-07 by Geoff's direction,
                                            # NOT independently confirmed against a known
                                            # Perplexity order/timestamp -- see the
                                            # 2026-08-07 update note above. Also the value
                                            # this task's own orders would carry. Expires
                                            # 2026-08-14; re-confirm properly or renew.
```

Gate 4's classification, once that value is real (this is the mirror of the
actual text now shipped in all six live-mirror prompts -- see those files for
the authoritative wording):

> Scope the interlock to executor traffic, and classify EVERY order in the
> window before deciding anything -- do not act on the first order examined.
> For each order: it is GEOFF'S OWN ACTIVITY (never counts toward anything
> below) when `placed_agent` is explicitly `'user'`, `'recurring'`, or
> `'drip'`; otherwise it is a COMPETING-EXECUTOR SIGNAL. Split every
> competing-executor signal into COMPATIBLE, when `placed_agent` matches
> `PERPLEXITY_PLACED_AGENT_VALUE` exactly, or ROGUE, when it does not (fail
> safe: unknown/unrecognized provenance is rogue, never compatible). Decide
> only after every order in the window has been classified: if the window
> contains ANY rogue signal, abort and alert exactly as today -- this holds
> even when a compatible order is ALSO present, since a rogue order
> coexisting with Perplexity's claim means a third, unauthorized executor may
> be live and a quiet stand-down would suppress that collision (Codex
> review: "Check for rogue orders before the compatible stand-down"). When
> the window contains ZERO rogue signals AND at least one compatible signal,
> do NOT stand down entirely and do NOT abort: note the compatible
> executor's activity in the final report and continue into Gate 5, but
> generate this slot's candidate list EXCLUDING every ticker the compatible
> executor already placed an order for in THIS SAME window:
> `config/budgets.json`'s `one_order_per_ticker_per_stream` safeguard blocks
> a second buy for the same ticker within the same slot/session regardless
> of size, so Gate 6.5's same-day top-up guard -- built to reconcile a
> legitimate repeat across DIFFERENT slots/sessions on the same day, not a
> same-window duplicate -- must never be asked to size a same-window overlap
> (Codex review: "Exclude same-window tickers from backup top-ups"). Only run
> tickers the compatible executor's window did not already cover through
> Gates 5-7 as normal, with Gate 6.5 still governing cross-slot repeats for
> those tickers exactly as before (Codex review: "Allow backup after
> partial-batch failures" -- a coarse "any order in the window means this
> slot was served" rule left the rest of a partial batch permanently
> unexecuted when Gate 7 stopped mid-batch on an earlier rejection; excluding
> only the already-covered tickers closes that gap without risking a
> duplicate submission on any ticker the compatible executor already
> completed). This design does not reopen the earlier "Stop after
> recognizing the compatible executor" concern (Codex review -- an earlier
> draft said only "do NOT abort" with no reconciliation mechanism at all,
> which risked placing a duplicate FULL batch): continuing here is safe
> specifically because every ticker the compatible executor already touched
> this window is excluded outright, not resized, so nothing already covered
> is ever resubmitted. This is a distinct outcome from a rogue signal, which
> still aborts unconditionally, and from Geoff's own manual activity, which
> never aborts and is simply noted in the final report so the manual and
> automated streams stay legible, then continue as normal.

## 4. Paste-ready addition for Perplexity's own prompts

Perplexity cannot read this repo (see the same-day top-up guard doc, §1), so
the mirror image of §3 has to be restated as a manual step for Geoff to paste
into Perplexity's five task prompts, the same way the top-up guard was.

> ### FIRST-COME-FIRST-SERVE CHECK (run before placing anything)
>
> Before sizing or placing any order for this slot, call `get_equity_orders`
> for the account with `created_at_gte` set to today, and follow every
> pagination cursor to completion. Classify EVERY order whose `created_at`
> falls inside this slot's window (the same window this task already uses
> for its own duplicate-order/top-up checks) before deciding anything --
> do not act on the first order examined. An order is Claude-attributable
> ONLY when its order/client identifier carries the Claude-specific prefix
> `dca-runner-<this slot's id>-`. A bare `placed_agent` of `'agentic'` alone
> is NOT sufficient on its own -- that value is generic enough that another
> automation could share it, so treating it alone as Claude's mark could
> silently suppress a genuine collision with a third, unrecognized executor
> (Codex review: "Require Claude-specific evidence before standing down").
> Any order in the window that is NOT Claude-attributable by that identifier
> test AND is not your own just-placed activity is UNRECOGNIZED.
>
> Decide only after the whole window is classified: if it contains ANY
> unrecognized order, treat this the same as any other same-window collision
> under this task's existing safety rules (escalate/alert per that rule) --
> do not silently stand down, since an unrecognized order alongside a
> Claude-attributable one means a third executor may be live, not just
> Claude. When the window contains ZERO unrecognized orders AND at least one
> Claude-attributable order, do NOT stand down entirely and do NOT abort:
> note Claude's activity, then size and place your own candidates for this
> slot as normal, EXCLUDING every ticker Claude already placed an order for
> in THIS SAME window entirely -- do not use your own same-day top-up guard
> (see `claude/PERPLEXITY-SAME-DAY-TOPUP-GUARD.md`) to size a delta for one
> of those tickers here. That guard exists to reconcile a legitimate repeat
> across DIFFERENT slots/sessions on the same day; a same-window overlap
> with Claude is a second-buy-same-session case, which stays blocked
> regardless of size, not a resized top-up (Codex review: "Exclude
> same-window tickers from backup top-ups"). Size and place only the tickers
> Claude's window did not already cover, using your own same-day top-up
> guard as normal for genuine cross-slot repeats on those tickers. This is a
> straight mirror of the Claude-side fix (Codex review: "Reconcile partial
> Claude batches on the Perplexity side" -- an earlier draft of this
> instruction stood down entirely the moment ANY Claude order was seen,
> which left the rest of a partial Claude batch permanently unexecuted if
> Claude's own Gate 7 stopped mid-batch on an earlier rejection, exactly the
> same partial-batch-failure gap already fixed on the Claude side).
> Otherwise (no Claude-attributable order and no unrecognized order),
> proceed as normal. Recognizing Claude's partial or full activity is not a
> special case to escalate or report loudly; a slot Claude already handled,
> in whole or in part, is the expected, designed outcome of running both
> executors side by side, not a fault.

This is the Perplexity-side mirror of §3's Gate 4 addition: whichever engine's
qualifying order shows up first in the window wins that ticker, and the other
recognizes that and sizes its own remaining candidates against it via the
same-day top-up guard rather than needing anything pushed to it.

## 5. Long-term Claude-primary / Perplexity-backup, and what's true today

Nothing above hard-codes a priority order -- "first come, first serve" is
symmetric by design, matching Geoff's framing that it should be independent
per slot. The Claude-primary intent is achieved structurally, not by
special-casing one engine over the other:

- **Today:** Claude cannot complete Gate 5 for any slot (no signal-generation
  entry point exists yet -- see `claude/GATE5-PROPOSAL.md`), so it never
  places an order, so Perplexity's own per-slot check (§4) never finds a
  Claude order in the window and always proceeds. Perplexity wins every slot
  by construction, which matches exactly what Geoff described: "for now
  Claude hasn't been firing, so it's only been backups."
- **Once Gate 5 is wired for a slot:** Claude reaches Gate 7 (placement)
  faster in practice than Perplexity is likely to, simply because Claude's
  Cowork schedule and Perplexity's cloud schedule both fire at the same
  nominal cron time and there's no reason to expect Perplexity to
  systematically win a race neither side is tuned to win -- but nothing here
  guarantees Claude wins a given morning. If genuine Claude-first priority
  is wanted once Gate 5 works (rather than true 50/50 first-come-first-serve),
  the concrete way to get it is to fire Perplexity's own cron a few minutes
  LATER than Claude's for the same slot, so Claude's check almost always runs
  against an empty window and Perplexity's own check almost always finds
  Claude has already served it. That is a scheduling change on the
  Perplexity side, not something this repo's config can enforce.
- **This document does not retire Perplexity, does not wire Gate 5, and does
  not change any trading behavior by itself.** It only changes what Gate 4
  does with an order it already sees, and gives Geoff the paste-ready text to
  make Perplexity's side symmetric. See `claude/PERPLEXITY-SAME-DAY-TOPUP-GUARD.md`
  §5 item 4 for the still-unresolved tension between keeping Perplexity live
  and `connector_native_execution_policy`'s blanket "no executor without a
  reviewed repository entry point" text -- this document does not resolve
  that either.

## 5.5 The 1230 slot's Gate 4 is different code, not covered by §3

The 1230ET slot (`claude/dca-executor-prompts/live-mirror/1230ET-1230-pm-midday-buying-run.txt`)
does not use the placed_agent-based Gate 4 text described in §2/§3 -- it calls
`dca_runner.assess()`, whose own sole-executor check (`gate_sole_executor()`)
deliberately ignores `placed_agent` entirely (see its docstring: "Manual and
automated buying draw on one shared pool"), a different design choice made
for a different reason. Extending the recognized-compatible-executor idea to
that path is a real code change to `dca_runner.py`, not a prompt-text edit,
and is left as a separate, explicit follow-up -- not attempted here. It is
lower urgency in practice: that slot's live placement is already OFF pending
validation, so today it cannot place a duplicate order regardless of what
Gate 4 decides.

## 6. What this supersedes

`docs/safeguards.md`'s "Live execution roster" section and
`claude/GATE5-PROPOSAL.md` / `claude/dca-executor-prompts/0-setup-card.md`
previously described the seven-slot roster running alongside the five legacy
tasks only as a **transitional** state, with an explicit instruction that
once Gate 5 is wired and the old tasks retire, any future overlap "must
revert to abort-and-alert on any collision" -- i.e. a hard cutover, not
standing coexistence. That framing is superseded by Geoff's 2026-08-02
direction: the two executors are meant to coexist indefinitely, coordinating
per slot as described above, not handed off in one flip of a switch. See the
updated "Live execution roster" section in `docs/safeguards.md` for the
current framing.
