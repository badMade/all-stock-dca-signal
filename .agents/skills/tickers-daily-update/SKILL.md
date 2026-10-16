---
name: tickers-daily-update
description: Incrementally refresh the Yahoo Finance daily-price CSV archive in the managed Google Drive Tickers folder. Use when maintaining the full ticker-history archive, checking its daily update progress, or preparing current history data for per-ticker DCA trigger calibration.
---

Use the configured incremental updater; do not replace it with a full-history
download. The archive's existing CSV files define the ticker universe.

1. Use the repository's `ticker_data` directory. It is the local link to the
   managed Google Drive archive, so both paths update the same CSV files.
   `DCA_TICKER_HISTORY_DIR` is an optional override only. Require
   `DCA_TICKER_DAILY_UPDATE_SCRIPT`, the maintained incremental updater; if it
   is missing or unreadable, report that exact block and make no archive changes.
2. Run `python -m scripts.run_ticker_maintenance update` from the DCA
   repository. This Python dispatcher invokes the updater in bounded batches,
   stops at `ALL_DONE`, or reports a non-progressing/throttled run after the
   safety limit.
3. On completion, verify its `ticker_update_event.json`. Its
   `changed_active_tickers` list is the handoff to the separate
   `Codex-per-ticker-calibration` task. To test that handoff, run
   `python -m scripts.run_ticker_maintenance event-calibrate
   --ticker-update-event <event-path>`; it recalculates only if the event names
   an active ticker.
   The calibration task also has its own independent nightly `--force` run.
4. Read the generated `_daily_update_log.txt` entry and report only total
   tickers, update attempts, remaining count, and notable errors.
5. Do not delete, blacklist, or truncate ticker CSVs. Persistent Yahoo
   failures remain for the next daily run.
6. Do not place orders or use broker data. This is archive maintenance only.

The updater path is deliberately configured rather than hard-coded. The source
path bundled with the original skill may differ between machines.
