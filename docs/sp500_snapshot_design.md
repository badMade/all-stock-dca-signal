# S&P 500 market snapshot task (design, not implemented)

Status: proposal, 2026-10-18. Nothing here is wired; no code or schedule exists yet.

## Why a separate task

The plan-first courier (`scripts/snapshot_cycle.py`, every 5 minutes) fetches only the 68
`active` symbols of `config/tickers.json`, in chunks of 25 (`dca_market_history.fetch_plan`):
quotes + 5-minute bars per chunk, plus daily bars on the first run of a day, plus one paginated
`get_equity_orders` and one `get_portfolio`. That is about 11-14 MCP calls per run.

Pointing the same loop at ~503 S&P 500 symbols means 21 chunks: 42 calls per run (63 on the
first), every 5 minutes, each saved verbatim by the agent. Two problems:

1. The courier's contract is "save every result verbatim, stop on any failure or oversize
   result". 21 chunks of 5-minute bars since 9:30 will hit "too large to save verbatim" and
   turn every run into `courier error`, which would also starve the buy-universe recommendations.
2. `universe` feeds `universe_hash` and the plan validators. Symbols outside the buy universe
   must never reach them, or plan validation fails (`scripts/backup_planner.py` compares
   `universe_hash`).

So the S&P 500 snapshot is its own scheduled task, its own run directory, and its own output
tree. It never touches the recommendation engine, `RecommendationStore`, or any order path.

## Scope: read-only market data

Allowed tools: `get_equity_quotes` (and, once a day, `get_equity_historicals`). No orders,
portfolio, or account tools, so it needs no account number.

| Data | Cadence | Calls (503 symbols, 25/chunk = 21 chunks) |
| --- | --- | --- |
| Quotes (last, bid/ask, prev close) | every 15 min, 09:35-15:50 ET | 21 |
| Daily bars (last ~14 days) | once, after the close (16:10 ET) | 21 |
| 5-minute bars | not fetched (see below) | 0 |

Why no 5-minute bars: the 15-minute quote series already gives an intraday path, and the full
daily history for the S&P 500 already lives in the Yahoo CSV archive maintained by
`tickers-daily-update` (`ticker_data/`, ~6,178 files). Check coverage before relying on it
(see Open questions). 5-minute bars for 500 names is the part that would blow the verbatim limit.

## Constituent list

- New file `config/sp500.json`: `{"version", "as_of", "source", "symbols": [...]}`, validated by a
  new `schemas/sp500.schema.json`, loaded through `dca_config` (fail closed: duplicate keys,
  non-object, empty list all reject).
- Not a source of truth for buying. `tests/test_config_invariants.py` should assert it is
  independent of `active`/`excluded`/`whole_share`; overlap with `active` is expected and fine.
- Refresh: manual, monthly or on index-change announcements. The repo makes no network calls, so
  the list is produced outside (user export, or an agent session with a web source) and committed.
  Share-class tickers need `symbol_corrections` handling (BRK.B, BF.B: Robinhood spelling).
- Because this session had no broker access to verify instruments, unresolved symbols are
  expected. The task records them per chunk and carries on; it does not abort the run.

## Mechanics (mirrors the existing courier)

New module `scripts/sp500_snapshot.py`, same two-phase shape as `snapshot_cycle`:

1. `fetch-plan`: session gate (reuse `_session_status`), then emit `{"run_dir", "calls"}` with
   the chunked quote calls. Run dir: `<cron root>/market_snapshots/sp500/<date>/incoming/<HHhMMmSSs>/`.
   Write a `fetch_plan.json` marker (`planned_at`) exactly like `RUN_MARKER` so ingest can reject
   stale or future-stamped runs (`MAX_RUN_AGE_SECONDS`).
2. `ingest --run-dir`: verify the dir sits under today's `incoming/`, verify freshness, parse each
   `quotes_N.json`, and write one normalised `sp500_quotes_<HHMM>.json` plus a rolling
   `sp500_latest.json`. Per-symbol status (`ok`, `missing`, `stale_quote`, `halted`), reusing the
   stale/late/future-stamp refusals from PR #1708/#1709 instead of re-deriving them.
3. Lease: `SchedulerLease` with key `window_key(day, "sp500_snapshot", "ingest")`, so two firings
   cannot interleave.
4. Output is partial-tolerant: a chunk that fails marks its symbols `missing` and the run still
   reports `PARTIAL`. (The buy-universe courier fails closed because it feeds money decisions;
   this one feeds analysis only.)

Scheduled task (separate `~/.claude/scheduled-tasks/dca-sp500-snapshot/SKILL.md`), same hard
rules as the courier: Robinhood read tools only, save results verbatim, no repo edits, one-line
reply. Cron `*/15 9-15 * * 1-5` plus one `10 16 * * 1-5` for the daily-bars run.

## Where the data is used

- Analysis/research only to start: market breadth, relative strength of the 68 vs the index,
  candidate screening for `ticker-advisor/`, and the 3D market graph's data loaders
  (`docs/market_graph.md`).
- Not wired into sizing, budgets, or any slot. Doing that later is a separate decision with its
  own review, per the "consolidating is a decision" rule in `CLAUDE.md`.

## Required repo changes (when implemented)

- `scripts/sp500_snapshot.py` + `scripts/tests/test_sp500_snapshot.py` (fetch-plan chunking,
  idle outside session, ingest rejects stale/outside-incoming runs, partial chunk failure,
  universe hash untouched).
- `config/sp500.json`, `schemas/sp500.schema.json`, an invariant test, and a `pyproject.toml`
  `data-files` entry (new config file rule).
- Scheduled-task file, plus a short pointer in `docs/plan_first_recommendations.md` and
  `docs/schedule_manifest.md`.
- `ruff check .` and the coverage gate stay green; no new runtime dependency.

## Open questions (need an owner decision)

1. Data wanted: quotes only (recommended), or also daily bars via the courier path?
2. Cadence: 15 minutes (recommended) or 5? 5 minutes is 252 calls/hour against 84.
3. Constituent source: who produces `config/sp500.json`, and how often is it refreshed?
4. Archive coverage: how many S&P 500 symbols are already in `ticker_data/`? If most are, daily
   bars can come from the archive and the post-close run can be dropped.
5. Does Robinhood's `get_equity_quotes` accept 25 symbols per call reliably for all 21 chunks?
   The courier's 25 was chosen for 68 symbols; confirm with one live market-hours run before
   scheduling it.
