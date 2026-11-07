# Console changelog

Covers `console/` and `dca_console*.py`. Newest first. Every change to a file under
`console/` needs a new heading here, a `CONSOLE_VERSION` bump and a re-pinned
`ASSET_DIGEST` in `dca_console_release.py` (`tests/test_console_release.py` enforces it).
Process: `docs/local_console.md#release-process-stable-vs-sandbox`.

## 1.1.0

- New Composite rank page (read-only): the daily S&P 500 composite ranking written by the scheduled
  `daily-composite-rank` task, what entered or left the above-80 list, and how far ranks typically move. Backed by
  `GET /api/rank` and `dca_console_rank.py`; shows nothing until the task has run.

## 1.0.0

- First versioned release. Version shown in the sidebar and `/api/health`.
- Stable / sandbox channels: `--sandbox` runs demo data only, badged SANDBOX, default port 8766.
- Environment page re-probes package availability on each load (others on a 60 s TTL).
- `from.py` no longer creates a `.venv` inside a linked worktree; it reuses the main checkout's.
