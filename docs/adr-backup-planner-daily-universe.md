# ADR: How a daily-changing universe reaches the Perplexity backup planner

- **Status:** Accepted by the operator 2026-11-20 (option 1, as revised by the second read below). Implementation is
  in progress, test-first, in separate small pieces. Piece 1 (the recommendation set can carry the list) is done 2026-11-20
  (`dca_rec_models.RecommendationSet.universe`, 12 tests). Piece 2 (the recommender carries the list) is done 2026-11-20 (`dca_recommender.py`, 6 tests): the list is carried only
  when it has at most 100 names, so the live 122 leaves the set as before. Piece 3 (the plan-first plan and `scripts/backup_planner.py` validate against a carried list) is done 2026-11-20 (20 tests). Piece 3(d) (`dca_order_plan.validate_plan` accepts an optional carried `universe` for Mac plans, with the same bounds) is done 2026-11-20 (`tests/test_dca_order_plan_carried_universe.py`). The planner and the six prompts were rebuilt for both. Piece 4 (the compute fallback `--quotes` path and the `universe` command read the day's list via `--published`; open question 2 answered yes) and piece 4b (the safety-gated `compute --market-data` path) are done 2026-11-21. Known limit of 4b: on the gated path a carried name must already be on the bundled list (`carried_name_unsupported` otherwise), because `dca_runner.ACTIVE_TICKERS` is frozen at import and per-ticker sizing must cover every name; a name newly ranked into the top 100 is therefore bought by the gated backup only after the planner is rebuilt with it. Still not built: the final rebuild plus the one manual re-paste into Perplexity (piece 5).
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

## Second read (2026-11-20, before any code)

The Mac already PUBLISHES the day's recommendations: `RecommendationSet` (`dca_rec_models.py`) carries ticker,
amount and reference id per item plus `universe_hash`, and goes to the GitHub `recommendations` branch and Notion
(`dca_rec_publish.py`, `docs/plan_first_recommendations.md`). The backup planner reads it as `published_rec`. So the
day's hash is already published on a channel the backup trusts for amounts, and a separate slot-status hash adds
little independence (the same Mac writes both). The change therefore reduces to letting the SET carry the list.
`sizing_guard` is the precedent: an optional key, absent on older sets, so older payloads still parse.

## Options

| # | Option | Effect |
| --- | --- | --- |
| 1 | The plan carries the day's list, and the planner checks it against a hash published for that day | Backups follow the live list. The planner no longer knows the list on its own, so a trust anchor is needed (below) |
| 2 | Commit the list to `main` daily and rebuild and re-paste the planner whenever it changes | Keeps the current design, but needs a manual re-paste on most days. Not workable |
| 3 | Backups keep `main`'s list | Simple, but Mac plans are refused whenever the live list differs, and a self-built plan can buy names that left the top 100 |

## Decision (proposed): option 1, with a published-hash trust anchor

1. **The universe travels with the recommendation set, then the plan.** `RecommendationSet` gains an optional
   sorted `universe` (at most 100 canonical symbols; it must hash to `universe_hash`; every recommended ticker must be
   in it). The plan-first document gains the same list. The planner checks that `universe_hash` equals the hash of
   that carried list, instead of the bundled list.
2. **The published set is the anchor.** The recommendation set the Mac publishes for the day already carries the
   hash. The planner refuses a plan whose `universe` or `universe_hash` differs from the published set it was built
   from, and refuses a set with no carried list on a day the bundled list differs. A missing or stale set refuses the
   plan (fail closed). The backup is best effort, so a refused backup is acceptable. Publishing the hash a second
   time on the slot-status signal is optional hardening (open question 1).
3. **Bounds the planner keeps on its own,** because it can no longer rely on a bundled list: at most 100 symbols;
   every symbol canonical; no symbol in the BUNDLED `excluded` list (still hand-held); every existing
   per-order, daily and weekly dollar cap unchanged. A wrong list can change WHICH names are bought, never how
   much.
4. **The bundled `tickers.json` stays in the planner,** as the fallback for the hand-held lists and for a day with
   no published hash. A bundled-list plan is accepted only on a day with no list change.
5. **What changes in code (each test-first):** (a) `dca_rec_models.RecommendationSet` carries the list (tests
   written 2026-11-20, `tests/test_dca_rec_models.py`); (b) the recommender fills it and `dca_rec_publish`
   publishes it; (c) the plan-first document and `scripts/backup_planner.py` validate against the carried list with
   the bounds in item 3; (d) `dca_order_plan.validate_plan` for Mac plans; (e) the six generated prompts and the
   planner rebuild, which is the one re-paste you do by hand.

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
   (Answered yes 2026-11-21; see Status.)
3. Who approves this change: it alters the backup's independent check, so it needs the operator.
