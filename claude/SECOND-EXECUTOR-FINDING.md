# The connector-native backup is what trades, and it is not applying the top-up guard

Written 2026-07-31 after the close. **Revised** the same evening: the first
draft treated the order flow as an unidentified second executor. It is not
unidentified. It is the intended connector-native backup on the five legacy cron
ids, and it is documented in `config/scheduled_tasks.json` →
`connector_native_execution_policy`. Findings only; no code or schedule change
was made in response, and nothing here was committed.

## What it is

`connector_native_execution_policy` (added 2026-07-27) applies to cron ids
`2ba19526`, `42173154`, `a6239dae`, `7be82b97`, `10ce362c` and says:

> "A missing repo runner script, a missing entry point, or a missing internal
> order-submission implementation is NOT a blocking condition and NOT a reason
> to escalate. It is the TRIGGER for connector-native execution."

That is exactly the condition the seven Claude slots hit at Gate 5 every day, so
the backup firing is by design, not a rogue process. The read-only monitor is
not involved: a read-only task places no orders, and every one of the 28 orders
on 2026-07-31 carries `placed_agent='agentic'` with a real fill.

**Not a security finding. Retracted from the first draft: the "surviving
Perplexity cron" and "separate Claude account" hypotheses, and the claim that
the cutover silently failed.**

## What is still wrong

The policy binds the backup to the same rules as a runner-driven run. Its own
`required_steps`:

> "Evaluate the first-allocation and ticker-scoped same-day top-up rules per
> ticker."

and its `safety_note`:

> "It does not relax any budget cap, circuit breaker, whitelist, ordering,
> spread, confidence, or top-up rule; a connector-native run passes exactly the
> same gates as a runner-driven one."

On 2026-07-31 it did not. Evidence:

* **SPCX bought three times** — $2.00 at 11:40:32, $2.00 at 11:41:42, $1.00 at
  14:34:31. $5.00 total.
* **AAAU bought twice** — $1.50 at 11:40:32 and $1.50 at 11:41:42, **70 seconds
  apart, inside the same burst.** That is not a cross-slot top-up the policy
  contemplates; it is a duplicate within one run, which even the older
  `one_order_per_ticker_per_stream` rule forbids.
* NFLX, DFTX, IBM, DXYZ and AAPL each bought twice across bursts, at full size
  rather than at the incremental delta.

The backup was obliged to apply the guard and had no tool to apply it with.
`scripts/same_day_top_up_gate.py` is now that tool.

## Where the guard needed to go, and what was done about it

The guard was first wired only into the seven Claude slots as Gate 6.5 (step 4.5
on the 1230 slot). Those slots place nothing today, so that alone constrained
nothing. It belongs in the connector-native path, which is the executor actually
trading.

**Done 2026-07-31:** `config/scheduled_tasks.json` now names the tool in the
contract the connector-native path runs from:

* `connector_native_execution_policy.required_steps` — the "evaluate the
  first-allocation and ticker-scoped same-day top-up rules" step now says to run
  `python3 -m scripts.same_day_top_up_gate` rather than evaluate by hand, treats
  `buy_usd` as a ceiling, and adds an explicit FAIL CLOSED step and an in-batch
  dedup step.
* `intraday_connector_native_recovery_policy.required_steps` — same two additions.
* New top-level section `same_day_top_up_guard_enforcement_2026_07_31` records
  the incident, the root cause, what is bound by the guard, and what was
  deliberately *not* changed.

`scheduled_tasks.json` line 216 states that for the `no_python_entry_point=true`
streams "an external scheduler/agent runs them directly from this manifest," so
amending the manifest is the designed lever. **Unverified from here:** whether
the external agent re-reads the manifest on each run, or whether the five task
texts also carry their own frozen copy that must be updated separately. Worth
confirming before trusting tomorrow's runs.

The policy itself was not touched. Repeat same-day buys remain permitted.

## Why Claude's Gate 4 aborts every day: a scheduler-jitter race

A backup that fires *because* Claude failed cannot fire *before* Claude runs.
On 2026-07-31 it did, on at least two slots.

Claude's scheduler applies a deterministic per-task delay. Observed values from
`list_scheduled_tasks`:

| Slot | Nominal | Jitter | Actual fire |
| --- | --- | --- | --- |
| 0935 | 09:35 | 494 s | 09:43:14 |
| 1030 | 10:30 | 594 s | 10:39:54 |
| 1130 | 11:30 | 413 s | 11:36:53 |
| 1230 | 12:30 | 192 s | 12:33:12 |
| 1330 | 13:30 | 46 s | 13:30:46 |
| 1430 | 14:30 | 313 s | 14:35:13 |
| 1530 | 15:30 | 186 s | 15:33:06 |

Against the backup's actual bursts:

| Burst (ET) | Claude slot fired | Who was first |
| --- | --- | --- |
| 09:36:36 | 09:43:14 | **backup, by 6m38s** |
| 10:13:35 | 10:39:54 | **backup, by 26m19s** |
| 11:40:31 | 11:36:53 | Claude, by 3m38s |
| 14:34:30 | 14:35:13 | **backup, by 43s** |

Compare 2026-07-30, when the relationship still held the right way round: the
0935 Claude slot ran at 09:43, found Gate 4 clear with zero orders on the day,
aborted at Gate 5 — and the backup placed its first orders at 09:58, **15
minutes later.** Correct fallback behavior.

**Hypothesis (not confirmed — the backup's schedule is not visible from here):**
the backup is timed off Claude's *nominal* slot time, but Claude fires up to
9m54s late because of jitter. On the 9:35 slot the jitter is 8m14s, so the
backup usually wins the race. The result inverts the design: the backup
pre-empts, Claude's Gate 4 correctly refuses a window that already has orders in
it, and that refusal then reads downstream as "Claude failed," which is the very
condition the backup exists to cover.

If that is right, the fix is a timing one — have the backup wait on Claude's
*observed* completion (or its alert file) rather than a clock offset from the
nominal time — not a change to any gate. Gate 4 is behaving correctly and should
not be relaxed to make room for the backup.

## Separately: the 1530 slot fired 3h50m late

The 3:30 slot fired at 19:43 ET and Gate 1 refused it as a scheduler fault. That
is correct behavior and an unrelated problem worth chasing on its own.

## What needs a human

1. **Confirm the five legacy tasks actually pick up the amended manifest.** The
   repo-side contract now mandates the guard and in-batch dedup. If those task
   texts carry their own frozen copy of the steps, they need the same edit
   applied where they live. This is the one that stops the duplicate buying, and
   it is the only item that can regress money tomorrow.
2. **Fix the ordering race** so the backup follows Claude rather than pre-empting
   it, or accept that Claude's slots are now vestigial and retire them. Do not
   relax Gate 4 to make room for the backup; Gate 4 is behaving correctly.
3. **The 1530 slot's 3h50m late fire.**
4. `budgets.json`'s `order_rules.one_order_per_ticker_per_day: true` still
   contradicts `same_day_top_up_policy`. Unresolved; see the 1230 prompt's
   KNOWN CONFLICT note and
   `same_day_top_up_guard_enforcement_2026_07_31.known_open_conflict`.
5. **Delete the FUSE orphans.** `.fuse_hidden*` files hold pre-redaction copies
   of the executor prompts, including the literal account number. They are now
   gitignored so `git add .` cannot stage them, but an ignored file is still
   readable on disk. The sandbox cannot unlink them:
   `rm -f claude/.fuse_hidden* claude/dca-executor-prompts/.fuse_hidden* claude/dca-executor-prompts/live-mirror/.fuse_hidden* claude/dca-executor-prompts/live-mirror/.template.txt scripts/.fuse_hidden* scripts/tests/.fuse_hidden*`
   Separately, `git log -S "$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"` shows the literal
   in older commits on this branch, predating this session.
