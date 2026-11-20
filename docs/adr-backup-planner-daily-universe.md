# ADR: How a daily-changing universe reaches the Perplexity backup planner

- **Status:** Proposed (2026-11-20). Nothing in this ADR is implemented. It needs operator acceptance first.
- **Why now:** `docs/adr-program-maintained-universe.md` lets the program change the buy list every trading day.
  The backup planner was built for a list that changes by hand, rarely. This ADR is the open question 9 of that
  ADR (item "Mac checkout and the backup planner").
- **Scope:** only how the backup path learns the day's list. The primary Claude slots read the Mac checkout and
  are not affected.

## Context (verified in the repo on 2026-11-20)

- `backup_planner.pyz` BUNDLES `config/tickers.json` (`scripts/build_backup_planner.py`, its file list).
- Plan validation compares the plan's `universe_hash` with `dop.universe_hash(config.tickers)`, where `config` is
  the planner's OWN bundled config (`scripts/backup_planner.py`, `dca_order_plan.py`). A mismatch is
  `universe_hash_mismatch`, and any defect rejects the whole plan. It also requires each symbol to be in the
  active universe and not excluded, again from the bundled config.
- The planner's own hash is stamped in each of the six prompts. A changed bundled file means a rebuilt planner
  and re-pasted prompts, by hand, in Perplexity.
- Nothing keeps the Mac checkout in sync with `main` (the operator pulls by hand).

Consequence: with a list that changes daily, the bundled list is stale almost every day. Mac-built plans carry
the live list's hash and are refused (fail closed, no backup that day). A plan the planner builds itself would
use the stale bundled list and could buy a name that has left the top 100.

## Options

| # | Option | Effect |
| --- | --- | --- |
| 1 | The plan carries the day's list, and the planner checks it against a hash published for that day | Backups follow the live list. The planner no longer knows the list on its own, so a trust anchor is needed (below) |
| 2 | Commit the list to `main` daily and rebuild and re-paste the planner whenever it changes | Keeps the current design, but needs a manual re-paste on most days. Not workable |
| 3 | Backups keep `main`'s list | Simple, but Mac plans are refused whenever the live list differs, and a self-built plan can buy names that left the top 100 |

## Decision (proposed): option 1, with a published-hash trust anchor

1. **The universe travels with the plan.** A plan-first document gains the sorted `universe` list. The planner
   checks that `universe_hash` equals the hash of that list, instead of the bundled list.
2. **The day's hash is published.** After the daily run applies a list, the Mac publishes that day's
   `universe_hash` on the public slot-status signal the backup prompts already read. The planner refuses a plan
   unless its `universe_hash` equals the published hash for the trading day. A missing or stale published hash
   refuses the plan (fail closed). The backup is best effort, so a refused backup is acceptable.
3. **Bounds the planner keeps on its own,** because it can no longer rely on a bundled list: at most 100 symbols;
   every symbol canonical; no symbol in the BUNDLED `excluded` list (still hand-held); every existing
   per-order, daily and weekly dollar cap unchanged. A wrong list can change WHICH names are bought, never how
   much.
4. **The bundled `tickers.json` stays in the planner,** as the fallback for the hand-held lists and for a day with
   no published hash. A bundled-list plan is accepted only on a day with no list change.
5. **What changes in code (each test-first, each its own PR):** the plan schema and validator
   (`dca_order_plan.py`, `scripts/backup_planner.py`); the published status record; the Mac-built plan generator;
   the six generated prompts; the planner rebuild.

## Risks and trade-offs

- **Main risk:** a compromised or buggy Mac could publish a bad list and a matching plan. The dollar caps bound the
  loss, the hash binds plan to publication, and the bounds in item 3 hold. This is weaker than a bundled list the
  planner knows independently, and that is the price of a daily list.
- **Operational:** the planner and the six prompts change once, then stay stable while the list changes daily.
- **Not verified:** the format and publisher of the public slot-status signal (I have not read it), whether it can
  carry a hash, and whether Perplexity's sandbox can read it on the backup schedule.

## Verification

- A plan with a hash that differs from the published one is refused (`universe_hash_mismatch`).
- A missing published hash refuses the plan, and the primary slots are unaffected.
- A plan listing an excluded symbol or more than 100 symbols is refused.
- A dry run on the Mac for one day produces a plan the planner accepts and, with a doctored list, refuses.

## Open questions

1. Is the public slot-status signal the right channel, and who publishes it (the `slot-status-publish` job)?
2. May a bundled-list plan still be accepted on a day with no list change, or must every plan carry the list?
3. Who approves this change: it alters the backup's independent check, so it needs the operator.
