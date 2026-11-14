# Console changelog

Covers `console/` and `dca_console*.py`. Newest first. Every change to a file under
`console/` needs a new heading here, a `CONSOLE_VERSION` bump and a re-pinned
`ASSET_DIGEST` in `dca_console_release.py` (`tests/test_console_release.py` enforces it).
Process: `docs/local_console.md#release-process-stable-vs-sandbox`.

## 1.3.0

- Goals panel on the Config page (docs/adr-goals-and-loss-hedge-shadow.md): shows the goals in effect and saves a
  growth-goal or loss-hedge change as a proposal file under `<output>/goal-proposals/` (`POST /api/goals/propose`,
  same guard order and 1 KB limit as `/api/jobs`; read side `GET /api/goals`). The new allowlisted job
  "Open goal-change PR" (`propose-goal`, `scripts/propose_goal_pr.py`) turns the newest proposal into a draft PR
  against `config/goals.json`. The console still never writes `config/`. Hedge mode is off or shadow only.

## 1.2.0

- Composite rank page: adds the daily trade list (with how it changed day to day), the review-first ticker proposal,
  and the zero-weight shadow logs for Octopus (next-day forecast, S&P 500 names) and ml-forecaster (sizing tilt).
  Read-only; the realised-return scorecards stay a CLI command (`shadow-scorecard`).

## 1.1.0

- New Composite rank page (read-only): the daily S&P 500 composite ranking written by the scheduled
  `daily-composite-rank` task, what entered or left the above-80 list, and how far ranks typically move. Backed by
  `GET /api/rank` and `dca_console_rank.py`; shows nothing until the task has run.

## 1.0.0

- First versioned release. Version shown in the sidebar and `/api/health`.
- Stable / sandbox channels: `--sandbox` runs demo data only, badged SANDBOX, default port 8766.
- Environment page re-probes package availability on each load (others on a 60 s TTL).
- `from.py` no longer creates a `.venv` inside a linked worktree; it reuses the main checkout's.
