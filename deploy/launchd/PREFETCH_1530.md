# 3:30 PM yfinance prefetch (launchd)

Runs `yfinance-prefetch/fetch_yfinance_fallback.py` at 15:31 ET on weekdays,
outside any Cowork session, and writes the 3:30 slot's yfinance fallback
payload where that slot's Gate 5 can pick it up.

## Why

On 2026-09-22 the 3:30 run aborted at Gate 5: no Finance connector, and that
session had no yfinance either. The 12:30 run three hours earlier built a
yfinance payload for 63 tickers without trouble. Whether a session can fetch
yfinance varies by session, so the fetch runs here instead, on a schedule and
in its own venv. `scripts/generate_buy_candidates.py` already accepts the
labeled `sources.yahoo_finance_yfinance` block this produces.

## Why 15:31, not earlier

Gate 5 anchors on the start-stamped 15:15 bar, which only completes at 15:30.
The fetch script keeps every bar stamped at or before the decision time, so a
run before 15:30 would hand the generator a still-forming 15:15 bar as if it
were complete. A bar stamped 15:30 may also be present; the generator anchors
at 15:15 (`near_close_dca.completed_bar_anchor`) and never reads it.

## Files

| File | Role |
| --- | --- |
| `prefetch_1530.sh` | Resolves the slot's `$ROOT` via `dca_paths`, builds Gate 5's universe from `config/tickers.json` (active, minus excluded, whole-share and GFUZ), runs the fetch, and renames the file into place atomically |
| `com.all-stock-dca.prefetch-1530.plist.template` | launchd agent template (a `.template` because `.gitignore` excludes rendered `deploy/launchd/*.plist`), Mon to Fri 15:31 local time |

## Setup (on the Mac that runs the Cowork tasks)

```bash
cd "$DCA_REPO/yfinance-prefetch" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp "$DCA_REPO/deploy/launchd/com.all-stock-dca.prefetch-1530.plist.template" ~/Library/LaunchAgents/com.all-stock-dca.prefetch-1530.plist
# Edit the copy: replace the three REPLACE_WITH_* values.
launchctl load ~/Library/LaunchAgents/com.all-stock-dca.prefetch-1530.plist
```

- `DCA_CRON_TRACKING_ROOT` must be the same value the Cowork sessions use.
  launchd does not inherit your shell, and without it `dca_paths` resolves to
  the checkout's own `cron_tracking/`, where the 3:30 session never looks.
- `StartCalendarInterval` is local time. It assumes the Mac is on US Eastern.
- Output goes to
  `stream_root('ce16ba6b', 'scheduled_runs', '<YYYY-MM-DD>_1530ET')/yfinance_fallback_<YYYY-MM-DD>_1530.json`.
  Log: `/tmp/dca-prefetch-1530.log`.

## Verify

Run `DCA_REPO=... DCA_CRON_TRACKING_ROOT=... bash deploy/launchd/prefetch_1530.sh`
by hand after 15:30 on a trading day. To recover an earlier slot after the ET
date has rolled, add `DCA_TRADING_DAY=<YYYY-MM-DD>` for the intended regular
3:30 session day only (never a weekend, holiday, or 1:00 PM early-close day).
The file should appear under that day's `ce16ba6b/scheduled_runs/<date>_1530ET/`
with about 63 tickers and few or no `ticker_errors`.

## 3:30 prompt block

Nothing reads the file until the live 3:30 task is told to. Paste this into
Gate 5, directly after the sentence beginning "If Finance is unavailable,
stale, incomplete, or unresolved for ANY ordinary ticker":

> PREFETCHED YFINANCE FALLBACK: a launchd job writes $ROOT/yfinance_fallback_<YYYY-MM-DD>_1530.json at 15:31 ET, outside this session. Use it only when the rule above calls for yfinance, and prefer it to fetching yfinance yourself. Before using it, check that the filename's date is today's ET date and that the file was modified after 15:30 ET today. If it is missing, poll every 30 seconds for up to 3 minutes. If it is still missing, run `DCA_REPO=<the checkout Gate 2 resolved> bash "<that checkout>/deploy/launchd/prefetch_1530.sh"` once, or `DCA_TRADING_DAY=<the intended regular 3:30 session day> DCA_REPO=<the checkout Gate 2 resolved> bash "<that checkout>/deploy/launchd/prefetch_1530.sh"` when rerunning after the ET date has rolled. Do not use that override for weekends, holidays, or 1:00 PM early-close days: this helper only writes fallback artifacts for dates when the 3:30 slot itself is meant to exist. If that command says today's date is outside config/trading_window.json, treat the missing file as expected on this day and continue under the existing no-fallback rule instead of retrying. If the file is still missing after a successful in-window retry, fetch yfinance evidence for EVERY ordinary ticker yourself before invoking the generator, using the same start-stamped 15-minute bars from at least 09:30 AM ET, previous completed-session daily bar, current quote, timestamps, and open-bar omission the prompt already requires; store it only under `sources.yahoo_finance_yfinance` with any yfinance fetch exceptions recorded under the matching `ticker_errors.yahoo_finance_yfinance` entries. Never copy yfinance contents under `finance` or `robinhood`. A ticker listed in `ticker_errors.yahoo_finance_yfinance` counts as a yfinance fetch failure for that ticker, not as a missing ticker.

Then re-capture `claude/dca-executor-prompts/live-mirror/1530ET-330-final-buying-run.txt`
so the mirror matches, or `scripts/check_live_prompt_drift.py` will report the
slot as drifted.
