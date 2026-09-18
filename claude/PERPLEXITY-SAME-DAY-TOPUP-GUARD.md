# Same-day top-up guard, self-contained for the Perplexity executor

Written 2026-07-31. **This file is a source document, not an executable path.**
Perplexity runs inside its own app and cannot read this repo, so nothing here
takes effect until the text in §2 is pasted into the Perplexity task prompts
themselves.

> **Confirmed by Geoff, 2026-08-01: Perplexity is currently live, not retired.**
> While preparing this commit, a contradiction turned up: a same-evening commit
> (PR #293, merged 2026-07-31 22:56 ET) records "Geoff confirmed the
> seven-slot/Claude-sole-executor decision is real (Perplexity was turned off
> directly...)", but `docs/safeguards.md`, `config/scheduled_tasks.json`
> (`"status": "active"` on all five legacy tasks), and a 2026-07-31 17:50 ET
> Slack post all said the opposite. Asked directly, Geoff confirmed: he turned
> Perplexity back on because the Claude seven-slot roster's own tasks were
> failing (Gate 5 hard-aborts every fire, so
> that roster places nothing; Perplexity was the only executor actually
> trading). He intends to turn it off again once the Claude side is functioning
> reliably, but that has not happened yet as of this commit. So the PR #293
> commit message described a decision that had already been reversed by the
> time this file was written, not current fact — **§5 item 1 below is live, not
> moot: paste §2 into Perplexity's five task prompts. That is still the one
> thing this repo cannot do for him, and it is still what stops the duplicate
> buying.** Separately, §5 item 4 flags that this decision sits in unresolved
> tension with `config/scheduled_tasks.json`'s own `connector_native_execution_policy`
> text — worth reading before treating this document as fully settling the
> question of whether Perplexity should be trading at all, as opposed to how
> dangerous its trading is while it continues.

## 1. Why this exists

The seven Claude executor prompts (mirrored under
`claude/dca-executor-prompts/live-mirror/`) were amended 2026-07-31 so that
every one of them calls `python3 -m scripts.same_day_top_up_gate` as Gate 6.5.
That amendment cannot bind Perplexity: it has no filesystem access to this
repo, so it can neither read the manifest nor execute the script.

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
> report why. Do not assume an empty history. If ANY fetched row lacks a usable
> order ID or ticker symbol, you cannot deduplicate or attribute its exposure —
> treat the entire pass as unreadable (go to Step 5), do not silently drop or
> ignore the row. Deduplicate the remaining orders by order ID before Step 2 —
> a page-boundary repeat, or a page fetched twice, is not separate exposure and
> must not be summed twice.
>
> **Step 2 — Build the prior picture, per ticker.** Look at every order that is
> NOT an explicit sell. Treat a missing, unrecognized, or otherwise ambiguous
> `side` as a POSSIBLE buy and include it — never discard a row for being
> ambiguous. The dangerous mistake here is undercounting: a real prior buy
> silently dropped for having an unclear `side` is exactly the duplicate-buy
> failure this guard exists to prevent. Only skip a row whose `side` is
> unambiguously `sell`.
>
> Each remaining order's contribution depends on its state:
> - **Filled** → counts its *executed* dollar amount (filled quantity × average
>   fill price), and that average fill price is a candidate reference price.
> - **Pending / still working** → counts its full submitted dollar amount,
>   conservatively, since it may still fill for that amount — but contributes
>   NO usable reference price; its eventual fill price is not yet known.
> - **Cancelled, rejected, failed, voided, or expired, with ZERO fill** →
>   counts **$0**. An order that never executed is not prior exposure, no
>   matter what dollar amount was originally submitted.
> - **Cancelled/rejected/etc. WITH a partial fill before it stopped** → counts
>   only the executed portion that actually filled, never the original
>   requested size — and contributes NO usable reference price, even though it
>   has an `average_price` for that partial fill. A fill that was later
>   cancelled or rejected was never CONFIRMED at that price for the order as a
>   whole; treat it exactly like a pending order for pricing purposes, even
>   though (unlike a pending order) its dollars already count above.
> - **Anything else** — a `state` string that is not "filled", not one of the
>   still-open states (e.g. `new`/`queued`/`confirmed`/`unconfirmed`/
>   `partially_filled`), and not one of the terminal states above — treat the
>   ENTIRE PASS as unreadable (go to Step 5). Do not guess this row's exposure
>   and do not silently exclude it: an order under a state this list doesn't
>   name could be an outstanding real buy, and guessing wrong here is exactly
>   the silent gap Step 5 exists to close.
>
> From those contributions, per ticker:
> - `prior_usd[ticker]` = sum of every contribution above.
> - `prior_price[ticker]` — a zero-fill terminal order (it counts $0 above and
>   carries no price) does not affect `prior_price` at all; skip it here
>   exactly as its exposure was skipped above. ONLY a fully **Filled** order
>   can ever supply `prior_price` — a **Pending** order and a terminal order
>   with a **partial fill** both count toward `prior_usd` but NEVER toward
>   `prior_price` (see their bullets above). So: if EVERY remaining
>   contributing order is Filled with a confirmed price and a usable
>   `created_at` timestamp, `prior_price` is the price of whichever one has
>   the latest `created_at` — that field specifically, not `last_transaction_at`
>   or any other timestamp the broker row carries; the two can order a delayed
>   fill differently. If even ONE remaining contributing order is Pending or a
>   partial-fill terminal, OR a Filled order has no usable `created_at`,
>   `prior_price` is unusable — no matter how much older or newer that order
>   is than the others. Do not fall back to a submitted, limit, or
>   partial-fill price as a stand-in reference.
>
> **Step 3 — Size each candidate.** Immediately before this step — not
> earlier, and not reused from whatever quote originally justified the
> candidate — re-fetch each candidate's current quote. `current_price` below
> means THIS freshly-refetched price, nothing older. The market can move
> while Steps 1-2's paginated broker-history read runs; a ticker that has
> since risen above its qualifying reference must never receive a buy
> authorized on a stale, earlier quote. For each ticker you intend to buy,
> with `desired_usd` from the ladder and this fresh `current_price`:
> - If `current_price` is missing, non-numeric, or not strictly positive → buy
>   **$0.00** and flag the ticker for manual review, EVEN IF this would
>   otherwise be a first allocation. A missing price never authorizes a buy,
>   including the very first one for that ticker today.
> - Else if `prior_usd` is 0 → this is the first allocation. Buy the full
>   `desired_usd`.
> - Else if `prior_price` is unusable (see Step 2) → buy **$0.00** and flag the
>   ticker for manual review. A missing or unusable reference price never
>   authorizes a buy — check this BEFORE the next bullet, since there is
>   nothing numeric yet to compare `current_price` against.
> - Else if `current_price` is NOT strictly less than `prior_price` → buy
>   **$0.00**. Skip it. Equal prices do not qualify.
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

**Known limitation in Step 2, inherited from `scripts/same_day_top_up_gate.py`
itself, not introduced here:** `config/scheduled_tasks.json` documents the
reference-price precedence as logged `target_price`, then `decision_price`,
and only then `average_fill_price`. Broker-only data — all either Perplexity
or the repo's own gate script can see — carries none of the first two, only
what actually filled. The repo's gate script accepted this as a deliberate,
documented trade-off (see that module's docstring, "KNOWN LIMITATION") rather
than reintroducing the artifact reads whose absence it exists to tolerate.
Step 2 above makes the same choice for the same reason: there is no broker-only
way to close this gap, so it inherits the limitation rather than hiding it.

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
same slot. When one clearly runs after the other — its Step 1 read happens
once the first executor's order is already visible in broker history — it
makes that case non-catastrophic, sizing the later one against what the first
already bought. **It does NOT cover a genuinely simultaneous start.** If both
executors complete Step 1 before either has placed anything, both see the same
empty prior picture and both can submit full size — the identical failure Step
4 describes for two orders inside one executor's own batch, just at the
cross-executor level, where nothing in this guard can dedupe it. Closing that
needs the same shared medium the rest of this section is about, which does not
yet exist.

Genuine first-come-first-serve would need one medium both sides can write and
read. Candidates worth checking, in rough order of likelihood:
- Slack. Claude can post (verified 2026-07-28, channel `C0BKS1ZL5UY`). Whether
  Perplexity can post and read there is unconfirmed — though worth noting: the
  2026-07-31 `#dca-alerts`/DM history shows posts attributed to a sender named
  `Computer`, distinct from the ones attributed to `Claude`, e.g. a 10:30 AM
  "account access" block message. Whether `Computer` is Perplexity's own
  posting identity, some other agent, or something else is unconfirmed and
  worth checking before ruling Slack out.
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

**Reviewed 2026-09-18 (resolution: scope-narrowing, not a sharper trigger).**
This gap was reviewed against a concrete incident (9/16: a flat $1 duplicate
top-up on IBM already filled correctly at $4 ladder size). No sharper trigger
condition is constructible today: Perplexity has no filesystem access to this
repo, so it cannot read `run_state.json`'s `is_trading_failure` /
`run_classification_outcome` / `completion_reason` fields, which are exactly
the fields that already distinguish a genuine failure from a deliberate
no-signal decline in this repo's own schema (`schemas/run_state.schema.json`)
— and no broker-visible signal (order tag, note field, watchlist flag) stands
in for them. The resolution chosen instead, recorded in
`config/scheduled_tasks.json`'s `standing_perplexity_backup_policy.mechanism`:
Perplexity's backup role only ever authorizes it to run its **own** independent
signal/ladder/safety evaluation for the slot (already true per §2/§4's
top-up sizing steps above), never to treat Claude's absence as license to
buy purely on Perplexity's own discretion beyond that. This is a documented,
accepted residual risk, not a solved problem — matching the pattern this
section already uses for the "simultaneous start" gap above. Item 2 below
("confirm which medium, if any, both executors can read and write") remains
the actual precondition for ever building a real distinguishing trigger.

## 5. Open items for a human

1. **Paste §2 into all five Perplexity task prompts.** Confirmed live by Geoff
   2026-08-01 (see the note at the top of this file) — Perplexity is currently
   the account's active executor and will stay that way until the Claude
   seven-slot roster is functioning reliably. Nothing in this repo can paste
   the text for him. This is the only item that stops the duplicate buying.
   When Perplexity is eventually retired for real, `docs/safeguards.md`'s
   "Live execution roster" section and `config/scheduled_tasks.json`'s
   `"status": "active"` entries for 2ba19526/42173154/7be82b97/a6239dae/10ce362c
   will need updating to match — they are accurate today, not stale, but will
   become stale the day the switch actually flips.
2. Confirm which medium, if any, both executors can read and write, so §4 can be
   revisited — including running down who/what the `Computer` Slack sender is
   in the workspace.
3. **Correction, 2026-08-02 (superseding the 2026-08-01 "correction" that used
   to be here).** The 2026-08-01 text claimed `config/budgets.json`'s
   `order_rules.one_order_per_ticker_per_day` was `true` and had never been
   toggled. That claim does not hold: the field is `false` on disk right now,
   with an explicit note recording it as permanent policy. More importantly,
   **`dca_runner.py`'s own `assess()` reads this exact flag directly from
   `config/budgets.json`** (`ONE_ORDER_PER_TICKER_PER_DAY =
   _load_one_order_per_ticker_per_day()`, evaluated at import time, no
   override) — so, unlike `near_close_dca.StrategyConfig` (which deliberately
   does NOT overlay the JSON, for the separate reason described in
   `dca_config.py`), `dca_runner.assess()`'s behavior tracks this file's
   literal value directly. With the file at `false`, `assess()` does not
   exclude a same-day-bought ticker at all.
   **Geoff has now ruled on the KNOWN CONFLICT the 1230ET prompt used to flag:**
   buys on the same ticker are allowed more than once a day, never twice in the
   same slot/session. There is no more disagreement between `assess()` and
   `same_day_top_up_policy` to reconcile — both permit the resized repeat, and
   the 1230ET prompt's step 4.5 (`scripts/same_day_top_up_gate.py`) is simply
   the authoritative sizer. §2 above already only ever implemented the
   top-up/resize side, which is now correct rather than partial: it needed no
   change for this ruling.
4. **Added 2026-08-01 (Codex review, PR #299): reconcile
   `connector_native_execution_policy`'s blanket text with keeping Perplexity
   live.** That policy's `removal_reason` (`config/scheduled_tasks.json`) says,
   without qualification, that no executor should place real orders on any of
   these five cron ids absent a reviewed repository entry point. Perplexity
   never has one — not a temporary gap, a structural fact (§1). Read literally,
   the policy's own text says Perplexity's trading, guarded by §2 or not, is
   exactly the outcome it exists to prevent. This document does not resolve
   that tension; it takes Geoff's 2026-08-01 decision to keep Perplexity live
   (the note at the top of this file) as a given, made with full knowledge that
   the Claude alternative isn't working yet, and only tries to make trading
   that is ALREADY HAPPENING less dangerous while it continues. A broker-only
   guard is not a reviewed repository entry point, and does not turn
   Perplexity's operation into the in-repo signal generation the policy was
   written to require — §2 constrains an existing decision, it does not
   authorize one. If Geoff wants the manifest's tombstone text to actually
   reflect current reality (Perplexity kept live, deliberately, with this
   guard as the accepted interim mitigation) rather than silently contradicting
   it, that needs an explicit edit to `connector_native_execution_policy`
   itself — not something this document should do unilaterally to a policy
   block it does not own.
