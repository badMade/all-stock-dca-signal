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
   | `finalize --trading-day D --slot S --plan F --orders-pages F --fresh-quotes F --fresh-spreads F --buying-power USD` | a plan, a poll-2 envelope, `{"SYMBOL": price}`, `{"SYMBOL": {"bid", "ask"}}`, fresh buying power | `{"status": "PROCEED", "SHADOW_ONLY" or "ABORT", "to_place", "would_place", "skipped", "errors"}` via `reconcile` then `revalidate_for_placement` |

2. **No single-file build for the Perplexity sandbox.** The cloud fallback runs `python3 backup_planner.py ...`,
   but the import closure of `dca_order_plan` is about 4.3 MB (`near_close_dca.py` alone is 2.4 MB). It needs
   a separately reviewed extraction of a small shared ladder core first (ADR, "Cloud single-file build").

## Placement gates (added 2026-10-07 after review)

- **Only `PROCEED` places.** A plan written by `compute` finalizes as `SHADOW_ONLY` (would-be orders reported,
  nothing placed) because `compute` does not run the stream generator's trend and data-confidence gates. The
  switch is `scripts.backup_planner.COMPUTE_PLACEMENT_ENABLED`, off until `compute` is rebuilt on
  `scripts/stream_candidates.py`.
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
