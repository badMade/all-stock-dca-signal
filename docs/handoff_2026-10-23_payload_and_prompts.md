# Handoff: 9:35 payload abort and backup-prompt fixes (2026-10-21 to 2026-10-23)

State of `main` when this was last updated (2026-10-26, after #1958 and #1961): commit `91ee6f22`. Nothing below is live until
the prompts are repasted (steps 1 and 2). This is a one-time handoff, not a standing reference: for the payload itself see
[robinhood_payload.md](robinhood_payload.md).

## What happened

| Date and slot | Event | Cause |
| --- | --- | --- |
| 2026-10-21 09:35 (Claude) | Aborted at Gate 5, no orders | One ticker (TOPT) had no 5-minute bar for 09:30 to 09:35, so the launcher's old 100% coverage floor published nothing. Source: the Mac log (`coverage 89/90 ... publishing nothing`). |
| 2026-10-21 09:55 (Perplexity backup) | Failed closed, no orders | The prompt told the executor to read a `recommendations` branch that does not exist (both publishers are disabled). The executor then probed other routes and broke rules 8 and 11. |

The first theory, that the payload build outlasts the 5-minute wait, was wrong for the 09:35 abort. The Mac log shows
builds of 130 to 244 s with no retries on 2026-10-22.

Over 2026-10-19 to 10-21, 5 of 24 slots aborted this way. All four slots on 10-22 published 90/90.

## What is merged

| PR | Commit | Change |
| --- | --- | --- |
| #1862 | `25a9719e` | Perplexity backup prompts: a disabled publisher is not named; an enabled channel gets one read attempt and no alternate route. Planner and prompts regenerated. |
| #1873 | `c100807` | Six live-mirror prompts: re-check the payload every 30 s for up to 12 minutes (was 5). |
| #1883 | `627b7c2` | Launcher logs `timing chunk=` and `timing total=` lines. Also fixes a silent early exit on clean replies (`grep -o` under `set -euo pipefail`). |
| #1878 (separate change) | `7e3a888` | Launcher coverage floor lowered from 100% to 95% (4 of 90 tickers may fail). |
| #1897 | `41cddf1` | Six live-mirror prompts: abort on errored tickers only above 5% of the universe (more than 4 of 90). Up to that, continue and name them. |
| #1902 (separate change) | `26204ac3` | Shadow 15:31 launchd agent for the 3:30 payload, and an opt-in build deadline (`DCA_BUILD_DEADLINE_SECONDS`, default none). Operator-installed; changes no trade. |
| #1906 | `810d2564` | This note. |
| #1907 | `30d5a8ff` | Rebuilt the committed `backup_planner.pyz` (stamp `41fc5fa0`) after it had drifted from its sources, and added two tests that fail CI if the file or the prompts ever disagree again. |
| #1894 | `b0030620` | Changed `near_close_dca.py` (a bundled file), so the planner stamp moved to `b106fea3` (since superseded). Prompts and file were regenerated in the same PR. |
| #1911 | `147c4faf` | Rolled back by #1914, restored by #1957 with a stronger test. Jules fix: `bool` values no longer pass the numeric checks in `_unrepresented_prior_candidate_part` (`near_close_dca.py`). Planner file and prompts were rebuilt in the PR (stamp `5b0c20bd`). |
| #1914 | `b396c8e3` | Duplicate-JSON-key rejection in `dca_console.py` (kept). Built on the 10-19 base, so it also rolled back other merged work: see "What #1914 rolled back" below. Moved the planner stamp. |
| #1916 | `16639526d` | Added CCI to the active universe: 91 tickers, not 90. Moved the planner stamp. |
| #1921 | `d37adcc4` | Plan refs use the broker's native UUID. Moved the planner stamp to `07c79aa9` (since superseded). |
| #1922, #1923 | `297430ac`, `4e8aa8dc` | Changed bundled files (titles: "Rebase pr 1893", "Perf opt path resolve"), so the planner stamp moved to `61cbe8ec`. Prompts and file were regenerated. Not reviewed here. |
| #1925 | `5e5de7c7` | Restored this note after #1914 deleted it. |
| #1927 | `73968ab7` | Fixed a red test on `main` caused by #1925: a short commit hash made only of digits in this note reads as an account number to `test_no_account_shaped_literal_in_tracked_config_source_or_docs`. |
| #1926 | `9f6ae186` | Removed the stray `pr_description.txt` that came in with #1911. |
| #1930 | `205ae99e` | Restored `recorded_pr_number` handling in `triage_reconcile` that #1911 had dropped. |
| #1931 | `943ecac3` | Previous refresh of this note. |
| #1933 | `c6d6632b` | CI: every push to `main` gets its own concurrency group. |
| #1935 | `5aa6653f` | Console routes test module stubs the architecture scan for every test (autouse fixture), and the gate-order test asserts the untrusted-host request never reaches it. |
| #1937 | `830d2340` | Payload refusal marker change in `scripts/robinhood_payload.py` (see `docs/robinhood_payload.md`). Not reviewed here. |
| #1949, #1950, #1951 | `447d9cac`, `15a6d8c4`, `86039cd4` | Titles: `scripts.sync_live_prompts` (live task bodies up to their mirrors), Perplexity backups stand down before the broker pull, `ref_id_format_rejected` names only the UUID. Not reviewed here. The stamp moved to `6ca697a4` after these; which one moved it was not checked. |
| #1954 | `0503586a` | Re-added the account-loading step to the bridge-shell drafts and the three `cloud-clone-*` drafts on purpose (this restores #1913's content that #1914 had rolled back). |
| #1956 | `b6e2b9ec` | Restored #1907's two drift guards and #1915's CRLF fold that #1914 had rolled back. No prompt changes. |
| #1957 | `d69c8e23` | Restored the #1911 boolean-trap guards in `near_close_dca.py` with a stronger test (#1911's own test passed without the fix). Changed a bundled file, so the stamp moved (to `123248510381...`, since superseded). |
| #1958 | `91ee6f22` | Restored the #1912 `_is_finite_scalar` in `dca_inverse_martingale.py` and its tests. Merged on top of #1957, so the one rebuild covers both: the stamp is `98d1a5a9`. Six Perplexity prompts change (only the expected hash in step 0). Live-mirror prompts unchanged. |
| #1960 | `6924e5d6` | Previous refresh of this note. |
| #1961 | `5eafd4b6` | Titles: payload launcher rescues a dead chunk in small requests and keeps dead workers' logs (`deploy/launchd/robinhood_payload.sh`, `docs/robinhood_payload.md`, `tests/test_robinhood_payload.py`). Not reviewed here. The Mac must pull `main` to get it. |
| #1915 | `93f8aaeb` | `build_backup_planner` folds CRLF to LF when it reads bundled files, so a Windows rebuild gives the same hash and bytes as Linux. The committed file and prompts did not change. |

## Do these in order

1. **Repaste the six live-mirror prompts** from `claude/dca-executor-prompts/live-mirror/` into the scheduled tasks.
   This brings in the 12-minute wait and the new `ticker_errors` rule together.
2. **Repaste the six Perplexity backup prompts** from `claude/dca-executor-prompts/perplexity/`, and attach the
   `backup_planner.pyz` from the same `main` commit. Until #1907 the committed file had drifted from its sources
   (stamp `f1fc9f5c` against `41fc5fa0`), which would have made every backup stand down at step 0. It is fixed on
   `main` at `30d5a8ff`: the prompts and the committed file agree. On `main` at `91ee6f22` both carry `98d1a5a9` (#1957 and #1958 changed bundled files, so the stamp moved from `6ca697a4`; repaste the six Perplexity prompts once, now that both have landed) (read the stamp from the prompt you paste, not from this note). Two tests in
   `scripts/tests/test_build_backup_planner.py` now fail CI if the file stops matching its sources or the prompts.
   Before pasting, on a current `main`, confirm it still holds:
   - `python -m scripts.generate_perplexity_backup_prompts --check` (expect exit 0);
   - `python -m scripts.build_backup_planner --out backup_planner.pyz --check` (expect `UP_TO_DATE`).
   If `main` has changed the planner since, the stamp changes: read it from the prompt you are about to paste and make
   sure the attached file's hash equals it. Never reuse a `backup_planner.pyz` from an older checkout.
3. **Check the scheduled tasks' current Gate 5 text** against the repo copy. This was not visible when the
   changes were made. If the sentence "the launcher publishes only a complete universe" is not what a task holds,
   tell the engineer before pasting.
4. **Keep the Mac on current `main`.** It already logs timing lines, so it has #1883. #1961 changed `deploy/launchd/robinhood_payload.sh` (dead-chunk rescue), so pull `main` on the Mac again.
5. **After the next trading day, read `/tmp/dca-robinhood-payload.log`** on the Mac and check:
   - `timing total seconds=` per slot (expect about 2 to 4 minutes);
   - any `coverage X/90 ... publishing nothing` line, and which tickers it names;
   - any slot where tickers failed but the payload still published. The session should then continue and name the
     tickers in its evidence.

## What to do with the log

| What you see | What it means | Next step |
| --- | --- | --- |
| Slots publish, totals are under about 5 minutes | Everything works. | Nothing. |
| `newest intraday bar HH:MM is far before the HH:MM anchor` on liquid names | Robinhood bars lagging behind the launch. | Scope a retry for just the failed tickers. |
| A thin ticker repeatedly missing bars, more than 4 at once | Floor too tight for the universe. | Revisit the floor or exclude the ticker. |
| `no slot is due now`, or no run at all | Mac clock, launchd or sleep problem. | Check `date` shows Eastern time and `launchctl list \| grep all-stock-dca`. |

## Not verified

- That `main` stayed green after #1958 and #1961. The last run I read was green on #1958's own head.

- That the scheduled tasks hold the same Gate 5 sentence as the repo.
- The new `ticker_errors` rule and the 95% floor live. The 10-22 slots were all 90/90.
- Whether the QTOP and DXYZ aborts were quiet tickers. Only TOPT could have been checked, and the result was not seen.
- A core CI job on Python 3.11. None appeared in any run that was read.

## Known fragile points

- The prompt wording "more than 4 of the 90 tickers today" is prose and is now stale: #1916 made the universe 91. The
  threshold still holds (5% of 91 is 4.55, so more than 4 still aborts), only the count in the wording is out of date.
  Edit it by hand with the next prompt change. The test only ties the 95% figure to `DEFAULT_MIN_COVERAGE`.
- `tests/test_dca_console_routes.py::...[/api/diagnostics/architecture]` timed out on CI (a socket timeout) on #1897,
  #1907 and #1931, and passed on the next run each time. The cause is a real whole-repo scan on a background thread under
  CI coverage tracing starving the request. #1880 stubbed the scan in that test; #1914 silently reverted the stub with
  unrelated files; #1935 restored it for the whole module and added a guard test. One green run does not prove it is gone
  and the link is inferred, not reproduced. If it times out again, treat it as a real problem.
- What #1914 rolled back (it was built on the 10-19 base; squash-merged on 10-23):

  | Rolled back | State on `main` today |
  | --- | --- |
  | #1911 boolean-trap fix (`near_close_dca.py`) | Restored by #1957 (with a stronger test; #1911's own test passed with or without the fix). |
  | #1912 `_is_finite_scalar` (`dca_inverse_martingale.py`) | Restored by #1958. Opt-in mode, off by default. |
  | #1907 drift guards and #1915 CRLF fold | Restored by #1956. |
  | #1880 payload refusal marker and console scan stub | Restored by #1937 and #1935. |
  | #1913 account-loading step and `cloud-clone-*` drafts | Restored on purpose by #1954. (An earlier audit wrongly called #1914's deletion of the drafts correct.) |
  | #1917 no-folder failure form in `0915ET-bridge-health-check.draft.txt` | **Missing.** Draft only. |
  | #1892 wording of the 1530ET `reason` in `config/scheduled_tasks.json` | Missing; wording only. |
  | #1906 and this note | Restored by #1925. |

  Kept from #1914: duplicate-key rejection in `dca_console.py` POSTs, with its test.
- Line endings: a planner rebuilt on Windows used to stamp `f4bbe2f9` instead of CI's `675d66b7` (the hash of the same
  sources with CRLF endings), which failed three guard tests on #1911. #1915 fixed the build, #1914 rolled it back and #1956
  restored it, so a rebuild on either OS should now match. If a guard test still reports a mismatch, check for an older checkout of `build_backup_planner.py`.
- Write commit hashes in this note with at least one letter (use 9 or more characters). A hash made only of digits, such as
  the 8-character one that broke `main` after #1925, fails the account-number scan.
- Whenever `main` changes the planner or the backup procedure, the Perplexity prompt stamps change and need
  regenerating and repasting. Two parties pushing regenerated prompts to one branch caused repeated conflicts on #1862.
- The committed `backup_planner.pyz` must be rebuilt in the same PR as any change to the planner's sources. It drifted
  once (fixed in #1907), and two tests now catch it. Concurrent planner PRs will conflict on the binary: rebuild from
  the merged sources instead of choosing a side.
- `config/recommendations.json` has both publishers disabled. If one is enabled, regenerate and repaste the Perplexity
  prompts so they name that channel.

## Rollback

Each change is one revertable PR. After a revert, repaste the previous prompts. The launcher change takes effect when
the Mac pulls `main`.

Originating session: https://claude.ai/code/session_01RvsYi9CAQVeaqYQ3VsAxc4
