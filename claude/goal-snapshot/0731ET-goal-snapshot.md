# 07:31 ET goal snapshot task prompt

<!-- Operator: paste everything below the line into the new scheduled task (task ID pending:
TBD-task-id in docs/schedule_manifest.md). Not a live-mirror/ file: sync_live_prompts.py expects
every file there to map to an executor slot, and this task places no orders. -->

Schedule: 07:31 America/New_York, trading days only (cron `31 7 * * 1-5`; holidays skip through
`dca_goal_snapshot.should_run`). Requires this computer. Model: Sonnet 5.5, medium effort,
30-minute budget. The run must finish well before 3:40 PM ET; if it is still running at 3:40 PM ET,
stop, write no row, and report a `missed_snapshot`.

---

GOAL SNAPSHOT, READ ONLY. This task records the PREVIOUS trading day's close of the configured
Agentic account's holdings for goal progress (Phase 2 of docs/adr-goals-and-loss-hedge-shadow.md).
It places no orders. It calls exactly three Robinhood tools, all read-only: `get_portfolio`,
`get_equity_positions` and `get_equity_historicals`. Call no other Robinhood tool, and no order,
review or cancel tool of any kind, for any reason. Running at 07:31, before the open, means the
close being recorded is settled, not a live price.

1. TRADING DAY. From the repo root, run
   `python3 -c "import datetime, zoneinfo, dca_goal_snapshot as g; t = datetime.datetime.now(zoneinfo.ZoneInfo('America/New_York')).date(); print(t, g.should_run(t, None), g.previous_trading_day(t, None) if g.should_run(t, None) else '-')"`.
   `dca_goal_snapshot.should_run` decides whether today is a trading day (Gate 0's calendar);
   do not decide it yourself. If it prints False, stop: no read, no row, no alert. Otherwise the
   third value is the RECORDED DAY, the previous trading day, whose close you record.

2. ACCOUNT. The account number is written nowhere in this prompt. A native tool call does not
   expand `"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"`, so resolve it once with
   `python3 -c "import dca_paths, os; print(os.environ.get('ROBINHOOD_AGENTIC_ACCOUNT_NUMBER', ''))"`
   (the import loads the operator's gitignored `.env`) and use the printed value as the account of
   every native call below. If it is empty or still the placeholder, stop and report
   `missed_snapshot` (account unresolved). Never write the value into the artifact, the report or
   any message: write "the configured Agentic account".

3. READS. Call `get_portfolio` once for that account; an error means a missed snapshot. Then call
   `get_equity_positions` for that account and follow every page to the end. Save one object per
   position, `{"symbol": ..., "quantity": ...}`, exactly as the tool returned them (never drop,
   merge or round a position) to `$DIR/positions.json`, where
   `$DIR = <dca_paths.cron_tracking_root()>/goal_snapshot/<RECORDED DAY as YYYYMMDD>`. For each
   symbol, call `get_equity_historicals` for daily bars and take the close of the bar dated the
   RECORDED DAY. Save `{"SYMBOL": close, ...}` to `$DIR/closes.json`. A symbol with no bar for the
   RECORDED DAY is left out of that file, never filled from another day.

4. CAPTURE. Do not build rows yourself; `dca_goal_snapshot.capture_previous_close` owns that:
   ```
   python3 - "$DIR" <<'PY'
   import json, sys
   from datetime import date
   from pathlib import Path
   import dca_goal_snapshot as g
   d = Path(sys.argv[1]); day = date.fromisoformat(f"{d.name[:4]}-{d.name[4:6]}-{d.name[6:]}")
   positions = json.loads((d / "positions.json").read_text())
   closes = json.loads((d / "closes.json").read_text())
   res = g.capture_previous_close(day, positions_fn=lambda _d: positions,
                                  prior_close_fn=lambda sym, _d: closes[sym])
   out = {"rows": res.rows, "alerts": [{"kind": a.kind, "day": str(a.day), "detail": a.detail} for a in res.alerts]}
   (d / "snapshot.json").write_text(json.dumps(out, indent=2, sort_keys=True))
   print(json.dumps({"rows": len(res.rows), "alerts": out["alerts"]}))
   PY
   ```
   Any failed read (a tool error, a malformed position, a missing close) is a missed snapshot:
   the module returns no rows and one `missed_snapshot` alert, and you write no row anywhere, not
   even the positions that did read. If a tool call itself errored in step 3, skip the capture,
   write `snapshot.json` with no rows and one `missed_snapshot` alert naming the failing step (not
   the payload), and stop. Zero holdings is not a miss: the module writes one "no holdings" row.

5. LEDGER. Only when `snapshot.json` has rows and no alerts, ingest them with
   `LedgerStore(<GOAL_LEDGER_DB>).ingest_portfolio_snapshots(rows, source="goal_snapshot_0731", run_id="goal_snapshot_<YYYYMMDD>", as_of=<now, timezone-aware>)`
   from `dca-sizing-signals` (its own venv). The rows carry no account field; the ledger refuses
   any account-named key.

6. REPORT. One short final message: the RECORDED DAY, the number of rows (or "no holdings"), and
   any `missed_snapshot` alert with its detail. No dollar totals, no account number, no orders.
