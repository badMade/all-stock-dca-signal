# 07:31 ET goal progress snapshot -- registration and setup (task not registered)

Phase 2 of `docs/adr-goals-and-loss-hedge-shadow.md`, approved 2026-11-14. The code is on `main`:
`dca_goal_snapshot.py` (capture, `previous_trading_day`, `should_run`, `scheduled_task_spec`) and
`LedgerStore.ingest_portfolio_snapshots` in `dca-sizing-signals/market_data_ingest/ledger/store.py`.

## Registration

| Field | Value |
| --- | --- |
| Time | 07:31 America/New_York (`CRON_TZ=America/New_York 31 7 * * 1-5`; the prompt itself skips holidays) |
| Where | Claude Desktop Code-tab scheduled task on the Mac, "Requires this computer" |
| Working directory | the main checkout (`~/Documents/Git/all-stock-dca`) |
| Connectors | Robinhood MCP only |
| Budget | 30 minutes (`scheduled_task_spec()["max_runtime_minutes"]`) |
| Trades | **no** -- read tools only |
| Model | Sonnet 5.5, medium effort |
| Finish | well before 3:40 PM ET (the 30-minute budget ends long before) |

### One-time setup: the task's interpreter

The ledger needs `duckdb`, which neither `python3` nor the repo's `.venv` has (the core is
stdlib-only). Do not reuse `~/.cache/all-stock-dca/ci-venvs/...`: `scripts/local_ci.sh` rebuilds
those. Build a dedicated venv once, from the main checkout:

```bash
python3.12 -m venv ~/.cache/all-stock-dca/goal-snapshot-venv
```

```bash
~/.cache/all-stock-dca/goal-snapshot-venv/bin/pip install -r dca-sizing-signals/requirements.txt
```

After it is registered, add its row (with the task ID) to the canonical table in
`docs/schedule_manifest.md` and delete the "Planned, not yet registered" note there.

## Decisions (resolved 2026-11-15: operator chose the defaults)

1. **Ledger database file.** Resolved: the prompt uses
   `dca_paths.stream_root("goal_snapshot", "ledger.duckdb")`, which resolves under
   `$DCA_CRON_TRACKING_ROOT`. The Friday section and any future goal-progress job must read the
   same file.
2. **Where a `missed_snapshot` alert goes.** Resolved: the prompt writes it to `alerts.json` and
   ends the run with a visible failure. Wiring it into Gate 9 email / `scripts/alert_digest.py`
   is a later change, not part of this registration.

---

## Prompt

The complete prompt is its own file, `claude/goal-snapshot/0731ET-goal-snapshot.md`. Paste that
whole file as the task prompt: nothing in this document is part of the prompt, and the prompt
needs no edits or substitutions.

## How this matches the code

- `should_run` / `previous_trading_day` with `None` use the real NYSE calendar through Gate 0
  (`dca_runner.market_day_status`), so a Monday run records Friday and the Friday after
  Thanksgiving records Wednesday.
- `capture_previous_close` is all-or-nothing: any raised read, missing close, bad quantity or
  repeated symbol gives no rows and one `missed_snapshot` alert. Empty holdings give the single
  "no holdings" row, which the ledger accepts since #2328.
- `ingest_portfolio_snapshots` refuses account-named keys, so an account field can never reach the
  table even if the prompt is misfollowed. Re-ingesting identical rows is a no-op.
