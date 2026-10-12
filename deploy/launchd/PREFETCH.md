# yfinance prefetch for every live slot (launchd)

One launchd agent runs `deploy/launchd/prefetch_slot.sh auto` one minute after
each live slot's decision time (09:36, 10:31, 11:31, 12:31, 13:31, 14:31,
15:31 ET, weekdays), outside any Cowork session. Each run writes that slot's
labeled yfinance fallback payload, and the slot's Gate 5 merges it into its
`--market-data` payload.

## Why

- **The Robinhood-primary slots had no fallback.** Their only data path was
  the live session pulling about 64 tickers' 5-minute bars through MCP tool
  output (9:35, 10:30, 11:30, 1:30 and 2:30). That exceeded the tool output
  cap (2026-09-15) and the session's token budget (2026-09-18), and sessions
  without a data path aborted (Notion DCA Issues: `output_size_limit`,
  `intraday_bars_unavailable`, `token_limit_exceeded`,
  `market_data_unavailable`).
- **Whether a session can reach yfinance varies by session.** On 2026-09-22
  the 12:30 session (then Finance-primary) built a yfinance payload without trouble, and the 3:30
  session three hours later couldn't. Fetching here, on a schedule and in its
  own venv, removes that variable.

`scripts/stream_candidates.py` (09:35–14:30) and
`scripts/generate_buy_candidates.py` (15:30) accept the
`sources.yahoo_finance_yfinance` block as a fallback:

| Primary | Slots | Switches the whole universe to yfinance when |
| --- | --- | --- |
| Robinhood | 09:35, 10:30, 11:30, 12:30, 13:30, 14:30 | Robinhood failed as a whole: listed in `source_errors`, absent, or no scored ticker has data fresh for the slot. One ticker's gap does **not** switch |
| Finance | 15:30 | Finance is unavailable, stale, incomplete or unresolved for any ordinary ticker |

The whole universe always moves together; sources are never mixed. Policy:
`config/scheduled_tasks.json` → `market_data_source_policy`.

## What each run fetches

`prefetch_plan.py` builds the plan from the slot's own Gate 5 spec, so it
can't drift from what the generator scores:

| Slot | Decision | Bars | Universe |
| --- | --- | --- | --- |
| 09:35, 10:30, 11:30, 12:30, 13:30, 14:30 | slot time | 5-minute | stream config's tickers minus excluded (GFUZ included) |
| 15:30 | 15:30 | 15-minute | `config/tickers.json` active minus excluded, whole-share and GFUZ |

It runs one minute after the decision time for a reason: Gate 5 anchors on
the last completed start-stamped bar, which only closes at the decision time.
The fetch keeps every bar stamped at or before that time, so an earlier run
would hand the generator a still-forming bar as if it were complete.

Output:
`stream_root('yfinance_prefetch', '<YYYY-MM-DD>', 'yfinance_fallback_<YYYY-MM-DD>_<HHMM>.json')`.
This is deliberately **not** the slot's run directory. Creating
`<stream>/<day>_<slot>ET/` before the session fires would turn a missed slot's
`no_evidence` into `evidence_incomplete` in `scripts/verify_schedule.py`.

A run skips cleanly (exit 0) in these cases:
- outside `config/trading_window.json`
- a market holiday
- a 1:00 PM early close, for slots at or after 13:00
- `auto` with no slot due: the decision time passed less than 1 or more than
  14 minutes ago
- another run is already publishing, or the file already exists

A fetch failure exits non-zero without publishing anything.

## Files

| File | Role |
| --- | --- |
| `prefetch_slot.sh` | Takes `<HHMM>` or `auto`, runs the plan, fetches into a temp file, publishes it with `ln` (never overwrites) under a per-slot lock |
| `prefetch_plan.py` | Resolves the slot, checks the market day and trading window, and prints the root, decision time, interval and tickers |
| `prefetch_1530.sh` | Compatibility wrapper for `prefetch_slot.sh 1530` (#1513) |
| `com.all-stock-dca.prefetch.plist.template` | launchd agent, 35 calendar entries (7 slots × Mon–Fri). A `.template` because `.gitignore` excludes rendered `deploy/launchd/*.plist` |

## Setup (on the Mac that runs the Cowork tasks)

```bash
cd "$DCA_REPO/yfinance-prefetch" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sed -e "s#REPLACE_WITH_REPO_PATH#$DCA_REPO#g" -e "s#REPLACE_WITH_CRON_TRACKING_ROOT#$DCA_CRON_TRACKING_ROOT#g" \
  "$DCA_REPO/deploy/launchd/com.all-stock-dca.prefetch.plist.template" > ~/Library/LaunchAgents/com.all-stock-dca.prefetch.plist
launchctl load ~/Library/LaunchAgents/com.all-stock-dca.prefetch.plist
```

- `DCA_CRON_TRACKING_ROOT` must be the value the Cowork sessions use. launchd
  doesn't inherit your shell. Without it, `dca_paths` resolves to the
  checkout's own `cron_tracking/`, where no session looks.
- `StartCalendarInterval` is local time and assumes the Mac is on US Eastern.
  `auto` reads the ET clock itself, so a wrong offset makes it skip ("no slot
  is due") rather than fetch for the wrong slot.
- The agent starts through the checkout's `.venv/bin/python`, not `/bin/bash`.
  macOS privacy protection (TCC) grants `~/Documents` access per binary: a
  launchd agent running `/bin/bash` on a script there fails with
  "Operation not permitted" (exit 126). The venv Python is what the existing
  ticker-update agent already runs, and its child processes inherit that
  access. So the checkout's `.venv` must exist.
- The agent runs whatever `prefetch_slot.sh` the checkout has. If the
  checkout is switched to a branch without it, runs fail (logged) until it's
  back.
- Log: `/tmp/dca-prefetch.log`.
- If you installed the earlier 3:30-only agent, remove it:
  `launchctl unload ~/Library/LaunchAgents/com.all-stock-dca.prefetch-1530.plist`
  and delete that file.

## Verify

After a slot's decision time on a trading day, run
`DCA_REPO=... DCA_CRON_TRACKING_ROOT=... bash deploy/launchd/prefetch_slot.sh <HHMM>`
by hand. To recover a slot after the ET date has rolled, add
`DCA_TRADING_DAY=<YYYY-MM-DD>`, only for a real session day of that slot. The
file should appear under `yfinance_prefetch/<date>/` with about 63 tickers and
few or no `ticker_errors`.

## Prompt contract

Every `claude/dca-executor-prompts/live-mirror/*.txt` Gate 5 carries a
"PREFETCHED YFINANCE FALLBACK" paragraph. It tells the session to:
- merge the file when it exists, is dated today, and was modified after the
  decision time;
- for Robinhood slots, record `robinhood` in `source_errors` only when its
  own fetch was attempted and failed; when the fetch would exceed a limit,
  skip it, omit `sources.robinhood` and leave `source_errors` empty rather
  than hand-fetch bars in pieces. `source_errors` means "attempted and
  raised": the generator already treats an absent `robinhood` key as a
  whole-source outage and falls back to yfinance, so a skipped fetch must
  not be recorded as a broker data failure (2026-10-05, 9:35);
- when the file is missing, poll for up to 3 minutes, then run
  `prefetch_slot.sh <HHMM>` once;
- when neither source yields data, abort as `not_submitted_error
  --error-class market_data_unavailable`.

`tests/test_config_invariants.py::test_every_live_slot_gate_5_merges_its_prefetched_yfinance_fallback`
pins the paragraph in every mirror. Editing a mirror changes nothing live
until each task is repasted (see `docs/schedule_verification.md`, "Deploying
the prompt change").
