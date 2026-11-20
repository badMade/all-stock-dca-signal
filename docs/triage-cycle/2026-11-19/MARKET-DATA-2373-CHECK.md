# Rock 6 (#6): PR 2373 post-merge acceptance check (`market_data_unavailable`)

**Status change since the digest.** The 2026-11-19 digest called PR 2373 open. `gh pr view 2373` now shows it MERGED at
2026-11-19T23:35:13Z, after the digest ran (23:20Z). PR title: "market_data_unavailable: sliding worker pool for payload
chunks"; files: `deploy/launchd/robinhood_payload.sh`, `docs/robinhood_payload.md`, `tests/test_robinhood_payload.py`.
Merged is not verified-fixed: the PR only changes the Mac payload builder, so it can be judged only on a live trading day.

Format follows `docs/triage-cycle/MARKET-DATA-1530-CHECK.md`.

## Prerequisite (operator)
The payload builder is run from the Mac's checkout by launchd. After merge, confirm the Mac checkout holds the merge commit
(`git -C <checkout> log --oneline -1 -- deploy/launchd/robinhood_payload.sh`) and that the launchd job was reloaded if it
caches the script. Without that, the next day's builds still use the old scheduler.

## What to check on the next trading day (2026-11-20 first)
For each slot's payload build (9:36 and 10:31 are the ones the 2026-11-12 check named; the 14:30 build is the one that stalled
on 2026-11-16 and published at 14:48):
1. Build duration: the payload's own timing field or the build log's start and publish times; compare with the same slot on
   2026-11-16. Target: publishes inside its slot deadline.
2. Coverage: `payload_plan_<hhmm>.json` / `market_data_<date>_<hhmm>.json` ticker count against the slot's expected universe
   (for example "payload 122/122" in the 14:30 gate9 notes on 2026-11-19).
3. Chunk failures: count of chunks that timed out or returned empty in the build log; the sliding pool should show a stalled
   chunk not delaying the rest.
4. Slot effect: no `market_data_unavailable` / Gate 5 abort in `triage_log_summary --json` for the day.

## Condition to move the row off "PR open"
Two consecutive trading days with every payload published inside its slot deadline, full coverage, and no
`market_data_unavailable` abort. Until then the operator keeps the row at its current Progress note. The row's PR number
should be 2373 (the digest already set it); a Progress label of "Merged, checks green" is derivable now but is not evidence
the fix works.
