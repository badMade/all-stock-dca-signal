# Handoff: 9:35 payload abort and backup-prompt fixes (2026-10-21 to 2026-10-23)

State of `main` when this was written: commit `26204ac3`. Nothing below is live until the prompts are repasted
(steps 1 and 2). This is a one-time handoff, not a standing reference: for the payload itself see
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

## Do these in order

1. **Repaste the six live-mirror prompts** from `claude/dca-executor-prompts/live-mirror/` into the scheduled tasks.
   This brings in the 12-minute wait and the new `ticker_errors` rule together.
2. **Repaste the six Perplexity backup prompts** from `claude/dca-executor-prompts/perplexity/`, and make sure the
   attached `backup_planner.pyz` is the one those prompts expect. On `main` at `26204ac3` it is **not**:
   - the prompts require content hash `41fc5fa0` (they match their generator);
   - `python -m scripts.build_backup_planner --out backup_planner.pyz --check` reports `DRIFT` for the committed file,
     whose last change was #1872, before #1898 changed the planner sources.
   Perplexity compares the attached file's hash to the prompt and stands down on any difference (step 0), so
   repasting these prompts with the committed file would make every backup do nothing. Before pasting, on a
   current `main`:
   - `python -m scripts.generate_perplexity_backup_prompts --check` (expect exit 0);
   - `python -m scripts.build_backup_planner --out backup_planner.pyz --check` (expect `UP_TO_DATE`).
   If the second reports `DRIFT`, the file has to be rebuilt and committed
   (`python -m scripts.build_backup_planner --out backup_planner.pyz`), and the prompts regenerated
   (`python -m scripts.generate_perplexity_backup_prompts --write`), before anything is pasted. The stamp can
   move again whenever `main` changes the planner, so always read it from the prompt, not from this note.
3. **Check the scheduled tasks' current Gate 5 text** against the repo copy. This was not visible when the
   changes were made. If the sentence "the launcher publishes only a complete universe" is not what a task holds,
   tell the engineer before pasting.
4. **Keep the Mac on current `main`.** It already logs timing lines, so it has #1883.
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

- That the scheduled tasks hold the same Gate 5 sentence as the repo.
- The new `ticker_errors` rule and the 95% floor live. The 10-22 slots were all 90/90.
- Whether the QTOP and DXYZ aborts were quiet tickers. Only TOPT could have been checked, and the result was not seen.
- A core CI job on Python 3.11. None appeared in any run that was read.

## Known fragile points

- The prompt wording "more than 4 of the 90 tickers today" is prose. If the universe size changes, edit it by hand. The
  test only ties the 95% figure to `DEFAULT_MIN_COVERAGE`.
- `tests/test_dca_console_routes.py::...[/api/diagnostics/architecture]` timed out once on CI (a socket timeout) and
  passed on re-run. If it recurs, treat it as a real problem.
- Whenever `main` changes the planner or the backup procedure, the Perplexity prompt stamps change and need
  regenerating and repasting. Two parties pushing regenerated prompts to one branch caused repeated conflicts on #1862.
- The committed `backup_planner.pyz` can drift from its sources, as it has on `main` at `26204ac3` (see step 2). The
  `--check` command above is how to find out; this note does not claim any test catches it.
- `config/recommendations.json` has both publishers disabled. If one is enabled, regenerate and repaste the Perplexity
  prompts so they name that channel.

## Rollback

Each change is one revertable PR. After a revert, repaste the previous prompts. The launcher change takes effect when
the Mac pulls `main`.

Originating session: https://claude.ai/code/session_01RvsYi9CAQVeaqYQ3VsAxc4
