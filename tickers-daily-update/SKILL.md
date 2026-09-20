---
name: tickers-daily-update-DEPRECATED
description: "DEPRECATED / DO NOT USE -- superseded by .claude/skills/tickers-daily-update/SKILL.md. Retained for historical context only."
---

# ⚠️ DEPRECATED — DO NOT FOLLOW THE INSTRUCTIONS BELOW

**Superseded by [`.claude/skills/tickers-daily-update/SKILL.md`](../.claude/skills/tickers-daily-update/SKILL.md).
Use that skill instead. This file is retained only as historical context for
where the archive-update workflow came from.**

Why this version was retired:

- **It hard-codes machine-specific absolute paths** — both the archive
  (`/Users/geoffplymale/My Drive/Tickers`) and the updater script
  (`/Users/geoffplymale/Documents/Claude/Projects/Code/stock-history-downloader-updates/incremental_update.py`).
  That violates `AGENTS.md`'s "never hard-code local absolute paths" rule, and
  the paths are wrong on this machine anyway (this checkout lives under
  `/Users/geoffreyplymale/…`, a different home directory), so following them
  would fail or silently target the wrong location.
- **The canonical replacement resolves both paths properly**: the archive via
  `dca_ticker_paths.configured_ticker_history_dir()` (repo-relative
  `ticker_data`, overridable with `DCA_TICKER_HISTORY_DIR`), and the updater
  via the required `DCA_TICKER_DAILY_UPDATE_SCRIPT` environment variable,
  routed through `python -m scripts.run_tickers_daily_update`.
- **Only the replacement writes the `ticker_update_event.json` handoff** that
  `scripts/daily_per_ticker_calibration.py` consumes to rebuild
  `config/per_ticker_sizing.json`. Running this older flow would refresh CSVs
  without triggering recalibration.

Deprecated 2026-08-03 during a docs-vs-code audit. Everything below this line is
the original, superseded text.

---

You maintain a large local archive of Yahoo Finance daily price history: one CSV per ticker (~6,058 tickers) saved in the user's Google Drive at "/Users/geoffplymale/My Drive/Tickers" (in the sandbox this same folder is reachable via the workspace bash tool at `/sessions/<session>/mnt/My Drive/Tickers` — use whichever path form your Read/Write/Bash tools expect; if "My Drive" is not already connected/mounted, request access to it first via the Cowork directory-access tool before proceeding).

There is already a maintenance script for this at:
/Users/geoffplymale/Documents/Claude/Projects/Code/stock-history-downloader-updates/incremental_update.py

What this script does (don't rewrite it, just run it): it lists every {TICKER}.csv already saved in the Tickers folder (that file listing IS the ticker universe — no need to re-fetch any master list), fetches a small recent window (period="10d") per ticker from Yahoo via yfinance in batches, and merges fresh rows into each existing CSV — replacing any rows for dates that already exist (self-healing, in case an earlier run got truncated/corrupted data) and appending new trailing trading days. It never touches the older full-history rows outside that 10-day window. It is time-budgeted (~30s per invocation) and resumable via a `_update_progress.json` progress file that resets each new calendar day, so you MUST invoke it repeatedly in a loop until it prints "ALL_DONE" — a single invocation only processes part of the list.

Steps:

1. Ensure dependencies are present (idempotent, fast if already installed):
   pip3 install --break-system-packages --quiet yfinance pandas
2. Loop: run `cd "/path/to/Code/stock-history-downloader-updates" && python3 incremental_update.py` repeatedly (each call has its own ~30-45s internal time budget and will print a line like "DONE_THIS_RUN updated=N remaining=M"). Keep calling it until it prints "ALL_DONE" (all tickers refreshed for today) OR until remaining stops decreasing across 3-4 consecutive calls (this means Yahoo is throttling this session hard for the day — see the note on persistent stragglers below). Cap yourself at roughly 40 invocations total to avoid an unbounded loop; if still not done after that, stop and report partial progress honestly.
3. Some tickers may persistently fail every day with "possibly delisted" or empty-batch errors — this is expected for a small tail of illiquid/warrant/SPAC-type symbols and sometimes reflects genuine Yahoo-side throttling for specific symbols that built up over a prior heavy-usage session. Do NOT blacklist or delete anything — the script already handles this correctly by just leaving them for the next day. If the SAME set of tickers has failed for many consecutive days (you can eyeball this against "/Users/geoffplymale/My Drive/Tickers/_thin_data_warning.txt" if it exists), note that in your summary but don't take further action.
4. When done (or when you decide to stop for the day per the cap above), write/update a short one-line-per-run log entry to "/Users/geoffplymale/My Drive/Tickers/_daily_update_log.txt" (append, don't overwrite) with today's date, how many tickers were updated, how many remained un-refreshed, and any notable errors.
5. Report a brief summary back to the user: total tickers, how many refreshed today, how many still stuck (and whether that set changed from yesterday if you can tell from the log), and how long it took.

Keep the report concise — a few sentences, not a full transcript of every batch.
