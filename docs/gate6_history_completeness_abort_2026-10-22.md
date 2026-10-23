# Gate 6 weekly-history completeness abort, 2026-10-22

**Status: incident record plus an operator decision. No order was placed by the aborted slots.**
Written from the operator's commit message and the description of PR #1895. Anything not in
those two sources is marked "not known" rather than guessed.

## Answer

On 2026-10-22 the 0935 and 1030 live slots aborted at Gate 6 because the weekly order history
could not be proven complete. The operator decided that an unproven history should log a
WARNING and continue, not abort, because weekly spend (about $192) is far below the $900 cap.
That decision trades a fail-closed rule for availability; the trade-off and the condition for
reversing it are below.

## Facts (from the sources above)

| Item | Value |
| --- | --- |
| Date | 2026-10-22 |
| Slots affected | 0935 and 1030 |
| Gates 0 to 5 | Passed (0935: 65 candidates) |
| Gate 6 result | Abort, nothing bought |
| `get_equity_orders` result | About 81 orders, no pagination cursor |
| Oldest order returned | Monday 09:59 ET |
| Weekly spend at the time | About $192 |
| Weekly cap | $900 |

## Why it aborted

Gate 6 sums weekly spend from broker order history. The tool returned a page with no cursor,
and its oldest row (Monday 09:59 ET) was later than the start of the weekly window. Without a
cursor or an older boundary row, the gate cannot show it saw every order in the window, so it
treated the history as unproven and aborted. This matches the fail-closed rule in
`docs/reliability_spec.md` section 3 and 4 (`BROKER_HISTORY_INCOMPLETE`, `$0` buy).

Not known: whether the history really was complete (81 orders may be everything since Monday),
or whether the tool truncates silently. The sources do not say.

## Decision

Treat unproven completeness as a logged WARNING and size from the history as returned.
Implemented for the seven live-mirror prompts by PR #1895 (rule name
`HISTORY COMPLETENESS IS LOG-AND-CONTINUE`).

Still fail closed: tool errors or refusals, unfollowable cursors, malformed rows, rogue
in-window orders, Gate 1 window checks, and cap, count and buying-power math. The engine
`decide_effective_top_up` is unchanged and not live-wired.

## Risk

If the returned history is in fact incomplete, weekly spend is undercounted and a run could
exceed the $900 weekly cap. At about $192 spent, the margin is about $708.

## Suggested revisit trigger (a recommendation, not a recorded decision)

Reverse the carve-out, or add a hard stop, if weekly spend reaches a set share of the cap
(for example 50%, about $450) while any slot logged this WARNING in the same week. Pick the
number deliberately; it is not decided.

## Verification

1. After the next live slot, check that the run log shows the WARNING only when the cursor is
   absent, and that no slot placed orders beyond the day's budget.
2. Compare the weekly spend the gate computed with the broker's own order total for the week.
   A mismatch shows the history was incomplete.
