# Perplexity backup prompts (plan-first design)

Six generated prompts for the standing Perplexity backup executor, one per scheduled task:

| File | Task | Fires (ET) | Backs up slot |
| --- | --- | --- | --- |
| `0955ET-935-backup.txt` | `4412c3c4` | 09:55 | 0935 |
| `1050ET-1030-backup.txt` | `e028361e` | 10:50 | 1030 |
| `1150ET-1130-backup.txt` | `e3590042` | 11:50 | 1130 |
| `1250ET-1230-backup.txt` | `062605c6` | 12:50 | 1230 |
| `1350-1450ET-1330-1430-backup.txt` | `54ab24d2` | 13:50 and 14:50 | 1330 and 1430 (the clock decides) |
| `1545ET-1530-backup.txt` | `f83b8d62` | 15:45 | 1530 |

Design: `docs/adr-plan-first-backup-executor.md`. These files are generated, never hand-edited:

```bash
python3 -m scripts.generate_perplexity_backup_prompts --write   # regenerate
python3 -m scripts.generate_perplexity_backup_prompts --check   # exit 1 on drift (also a test)
```

## Status: NOT DEPLOYABLE YET (one blocker open)

Like the `live-mirror/` prompts, these are a repo copy. Nothing here changes a live task until a human
repastes a prompt into its Perplexity task. Do not repaste until both blockers below are closed and
shadow mode (ADR phase 6) has run for at least five trading days.

1. **CLOSED 2026-10-07: `scripts/backup_planner.py` now exists** and implements this contract (library logic
   in `dca_order_plan.py`, tests in `scripts/tests/test_backup_planner.py`). Every subcommand prints one JSON
   document and exits 0 when usable, 2 on any failure (fail closed, nothing placed). It runs from a repo
   checkout, so today only the Mac paths can use it; the sandbox form still needs blocker 2.

   | Command | Input | Output |
   | --- | --- | --- |
   | `plan --source mac_plan --trading-day D --slot S` | the plan file at `dca_order_plan.order_plan_path(D, S)` | `{"status": "VALID_PLAN" or "NO_VALID_PLAN", "plan", "errors"}` via `load_plan` |
   | `compute --trading-day D --slot S --quotes F --orders-pages F --buying-power USD --now T` | quotes file `{"quotes": [...]}`, a poll-1 orders envelope (from the ISO-week Monday), fresh buying power | `{"status", "plan", "errors"}` via `compute_plan_from_file` with the Normal tier |
   | `finalize --trading-day D --slot S --plan F --orders-pages F --fresh-quotes F --fresh-spreads F --buying-power USD --now T` | a plan, a poll-2 envelope, `{"SYMBOL": price}`, `{"SYMBOL": {"bid", "ask"}}`, fresh buying power | `{"status": "PROCEED", "SHADOW_ONLY" or "ABORT", "to_place", "would_place", "skipped", "errors"}` via `reconcile` then `revalidate_for_placement`; for a computed plan, first re-derives the trailing-7-day figure from poll 2 and ABORTs (`kill_switch_engaged_since_plan`) if the kill switch is now tripped and any order is above 1x (`--now` must be on the trading day in ET) |

2. **CLOSED 2026-10-08: the sandbox build exists, and these prompts now use it.** `python -m scripts.build_backup_planner
   --out PATH` builds `backup_planner.pyz` (about 1.4 MB, deterministic, stamped with the source commit; `--check`
   reports drift). The prompts run it in the sandbox (`python3 backup_planner.pyz ...`), verify it with
   `--build-info` first, and record the build in the state record. **How the file reaches the task is still
   undecided**: the prompts assume it is attached to the task's project files, a setup step you must confirm.

## Which slots take the safety-gate path

All seven, read from `scripts.backup_planner.GATED_SLOTS` when the prompts are generated. Each prompt asks the planner
<<<<<<< HEAD
for the symbols to fetch (`backup_planner.pyz universe`), dumps the raw `get_equity_historicals` (5-minute under `historicals`, the previous
session's daily bar under `daily`) and `get_equity_quotes` responses to `raw.json`, runs `assemble-market-data`, then `compute --market-data market.json
=======
for the symbols to fetch (`backup_planner.pyz universe`), dumps the raw `get_equity_historicals` and `get_equity_quotes`
responses to `raw.json`, runs `assemble-market-data`, then `compute --market-data market.json
>>>>>>> origin/main
--deployed-last-7d-usd auto`, so the primary's own Gate 5 decides what to buy and the planner derives the kill-switch
figure from the order history. No prompt names a stock or mentions Yahoo; the stocks live in the planner's configuration.

Slot 15:30 runs the final-buy engine; the six executor-class slots run the stream CLI path, scored identically from
Robinhood (`scripts/backup_stream_specs.py`). 10:30 and 12:30 are not special: no pre-flight, no Finance source, no
per-ticker exception. A slot dropped from `GATED_SLOTS` falls back to a quotes-only prompt that reports `shadow_only`.

## Placement gates (added 2026-10-07 after review)

- **Only `PROCEED` places.** A plan written by `compute` finalizes as `SHADOW_ONLY` (would-be orders reported,
  nothing placed). `compute --market-data` now runs the primary's own Gate 5 for all seven slots (15:30 through
  `generate_buy_candidates`, the six executor-class slots through `scripts.stream_candidates`), so that gap is
  closed everywhere. The switch `scripts.backup_planner.COMPUTE_PLACEMENT_ENABLED` stays off until the shadow run
  has proven the output.
- A Claude plan is reconciled against this slot's window only (later slots may top up); a computed plan excludes
  any ticker bought earlier that day.
- Buying power and a per-ticker bid/ask spread are required inputs and fail closed.

## Decisions these prompts encode that need confirmation

- **Latest placement time** is the next canonical slot's start (10:30, 11:30, 12:30, 13:30, 14:30, 15:30; 16:00
  for the final slot), so a backup that fires inside a slot's window can still finish before the next slot's gates.
- **Normal tier only.** The backup never escalates its budget tier; it applies $180 per day and $900 per week.
- **Time sanity** accepts a fire from 10 minutes before its fire time until the latest placement time.
- **Two polls only.** Claude's own prompts also re-fetch order history before every order; the backup does
  not, so a batch that runs long relies on the per-order quote check and the one-order-per-ticker rule.
- **Attribution.** Backup orders reuse the planner's `dca-runner-<slot>-<day>-<TICKER>` reference (the plan's
  `ref_id`), so Claude's and the backup's orders are indistinguishable by reference alone; the state record is
  the only attribution. Broker-side deduplication on a reused reference is unverified (ADR decision D6).

## Placeholders to fill when repasting (2026-10-08)

The prompts no longer contain an account number. Before pasting, set these in the Perplexity task:

| Placeholder | Meaning |
|---|---|
| `${ROBINHOOD_AGENTIC_ACCOUNT_LAST4}` | Last 4 digits the account lookup must match; the run stops if not exactly one tradable account ends in them |
| `${PPLX_RUN_STATE_DIR}` | Directory for the run-state JSON and `submitted_orders.csv` (the project files `cron_tracking/<task id>` folder) |
| `${DCA_ALERT_EMAIL_ADDRESS}` | Alert address, unchanged |
