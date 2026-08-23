# Same-day top-up guard, self-contained for the Perplexity executor

Written 2026-07-31. **This file is a source document, not an executable path.**
Perplexity runs inside its own app and cannot read this repo, so nothing here
takes effect until the text in §2 is pasted into the Perplexity task prompts
themselves.

## 1. Why this exists

`config/scheduled_tasks.json` was amended 2026-07-31 to require every
connector-native run to call `python3 -m scripts.same_day_top_up_gate`. That
amendment cannot bind Perplexity: it has no filesystem access to this repo, so
it can neither read the manifest nor execute the script.

Evidence that it does not read or write here: on 2026-07-31 it placed 28 real
orders and wrote **zero** artifacts and **zero** `run_state.json` files under
this repo.

The guard therefore has to be restated as broker-only arithmetic that Perplexity
can perform with the two calls it already makes (`get_equity_orders`,
`place_equity_order`). The policy is unchanged. Only its expression changes.

Policy source of truth remains `config/scheduled_tasks.json` →
`same_day_top_up_policy`. If the two ever disagree, that file wins and this one
is stale.

## 2. Paste-ready block for the Perplexity task prompts

> ### SAME-DAY TOP-UP GUARD (run before placing anything)
>
> Buying the same ticker more than once in a day is ALLOWED. What is not allowed
> is a repeat sized as though nothing had been bought yet. Repeats must be sized
> down to the incremental difference and only taken at a strictly lower price.
>
> **Step 1 — Read today's fills.** Call `get_equity_orders` for the account with
> `created_at_gte` set to today's date, and follow EVERY pagination cursor to
> completion. If you cannot reach the final page, place NOTHING this pass and
> report why. Do not assume an empty history.
>
> **Step 2 — Build the prior picture, per ticker.** From those orders, counting
> only buys, and counting an order whether it filled, is pending, was cancelled,
> or was rejected:
> - `prior_usd[ticker]` = sum of the dollar amounts already submitted today.
> - `prior_price[ticker]` = the price of the most recent of those buys, using
>   `average_price` when the order filled, otherwise its submitted `price`.
>
> **Step 3 — Size each candidate.** For each ticker you intend to buy, with
> `desired_usd` from the ladder and `current_price` your decision price now:
> - If `prior_usd` is 0 → this is the first allocation. Buy the full
>   `desired_usd`.
> - Else if `current_price` is NOT strictly less than `prior_price` → buy
>   **$0.00**. Skip it. Equal prices do not qualify.
> - Else if you have no usable `prior_price` or no `current_price` → buy
>   **$0.00** and flag the ticker for manual review. A missing price never
>   authorizes a buy.
> - Else → buy `max(0, desired_usd - prior_usd)`. If that is $0.00 or less, skip
>   the ticker.
>
> Never round a computed amount up. Never restore a $0.00 back to the desired
> amount. Never place more than the computed amount.
>
> **Step 4 — Deduplicate this batch by ticker.** At most one order per ticker per
> run, even when the same ticker qualifies more than once in one pass. Step 2
> reads the broker BEFORE the batch, so two orders placed seconds apart within
> one batch would both see the same prior picture and both pass. Only in-batch
> dedup closes that. On 2026-07-31 this exact race bought AAAU twice and SPCX
> twice, 70 seconds apart, inside a single burst.
>
> **Step 5 — Fail closed.** Incomplete pagination, an unreadable order history,
> or any uncertainty about what was already bought today means: place NOTHING
> this pass and report. There is deliberately no path that falls back to the full
> desired amount on an unproven-empty history. Proceeding as though the slate
> were clean is the precise failure that produced repeated full-size buys on
> 2026-07-30 and 2026-07-31.

## 3. Worked example, from the real 2026-07-31 tape

DXYZ, as it actually happened versus as the guard would have sized it.

| Time (ET) | Event | Actual | Guarded |
|---|---|---|---|
| 09:36:46 | first buy, filled @ $23.2499 | $1.00 | $1.00 (first allocation) |
| 10:13:35 | second buy, filled @ $22.3507 | $2.00 | **$1.00** |

The second buy qualified: $22.3507 is strictly below $23.2499, so a top-up was
permitted. It was the SIZE that was wrong. Desired $2.00 minus $1.00 already
bought equals $1.00, not another full $2.00.

Contrast AAAU, which should not have repeated at all:

| Time (ET) | Event | Actual | Guarded |
|---|---|---|---|
| 11:40:32 | first buy, filled @ $39.8599 | $1.50 | $1.50 (first allocation) |
| 11:41:42 | second buy, filled @ $39.8381 | $1.50 | **$0.00** |

Seventy seconds apart, inside one batch. Step 4 stops this one, not Step 3:
$39.8381 IS strictly below $39.8599, so the price test passes. Only the
one-order-per-ticker-per-run rule catches it.

Replaying the full policy over both days: 2026-07-30 would have been $17.00
across 7 orders instead of $22.50 across 12; 2026-07-31 would have been $36.00
across 23 orders instead of $50.00 across 28.

## 4. What this does NOT solve

**Mutual exclusion between Claude and Perplexity.** The two share no writable
medium: Perplexity cannot reach this repo, and Claude cannot reach Perplexity's
app. A claim file, lock, or handoff signal is therefore not constructible
between them. The guard above does not prevent both executors from running the
same slot; it makes it non-catastrophic when they do, by ensuring the second
one sizes against what the first already bought.

Genuine first-come-first-serve would need one medium both sides can write and
read. Candidates worth checking, in rough order of likelihood:
- Slack. Claude can post (verified 2026-07-28, channel `C0BKS1ZL5UY`). Whether
  Perplexity can post and read there is unconfirmed.
- Notion. Both may have connectors; unconfirmed on the Perplexity side.
- Any HTTP endpoint both can call.

Until one is confirmed, concurrent execution is a fact to be survived rather
than a race to be won.

**Trigger-on-failure handoff.** For the same reason, Claude cannot signal
Perplexity that it aborted. Note also that if such a signal is ever built, the
trigger condition must be *"aborted before producing a buy list"* and NOT
*"placed zero orders."* A quiet tape where the strategy correctly declines to
buy would otherwise hand off to a runner that buys anyway, inverting the
strategy on exactly the days it said stay out.

## 5. Open items for a human

1. Paste §2 into all five Perplexity task prompts. Nothing in this repo can do
   it. This is the only item that stops the duplicate buying.
2. Confirm which medium, if any, both executors can read and write, so §4 can be
   revisited.
3. `config/budgets.json` `order_rules.one_order_per_ticker_per_day` was set to
   `false` on 2026-07-31 so resized top-ups are permitted. The guard above
   assumes `false`. If it is set back to `true`, Step 3's top-up branch must be
   replaced by an outright skip of any ticker already bought today.
