# Edits to the Perplexity 10:00 AM (was 9:55 AM until 2026-11-19) backup prompt (and its siblings)

Written 2026-10-06. Source document, not executable: Perplexity cannot read this
repo, so nothing here takes effect until Geoff pastes it into each Perplexity
backup task (9:55, 10:50, 11:50, 12:50, 1:50, 2:50, 3:50 and any other).

> **Superseded by full prompts:** the corrected, ready-to-paste text of all six
> tasks now lives in `claude/perplexity-prompts/` (guarded by
> `tests/test_perplexity_prompt_mirror.py`). Use those; this file is the change
> rationale.

## Why

On 2026-10-06 the 12:50 pass rejected eleven tickers as "already bought today"
although several were 0.5%-3% cheaper than their earlier fill. The prompt text
that does this is, verbatim:

1. CAPS paragraph: `never duplicate a ticker that already received a buy today`
2. PER-ORDER GATES: `no duplicate same-day order`
3. PER-ORDER GATES: `no earlier same-day buy for that ticker by any runner`

The prompt also has no top-up guard, so there is no path by which a lower-price
repeat can ever be bought. Repo policy (`config/scheduled_tasks.json`
`same_day_top_up_policy`; `config/budgets.json` `one_order_per_ticker_per_day:
false`) allows the repeat at a strictly lower price, sized to the difference.

## Edit 1 - CAPS paragraph

Delete the clause `and never duplicate a ticker that already received a buy
today`. Keep "Count ALL same-day agentic buys ... toward the daily and weekly
totals".

## Edit 2 - PER-ORDER GATES

Replace

`no duplicate same-day order; no earlier same-day buy for that ticker by any runner; one order per ticker per run.`

with

`one order per ticker per run; no buy for a ticker the primary executor (Claude) already bought in THIS SAME slot window; a ticker bought earlier today in a DIFFERENT slot is NOT rejected -- it is sized by the SAME-DAY TOP-UP GUARD below.`

## Edit 3 - add this section after LADDER

Paste the block from `claude/PERPLEXITY-SAME-DAY-TOPUP-GUARD.md` section 2
("SAME-DAY TOP-UP GUARD") in full. It reuses the full-day order pull this prompt
already does for budgeting; follow every pagination cursor to completion and
fail closed (place nothing) if you cannot. The `desired_usd` it refers to is
the LADDER amount ($2 x multiplier, max $10).

Net effect per ticker bought earlier today, at a strictly lower price than its
latest filled price:

- `delta = ladder_amount - already_bought_today_usd`
- `delta >= $1.00` -> buy exactly `delta`
- `0 < delta < $1.00` -> buy nothing now (broker minimum); recomputed next pass
- `delta <= 0`, or price not strictly lower -> buy nothing

## Edit 4 - reporting

Replace the rejection label "Duplicate gate (already bought today)" in the run
report with the guard's own outcomes: `same-window with Claude`,
`price not lower`, `already at ladder amount`, `sub-$1 difference outstanding`,
`reference price unusable (manual review)`.

## Variant: watchdog prompts (10:50 and any slot that delegates to the Mac Studio)

The 10:50 prompt only watches; its BUY PROGRAM SPEC is the payload sent to the
Mac subagent, so the blocking text lives INSIDE that spec and the edits go there
(the cloud part of the prompt needs no change). Offending text, verbatim:

- CAPS: `never duplicate a ticker already bought today`
- PER-ORDER GATES: `no duplicate same-day order`

Apply Edits 1-4 above to the spec, with these differences:

- Its stand-down check is already window-based (buys at or after 10:25 AM ET),
  so the same-window exclusion is already covered by "Claude bought -> exit".
  Only the whole-day ticker rejection needs to go.
- Paste the broker-only guard block from `PERPLEXITY-SAME-DAY-TOPUP-GUARD.md`
  section 2 into the spec rather than telling the Mac agent to run
  `scripts.same_day_top_up_gate`. That script, run without a logged reference
  price, returns $0 / manual review for EVERY top-up (`UNLOGGED_REFERENCE_PRICE`,
  verified 2026-10-06 by replay), so delegating to it without the artifact scans
  from the live-mirror prompts would silently block the same repeats again.
- The spec's own `PER-ORDER GATES` already say "one order per ticker per run";
  keep that.

## Variant: 3:45 PM final backup

Same three blocking phrases, same edits (same-window cutoff is buys at or after
3:25 PM ET). Two final-slot points:

- The repo's 3:30 round-up rule (`minimum_order_aggregate_topup_1530`: a
  remainder of $0.50-$0.99 becomes a $1.00 buy, below $0.50 is forgotten) is
  approved only for Claude's 3:30 task (`10ce362c`). It is NOT in the paste
  block and Perplexity does not apply it. If Claude's 3:30 ran, the 3:45 pass
  stands down anyway; if Claude missed, a sub-$1 difference simply goes unbought
  today. Decide whether the 3:45 backup should mirror the round-up; it is a
  policy choice, not something to add silently.
- Time budget: first poll at 3:45, 5-minute wait, then a paginated full-day
  pull for the guard, all before the 4:00 PM cutoff. If the guard's pull is not
  finished by 3:58, place nothing (the guard already fails closed).

## Do not paste into the repo

The pasted 9:55 prompt contains the literal brokerage account number and a
personal email address. Both stay in Perplexity's task text only; this repo's
secrets invariant forbids them in source, docs, config, or tests. In anything
committed, use `${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}` and
`${DCA_ALERT_EMAIL_ADDRESS}`.

## Open point

`PERPLEXITY-SAME-DAY-TOPUP-GUARD.md` sizes a repeat against the latest FILL
price, which a market order often fills above its decision price, so Perplexity
can top up slightly more often than the repo's own gate would. Accepted
limitation; the repo gate cannot be used here because Perplexity cannot run it.
