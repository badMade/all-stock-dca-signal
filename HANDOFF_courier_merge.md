# Handoff: fold the plan-first courier into the payload program (local run)

Written 2026-10-26 from cloud session https://claude.ai/code/session_01MWod9HM7ZnMMHJvf4UMvRp.
Delete this file before PR #1970 merges.

## Goal (operator, in their words)
One program for Robinhood data. The payload program (`scripts/robinhood_payload.py` +
`deploy/launchd/robinhood_payload.sh`, docs/robinhood_payload.md) takes over everything the 5-minute Claude courier did,
including scheduling. Then delete the courier branch. Not "deprecate and leave"; move it.

## Where things are
- Repo: `/Users/geoffreyplymale/Documents/Git/all-stock-dca`. Work branch: `badmade/funny-noether-aankvh`.
- PR: https://github.com/badMade/all-stock-dca/pull/1970 (ready for review, CI green). Push to this same branch;
  do not open a new PR. Any new PR must be a draft.
- Branch pushed so far holds ONLY the deprecation notes (courier.draft.txt, snapshot_cycle.py docstring,
  docs/plan_first_recommendations.md, docs/README.md). No runtime code changed.
- The unfinished port is commit `ec070d05` (full id `ec070d05bb93d97d27ed68f78507ef658f26671a`). Its original branch,
  `origin/claude/rescue-no-data-tickers`, was already deleted from GitHub by 2026-10-26 23:30 UTC, leaving the commit
  unreachable. It is now pinned on `origin/rescue/courier-port` (session_01V6XG5FEYM3QS6oqKHh8VEu). Do NOT delete that
  branch until the port is on the PR branch.
- `/Users/geoffreyplymale/all-stock-dca-courier` was named by the operator; the cloud session could not see it.
  Check it: `git status` and `git log origin/<branch>..HEAD`. If it holds unpushed work, push it to a branch first.

## Get the port back
```bash
git fetch origin
git checkout badmade/funny-noether-aankvh
git rev-parse --verify ec070d05^{commit}   # present after the fetch (rescue/courier-port)
git checkout ec070d05 -- dca_market_history.py scripts/robinhood_payload.py
```
Verified: main has not touched those two files since ec070d05's base, so a whole-file checkout loses nothing.

## What the port already contains (untested by new tests)
- `dca_market_history.py`: `normalize_response` split out of `_load_response`; `MarketHistoryStore.broker_state(...)`;
  `ingest_run` calls it.
- `scripts/robinhood_payload.py`: optional `bid_price`/`ask_price` in `validate_ticker` (ask < bid fails the ticker);
  bid/ask requested in the chunk prompt; `build_broker_prompt`, `load_broker_reply`, `ingest`
  (lease, 10-minute freshness bound, exit codes 0/2/3/6); `broker-prompt` and `ingest` subcommands.

## Correction to the earlier handoff
The earlier note listed 5 failing tests in `tests/test_robinhood_payload.py`. They do not reproduce: with the port
applied, `tests/test_robinhood_payload.py`, `tests/test_dca_market_history.py` and `scripts/tests/test_snapshot_cycle.py`
all pass (cloud run, 2026-10-26). If they fail on the Mac, suspect the environment, not the port.

## Still to do
1. Tests first (the operator's rule: show tests, get approval, then implement). Cover: bid/ask accepted, absent, ask<bid
   rejected; `build_broker_prompt` (read tools only, account via `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`, never inlined);
   `load_broker_reply` (error reply, no pages, normalisation); `ingest` (stale payload, old reply, lease held, refusal);
   the launcher step.
2. `deploy/launchd/robinhood_payload.sh`: after the merge, run a second `codex exec` with `broker-prompt`, then
   `ingest`. Make a broker or ingest failure NON-fatal to the payload build (Gate 5 must not depend on it). Decide the
   gate: default on, or opt-in `DCA_PLAN_FIRST_INGEST=1` (earlier recommendation, default off, is the safer start).
3. Scheduling: one launchd job per slot does fetch and ingest. Remove the separate courier schedule entry and any
   `snapshot_cycle` cron. Check `deploy/launchd/`, `docs/schedule_manifest.md`, `config/` for courier references.
4. Docs: `docs/robinhood_payload.md`, `docs/plan_first_recommendations.md` (drop the deprecation banner once ported),
   `docs/schedule_manifest.md`, `docs/README.md`. If you add a module or config file, update `pyproject.toml`.
5. Gates: `ruff check .`, the three test files above, then the full suite (about 5 minutes). Smoke:
   `python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo`.
6. Real-run check on the Mac (cannot be done in the cloud): install the launchd job, run one slot with
   `DCA_TRADING_DAY` and `DCA_PAYLOAD_NOW_ET` set, confirm a payload and its ingest output appear.
7. Only after 1-6 and the PR merges: delete `origin/rescue/courier-port` (the courier branch itself is already gone).

## Keep ONE writer for the `recommendations` branch
The ported `ingest` (`scripts/robinhood_payload.py`, `publish_all(publishers_from_settings(...))`) publishes to every
enabled channel, `github_branch` included, and only the slot it just computed. On the Mac that push succeeds, and it
would overwrite the whole-day document that the `snapshot_cycle publish` launchd job (PR #1964, installed on the Mac
2026-10-26) pushes every 5 minutes, so the backups' `classify --published` would lose earlier slots' sets. Filter the
branch out exactly as the courier's ingest does: use `scripts.snapshot_cycle._courier_publishers(settings.publish,
repo_dir=...)` instead of `publishers_from_settings(...)`, and add a test that the ported `ingest` never calls the
`github_branch` publisher. The publish job then picks up the new `rec_*.json` within 5 minutes.

## Risks
- The broker worker must copy every order page back verbatim in one reply. The assembler fails closed on an incomplete
  chain, but a long history may truncate. Hence step 2: non-fatal.
- Without bid/ask in quotes, every plan-first order drops as `spread_unavailable` (Gate 6.7).
- Never inline the account number; `tests/test_config_invariants.py` greps for it.

## Other open item
PR #1968 (docstring-only plus rebuilt `backup_planner.pyz` and six regenerated Perplexity prompts): merge or close.

## Prompt to paste into the local Claude Code session
> Read HANDOFF_courier_merge.md on branch badmade/funny-noether-aankvh and continue PR 1970. Restore the port from
> ec070d05, write the tests first and show them to me before implementing, then do the launcher step, scheduling and
> docs so the payload program is the single Robinhood data program. The port is pinned on origin/rescue/courier-port;
> do not delete it until I say so. The ported ingest must not push the recommendations branch: use
> snapshot_cycle._courier_publishers, since the Mac's `snapshot_cycle publish` launchd job is its only writer.
