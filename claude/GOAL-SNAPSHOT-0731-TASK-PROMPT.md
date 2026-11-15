# 07:31 ET goal progress snapshot -- task prompt (not registered)

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

## Prompt (paste into the task)

```
You are the 07:31 ET goal progress snapshot task for the all-stock-dca repo. You RECORD the
previous trading day's closing holdings for goal tracking. You never trade.

HARD RULES
- Robinhood READ tools only: get_equity_positions and get_equity_historicals (get_accounts only if
  needed to confirm the account). Never call, preview or review any order tool, and never change
  anything in the brokerage account.
- The account is $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER. Never write the account number, or any value
  shaped like one, into a file, a log line, a commit or a message.
- Write only under $ROOT (below). Never edit config/, state files, source or docs.
- Fail closed. If any read fails, is ambiguous, or you are unsure a value is right, do NOT invent
  or carry forward a number: record the failure as described and let the capture step turn it
  into a missed_snapshot alert. A missing day is recoverable; a wrong one silently corrupts
  goal progress.

Use this interpreter for every python step (it has duckdb, which the ledger needs):
  PY=~/.cache/all-stock-dca/goal-snapshot-venv/bin/python
If $PY does not exist, stop and report "goal-snapshot venv missing"; do not fall back to python3.

STEP 0 -- Should this run? From the repo root:
  "$PY" - <<'PY'
  from datetime import datetime
  from zoneinfo import ZoneInfo
  import dca_goal_snapshot as g
  today = datetime.now(ZoneInfo("America/New_York")).date()
  print("run" if g.should_run(today, None) else "skip", g.previous_trading_day(today, None).isoformat())
  PY
If it prints "skip", end the run silently (weekend or NYSE holiday from config/market_calendar.json).
Otherwise call the printed date SNAPSHOT_DAY. If the command errors (for example the calendar has
run out), stop and report the error; do not guess a date.

Resolve the output directory:
  ROOT="$("$PY" -c 'import dca_paths; print(dca_paths.stream_root("goal_snapshot"))')/$SNAPSHOT_DAY"
  mkdir -p "$ROOT"

STEP 1 -- Positions. Call get_equity_positions for $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER and follow
pagination to the end. Keep every equity position with quantity > 0 as {"symbol", "quantity"}
(quantity as a number, fractional shares kept exactly). If the tool errors, times out, or
pagination cannot be completed, record that in read_errors (Step 3) and skip Step 2.

STEP 2 -- Closes. For the held symbols, call get_equity_historicals with interval "day", a span
that covers SNAPSHOT_DAY (for example span "week"), regular-session bounds, at most 10 symbols per
call, and NO dividend adjustment (the snapshot values what was held at that day's actual close).
If the tool saves a large result to a file, read that file. For each symbol take the close of the
bar dated exactly SNAPSHOT_DAY. If that bar is missing, or the call errored, set the close to null
and record the symbol in read_errors. Never use a bar from another day.

STEP 3 -- Save the raw inputs to $ROOT/inputs.json:
  {"snapshot_day": "<SNAPSHOT_DAY>",
   "positions": [{"symbol": "<TICKER>", "quantity": <number>}, ...],
   "closes": {"<TICKER>": <number or null>, ...},
   "read_errors": ["<what failed, no account data>", ...]}
An account that genuinely holds nothing has "positions": [] and no read_errors -- that is a valid
snapshot, not a failure.

STEP 4 -- Capture and store. From the repo root:
  "$PY" - "$ROOT" <<'PY'
  import json, sys
  from datetime import date, datetime, timezone
  from pathlib import Path
  sys.path.insert(0, "dca-sizing-signals")
  import dca_goal_snapshot as g
  from market_data_ingest.ledger.store import LedgerStore

  root = Path(sys.argv[1])
  inputs = json.loads((root / "inputs.json").read_text())
  day = date.fromisoformat(inputs["snapshot_day"])
  errors = inputs.get("read_errors") or []

  def positions_fn(_day):
      if errors:  # any recorded read failure makes the whole day a missed snapshot
          raise RuntimeError("a read failed; see inputs.json read_errors")
      return inputs["positions"]

  def prior_close_fn(symbol, _day):
      close = inputs["closes"].get(symbol)
      if close is None:
          raise LookupError("no close for a held symbol")
      return close

  result = g.capture_previous_close(day, positions_fn=positions_fn, prior_close_fn=prior_close_fn)
  (root / "snapshot.json").write_text(json.dumps(result.rows, indent=2))
  alerts = [{"kind": a.kind, "day": a.day.isoformat() if a.day else None, "detail": a.detail}
            for a in result.alerts]
  (root / "alerts.json").write_text(json.dumps(alerts, indent=2))
  if alerts:
      print("MISSED SNAPSHOT:", alerts[0]["detail"])
      sys.exit(3)
  import dca_paths
  db = Path(dca_paths.stream_root("goal_snapshot", "ledger.duckdb"))
  with LedgerStore(db) as ledger:
      n = ledger.ingest_portfolio_snapshots(result.rows, source="robinhood_readonly",
                                            run_id=f"goal-snapshot-{day.isoformat()}",
                                            as_of=datetime.now(timezone.utc))
  print(f"stored {n} new snapshot row(s) for {day.isoformat()} ({len(result.rows)} captured)")
  PY

STEP 5 -- Report. One short message: SNAPSHOT_DAY, how many positions were captured, how many
new ledger rows were stored (0 is normal on a re-run), and the total market value. If Step 4 exited
with code 3 or any error, say MISSED SNAPSHOT with the reason from alerts.json and stop: do not
retry with guessed values. A rerun later the same day is safe; identical rows are not stored twice.
```

## How this matches the code

- `should_run` / `previous_trading_day` with `None` use the real NYSE calendar through Gate 0
  (`dca_runner.market_day_status`), so a Monday run records Friday and the Friday after
  Thanksgiving records Wednesday.
- `capture_previous_close` is all-or-nothing: any raised read, missing close, bad quantity or
  repeated symbol gives no rows and one `missed_snapshot` alert. Empty holdings give the single
  "no holdings" row, which the ledger accepts since #2328.
- `ingest_portfolio_snapshots` refuses account-named keys, so an account field can never reach the
  table even if the prompt is misfollowed. Re-ingesting identical rows is a no-op.
