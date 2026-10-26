# Handoff: deprecate the plan-first courier (2026-10-26)

Branch: `claude/rescue-no-data-tickers` (also carries the payload no-data rescue commit, see `HANDOFF_payload_rescue.md`).
Nothing below is committed or pushed. Open any PR as a DRAFT (repo rule).

## Goal
Retire the 5-minute Claude "plan-first data courier" and keep one program for Robinhood data: the payload launcher
(`scripts/robinhood_payload.py` + `deploy/launchd/robinhood_payload.sh`, docs/robinhood_payload.md).
Operator choices: target = the payload program; courier files = mark deprecated, keep code, delete nothing.

## What "courier" is
- Prompt draft: `claude/dca-executor-prompts/drafts/plan-first/courier.draft.txt` (never scheduled; plan-first is shadow-only).
- Code: `scripts/snapshot_cycle.py` (`fetch-plan`, `ingest`, `status`) over `dca_market_history.py`, `dca_market_sources.py`,
  `dca_rec_store.py`, `dca_recommender.py`, `dca_rec_publish.py`, `dca_fill_compare.py`.
- It saves raw bars/quotes AND `get_equity_orders` pages + `get_portfolio` buying power. The payload program only fetches
  bars and quotes, so the order/buying-power half is the gap.

## Done (safe, uncommitted)
- Deprecation notice at the top of `courier.draft.txt`.
- Deprecation text in the `scripts/snapshot_cycle.py` docstring.
- Banner in `docs/plan_first_recommendations.md`; note in the `docs/README.md` index row.
- Checked: `scripts/tests/test_snapshot_cycle.py`, `tests/test_config_invariants.py`, ruff on `snapshot_cycle.py` pass.

## Half-done and NOT recommended to keep (uncommitted)
A port of the courier's broker fetch into the payload program:
- `dca_market_history.py`: `normalize_response` split out of `_load_response`; new `MarketHistoryStore.broker_state(...)`;
  `ingest_run` now calls it. `tests/test_dca_market_history.py` and `scripts/tests/test_snapshot_cycle.py` passed after this.
- `scripts/robinhood_payload.py`: optional `bid_price`/`ask_price` in `validate_ticker`, bid/ask asked for in the chunk prompt,
  new `build_broker_prompt`, `load_broker_reply`, `ingest`, and `broker-prompt`/`ingest` subcommands.
- Not done: `robinhood_payload.sh` (second Codex call and ingest step), tests, docs.
- Test run on these edits: ruff clean, but 5 failures in `tests/test_robinhood_payload.py`:
  `test_default_floor_tolerates_a_few_missing_tickers`, `test_fresh_payload_passes_within_the_age_bound`,
  `test_payload_older_than_the_bound_is_stale`, `test_missing_or_mismatched_sidecar_fails_closed`,
  `test_launcher_has_no_deadline_by_default_and_never_skips_a_chunk`. Cause not investigated. The rescue commit
  (6998a1cf) was also never run, so some may predate these edits: `git stash`, run that file, compare.

## Why the port is not recommended now
- Fixes nothing live: today's failure is the payload refusing at 83/91 coverage, which the rescue commit addresses.
- Riskiest piece: a Codex worker would copy every page of paginated order history back verbatim in one reply. The assembler
  fails closed on an incomplete chain, but it adds a new failure point to the payload run.
- Without bid/ask in the payload quotes, every plan-first order is dropped as `spread_unavailable` (Gate 6.7), so a port
  also needs the quote change above.
- The recommender, store and reader stay untouched either way.

## Decision pending (operator)
1. Revert the port files (`git checkout -- dca_market_history.py scripts/robinhood_payload.py`), keep the deprecation notes,
   and soften their wording so they do not promise a fold-in. Recommended.
2. Or finish the port: shell launcher step gated by an opt-in env var (suggest `DCA_PLAN_FIRST_INGEST=1`, default off),
   tests for `ingest`/`broker-prompt`/bid-ask, then docs.

## Next steps for the cloud session
1. Run `pytest tests/test_robinhood_payload.py` on the branch with the port edits stashed, then on the rescue commit alone.
2. Apply the operator's decision above.
3. `ruff check .`, the affected tests, then the full suite (about 5 minutes).
4. Open a DRAFT PR.

## Side note: PR #1968
Docstring-only change to `_is_finite_scalar` in `dca_inverse_martingale.py` plus a rebuilt `backup_planner.pyz` and six
regenerated Perplexity prompts. No behavior change, unrelated to the courier. Low necessity; merge or close.

## Tooling gotchas seen
- `gh` is not installed on the Mac/Windows shell; use `git fetch origin pull/<n>/head` to read a PR.
- The repo is private, so the built-in browser cannot open PR pages.
