# Something other than the seven Claude slots is trading account ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}

Written 2026-07-31, ~19:45 ET, after the close. Findings only — no code or
schedule change was made in response. Nothing here was committed.

## The short version

The seven scheduled Claude executor slots placed **zero** orders on 2026-07-31.
Every one of them aborted. Meanwhile **28 orders totalling $50.00** were placed
on the account, all `placed_agent='agentic'`, in four bursts that do not line up
with any of the seven slots' firing times.

Gate 4 caught this. It has been firing the correct alert — *"executor-placed
orders already present in the … window, a second executor may be live"* — on
most slots, on most days, since at least 2026-07-29. The alert is right. The
second executor is real.

## What the seven slots actually did on 2026-07-31

Read from `artifacts/alerts/`:

| Slot | Fired (ET) | Outcome | Gate | Reason |
|---|---|---|---|---|
| 0935 | 09:43:55 | ABORT | 4 | 7 agentic orders already in the 9:35 window |
| 1030 | ~10:40 | ABORT | 5 | "executor lacks access to market data providers" |
| 1130 | 11:37:29 | ABORT | 4 | 15 competing agentic orders in the 11:25-11:55 window |
| 1230 | — | (placement OFF by design) | — | — |
| 1330 | 13:31 | ABORT | 5 | no callable signal entry point; `no_python_entry_point=true` |
| 1430 | 14:35:59 | ABORT | 4 | AAPL $5.00 + SPCX $1.00 already in the 2:30 window |
| 1530 | 19:43 | ABORT | 1 | fired **3h50m past** the 15:25-15:55 window |

Two separate failure modes, both behaving correctly:

* **Gate 5** aborts because the seven-slot signal entry point does not exist.
  `run_dca.SLOT_MAP` contains only `1030ET`, `1230ET`, `1530ET`; `0935ET` is in
  `DISABLED_SLOTS` and raises `UnsupportedSlotError`. This is exactly what
  `GATE5-PROPOSAL.md` predicted. The prompts abort rather than improvise a buy
  list — the correct behavior, and worth noting explicitly: **no executor
  invented trades.**
* **Gate 4** aborts because someone else already traded the window.

The 1530 slot firing 3h50m late is a third, separate problem: a real scheduler
fault, correctly refused by Gate 1.

## What actually traded

Four bursts, none of which corresponds to a Claude slot's fire time:

| Burst (ET) | Orders | Notional | Nearest Claude slot |
|---|---|---|---|
| 09:36:36-09:36:48 | 7 | $12.00 | 0935 fired 09:43 — **7 min later** |
| 10:13:35-10:13:36 | 4 | $7.00 | 1030 fired ~10:40 — 27 min later |
| 11:40:31-11:41:42 | 15 | $25.00 | 1130 fired 11:37 — 3 min *earlier* |
| 14:34:30-14:34:31 | 2 | $6.00 | 1430 fired 14:36 — 2 min later |

The bursts are sub-second batches of sequential market buys — machine-placed,
not hand-typed. The pattern (a 09:36 open batch, a mid-morning batch, a large
late-morning batch, a small afternoon batch) matches the *legacy* three-stream
schedule far better than the new seven-slot roster.

`list_scheduled_tasks` returns exactly seven tasks, all of them the Claude
roster. So whatever is placing these orders **is not registered in this
scheduler**. Candidates, in rough order of likelihood:

1. The five legacy tasks (`2ba19526`, `7be82b97`, `ce16ba6b`, `10ce362c`, and
   the midday one) still running under an external scheduler on the Mac.
   `claude/dca-executor-prompts/0-setup-card.md` flagged the overlapping
   old/new task window as a cutover risk. The cutover appears never to have
   completed: the new tasks were added and the old ones were not retired.
2. A separate Claude session/account with its own scheduled tasks against the
   same Robinhood connector.
3. A surviving Perplexity-era cron.

## Why this matters for the same-day top-up guard

The guard added on 2026-07-31 (`scripts/same_day_top_up_gate.py`, wired in as
Gate 6.5 / step 4.5) is correct and tested, **and it will not constrain
anything until the thing that is actually trading runs it.** It is installed on
seven executors that place no orders.

That is not an argument against the guard — Gate 6.5 is required before the
seven slots are ever allowed to place — but it means the repeat-buy problem is
not fixed by it today.

## What the policy would have done to today's 28 orders

Replaying `same_day_top_up_policy` over the real fills, each order seeing only
what preceded it:

**$50.00 across 28 orders becomes $36.00 across 23.** Five orders blocked,
$14.00:

| ET | Ticker | Placed | Would be | Reason |
|---|---|---|---|---|
| 10:13:36 | NFLX | $1.50 | $0.00 | already met or overfilled |
| 11:41:42 | SPCX | $2.00 | $0.00 | already met or overfilled |
| 11:41:42 | AAAU | $1.50 | $0.00 | already met or overfilled |
| 14:34:30 | AAPL | $5.00 | $0.00 | price not below prior (302.12 vs 302.09) |
| 14:34:31 | SPCX | $1.00 | $0.00 | already met or overfilled |

Three repeats would have been **permitted and resized**, which is the policy
working as intended rather than a suppression:

| ET | Ticker | Placed | Would be | Prior | Ref → price |
|---|---|---|---|---|---|
| 10:13:35 | DXYZ | $2.00 | **$1.00** | $1.00 | 23.2499 → 22.3507 |
| 10:13:35 | IBM | $2.00 | **$1.00** | $1.00 | 219.9999 → 218.1899 |
| 10:13:36 | DFTX | $1.50 | **$0.50** | $1.00 | 44.4499 → 43.1099 |

Two details worth flagging on their own:

* **SPCX was bought three times today** ($2.00, $2.00, $1.00 = $5.00) and
  **AAAU twice** ($1.50, $1.50).
* The second SPCX and second AAAU orders came **70 seconds after the first**, in
  what looks like the same run. That is not a cross-slot top-up the policy is
  designed for; it is a duplicate inside one batch, which even the old
  `one_order_per_ticker_per_stream` rule should have caught. Whatever placed
  them is not applying that rule either.

## What needs a human

1. **Identify and stop the second executor.** Until then, Gate 4 will keep
   aborting the Claude slots every day, and the account keeps trading
   unguarded. This is the top item.
2. **Decide whether the seven-slot roster should be enabled at all** before
   Gate 5 has a real entry point. Today it is a no-op that alerts.
3. **Fix the 1530 slot's 3h50m late fire** — Gate 1 caught it, but a scheduler
   that late is its own fault to chase.
4. `budgets.json`'s `order_rules.one_order_per_ticker_per_day: true` still
   contradicts `same_day_top_up_policy`. Unresolved; see the 1230 prompt's
   KNOWN CONFLICT note.
