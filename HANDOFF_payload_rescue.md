# Handoff: 13:30 payload refusal and rescue fix (2026-10-26)

## Incident
- 13:30 ET payload refused at 13:36:02 ET: coverage 83/91 (91%), floor 95% (`DEFAULT_MIN_COVERAGE`, `scripts/robinhood_payload.py`).
- Failed tickers, all "no data returned for this ticker": DFTX, TRIN, AAAU, SPCX, EVR, LUNR, IRDM, DXYZ.
- The empty 13:57 plan followed from the refusal. It was not a "nothing dropped 0.1%" result.
- 09:35 was also refused earlier the same day (chunks 4 and 10 dead on both attempts). 10:30, 11:30 and 12:30 published normally.

## Ruled out
- Not the "no orders since Friday's close" theory. Previous close is Friday's price bar from market data (`validate_ticker` checks it against `_previous_trading_day`, which maps Monday to Friday). Order history only feeds caps.
- Not bad symbols. Live Robinhood quotes for all 8 returned full data (previous close dated 2026-10-23).

## Why no `chunk_0` log exists
- `deploy/launchd/robinhood_payload.sh` deletes `$WORK` and every `chunk_<i>.log` on exit.
- Only attempts that returned no reply are kept, under `<payload path>.chunk_logs/`. That keep-logs code is dated 2026-10-26 and may not have been on the Mac at 13:30.
- The 13:30 chunks replied, so nothing would have been kept anyway.

## Fix
- Branch `claude/rescue-no-data-tickers`, one commit.
- The rescue pass now also re-fetches tickers a replying chunk left out or answered with a per-ticker `error`, in `DCA_RESCUE_CHUNK_SIZE` requests (default 2), before the merge. A good entry is never overwritten.
- Tests added in `tests/test_robinhood_payload.py`: omitted, error, and still-missing-stays-failed.
- Not yet run anywhere.

## Cloud session (task_f46c38bd)
Runs `pytest tests/test_robinhood_payload.py`, `ruff check .`, and the full suite. It fixes failures, updates the rescue paragraph in `docs/robinhood_payload.md`, and opens a DRAFT PR.

## Still open
- Merge the PR, then pull on the Mac so the launchd job picks it up.
- Confirm the Mac has the keep-logs code, or the next failure leaves no log.
- Not covered by the fix: a ticker that replies with data but fails validation (stale quote, bad bar).
- Not checked: whether the 14:30 payload passed, and whether the same 8 tickers failed at 09:35 (`robinhood_payload_2026-10-26_0935.refused.json`, Mac repo folder).
