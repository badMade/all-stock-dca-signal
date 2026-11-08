# Market-open slot: move from 09:35 to a 09:32 fire (design spec, not implemented)

Status: PROPOSAL. Nothing here changes live behaviour. Written 2026-11-08 after the
2026-11-06 09:31:54 fire aborted at Gate 1 (`GATE1_OFF_WINDOW`) because the prompt and
`dca_runner.SLOTS["0935"]` both open the window at 09:35.

## Operator decisions (2026-11-08)

- Fire at **09:32 ET**; the Mac payload publisher runs at **09:31 ET**.
- Gate 5 decision price is the **live quote at fire time**, not a completed bar.

## Why this is not a one-line change

Gate 5 grades the completed 09:30 **five-minute** bar (`docs/market_open_signal_entry.md`,
`intraday_bars` interval=5minute, "09:30 through the 09:35 decision anchor"). That bar does
not exist before 09:35, so `near_close_dca._compute_signal_timestamps` /
`completed_bar_anchor` / `latest_bar_at_or_before` have nothing to return at 09:32.
A live-quote decision price is a new signal path in the shared core, not a schedule edit.

## Required changes (all must land together)

1. `dca_runner.SLOTS["0935"]`: nominal and `window_start_et` -> (9, 32); window end stays (10, 20).
   Gate 3/Gate 7 clock rechecks and Gate 4's interlock read the same bounds.
2. `market_open_dca.py`: `MARKET_OPEN_LIVE_SCHEDULED_FOR_ET`, `MARKET_OPEN_LIVE_WINDOW_START_ET`,
   `decision_time_et` -> 09:32.
3. Gate 5 signal path: when no completed 5-minute bar exists at the decision time, use the
   live quote (last vs previous close and open) as the decision price. Needs its own tests and
   a shadow comparison against the 09:35 result before it can size live orders.
4. `config/market_open_dca.json`, `config/scheduled_tasks.json`, `schemas/market_open_run_classification.schema.json`,
   `config/run_classification.json`: schedule/window strings.
5. `deploy/launchd/robinhood_payload.sh` and its plist template: publish at 09:31 for this slot;
   payload `intraday_bars` will be empty or partial at that time, so the payload contract must allow it.
6. Live-mirror prompt `claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt`
   (Gates 1, 4, 6.5, 7) and the external routine, which must be re-pasted; `check_live_prompt_drift`
   will flag any mismatch. The Perplexity backup task (4412c3c4, fires 9:55) and its prompt/planner
   must be reviewed together.
7. Scheduler: set the routine's cron to fire at 09:32 (external, outside this repo).
8. Tests and docs: roughly 40 files reference 09:35 (largest: `tests/test_dca_runner.py`,
   `tests/test_whole_share_wiring.py`, `docs/schedule_manifest.md`).

## Risks

- The first minutes after the open have wide spreads; a live-quote price can differ materially
  from the 5-minute bar close the ladder was calibrated on. Run shadow for several sessions first.
- If the scheduler fires late (as on 2026-10-27), the run still has until 10:20.

## Recommended order

1. Implement item 3 behind a flag, shadow-only, compare to the 09:35 result for a week.
2. Land items 1, 2, 4, 5, 6 and 8 together with the flag on.
3. Change the external schedule (item 7) last.

Until then the safe state is the routine firing at 09:35 or later.

## Shadow phase (implemented)

`market_open_quote_shadow.py` records, per ticker, the live-quote decision drop versus the previous
close next to the 09:30 five-minute bar's drop, plus the gap in percentage points. Enable it with
`--quote-shadow` on `scripts/generate_market_open_candidates.py` (default off, market-open slot only).
It appends to `<cron root>/market_open_quote_shadow/<YYYY>.jsonl`, is best-effort, and never changes
the emitted Gate 5 document, sizing, state or orders.

To start collecting, add `--quote-shadow` to the Gate 5 command in the live prompt (external re-paste,
then `check_live_prompt_drift`). Review the JSONL after about a week: if `drop_gap_pct_points` is
small and rarely crosses a ladder tier, proceed to the remaining changes above. Note the payload at
09:35 carries a ~09:35 quote, so it slightly understates a true 09:32 quote's divergence.
