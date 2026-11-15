# Handoff: scripted primary gates (2026-11-13 to 2026-11-15)

One-time handoff, not a standing reference. For the gate commands themselves read the docstring of
`scripts/primary_gates.py`; for the live prompts read `claude/dca-executor-prompts/live-mirror/`.

## What happened

On 2026-11-13 the 11:30 ET primary started at 11:39:25, spent about 5 minutes on gates, placed its first order (ANET)
at about 11:44:49, then took about 2 minutes per order. The script-driven Perplexity backup placed about 12 orders a
minute, reached the same tickers first, and bought CAT twice.

## What changed (operator decisions, 2026-11-13 and 2026-11-15)

| Change | Where |
| --- | --- |
| Gates 0, 1, 3 and the account pin run as one command (`clock`) | `scripts/primary_gates.py`, all seven live prompts |
| Gate 6 cap arithmetic runs as one command (`budget`, with `--tier`) | same |
| Gate 4 classification runs as one command (`gate4`); the prose rules stay as the manual fallback | six standard-layout prompts (12:30 keeps its own gate script) |
| Independent per-order reads go out in one tool-call turn (`PARALLEL PER-ORDER READS`) | Gate 7 of every prompt |
| Account number comes from `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`, never the command line | `scripts/account_arg.py` |

Unchanged on purpose: per-order revalidation, the per-order record and its audit
(`scripts/check_per_order_record.py`).

## State when written

- Merged: the scripted clock and budget, the parallel-reads rule, the rollout to all slots, `--tier`, account from
  the environment, and three macOS test-timing fixes.
- Open: PR #2335 (scripted Gate 4), CI green on `096529e`, waiting on the operator's merge decision. Merging it
  now or after Monday's live runs is the operator's call.
- Not verified: nothing here has run in a live session. The full local suite was never completed. The macOS
  timing fixes are a measured guess (not reproduced on a Mac); one green CI run is not proof.

## To do

1. After merges, check whether a `planner-repaste` issue was filed, so the Perplexity backup tasks get fresh
   prompts (the planner hash is pinned in those prompts).
2. Monday 2026-11-16: compare the 9:35 and 11:30 first-order times with 11:44:49 from 2026-11-13.
3. Check `account_ok` is true in the `clock` output and that the order-history scripts find the account in the
   environment.
4. Read the Gate 4 verdicts for a false ABORT or a wrong COMPATIBLE_CONTINUE. Gate 4 rules: `placed_agent` in
   `user`, `recurring`, `drip` is the owner and never counts; exactly `agentic` is the compatible executor; anything
   else is rogue and aborts, even beside a compatible order.

## Working notes

- Other sessions have pushed to `badmade/awesome-galileo-vsoun0` before. Fetch before pushing; do not force-push.
- Stage new files before running `scripts/local_ci.sh --registration`: the secrets guard only sees tracked files.
- Never write an account-shaped literal in source, docs or tests; the only allowed synthetic value is `999000111`.
