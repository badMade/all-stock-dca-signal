# Handoff: payload rescue for tickers a replying chunk left out or errored

Start Claude Code in this repo and say: "Read HANDOFF_payload_rescue.md and continue." The change below is merged to `main` via PR 1967 (badMade/all-stock-dca). Originating session: https://claude.ai/code/session_01CZmLj4zSQEtD2X3vEuof9d

## Goal
Stop the Robinhood payload build from refusing a slot when a chunk replies but leaves tickers out or marks them `error`, while live quotes for those tickers are fine.

## Incident
2026-10-26, 13:30 ET build refused at 83/91 (91%, floor 95%). Eight tickers (DFTX, TRIN, AAAU, SPCX, EVR, LUNR, IRDM, DXYZ) returned `no data returned for this ticker`. Their chunks had replied, so the existing dead-chunk rescue never ran. The empty plan at 13:57 followed from that refusal.

## What changed (merged)
- `deploy/launchd/robinhood_payload.sh`: the rescue pass now also targets tickers that are absent from a replying chunk or carry a per-ticker `error`. They are re-asked in requests of `DCA_RESCUE_CHUNK_SIZE` (default 2), reusing `run_chunk` (retry and build deadline apply), then folded back into that chunk's reply before the merge. Dead chunks are rescued as before.
- A ticker that replied with data is never re-fetched. A rescue reply that is again missing or an error leaves the original entry in place, so the rescue cannot make a ticker worse.
- Logs: `rescue chunk=<i> tickers=<n> size=<s>` and `rescue chunk=<i> replied=<n>`. Rescue parts log timing as `timing chunk=<i>.r<k> ...`.
- Bash 3.2 compatible for macOS: no `wait -n`, no bash 4 features.
- `docs/robinhood_payload.md`: new bullet under "Honest limits" describing this rescue.
- `tests/test_robinhood_payload.py`: `test_launcher_rescues_tickers_a_replying_chunk_left_out_or_errored` (omitted and error modes) and `test_launcher_rescue_never_makes_a_still_missing_ticker_worse`. `test_launcher_has_no_deadline_by_default_and_never_skips_a_chunk` now counts only whole-chunk timing lines (`timing chunk=\d+ attempt=`), because the stub's empty replies trigger rescue for every ticker.

## Verified (cloud session, not local)
- `tests/test_robinhood_payload.py` green, `ruff check .` clean, full `python -m pytest` green (exit 0).
- CI check suites on the PR head completed without a failure before the merge.

## Not verified
- No live run. The first real signal is the next scheduled payload build: look for `rescue chunk=` lines in `/tmp/dca-robinhood-payload.log` and whether coverage stays at or above 95%.
- Whether the 8 ambiguous tickers were a stub-able "no data" reply or a genuine worker truncation is unknown. If rescue requests also return `no data`, the cause is upstream of chunking (worker prompt or MCP quote tool), not the launcher.

## Open items
1. Watch the next few builds for `rescue chunk=` frequency. Frequent rescues mean first-pass chunks are lossy and chunk size or the worker prompt needs work.
2. The coverage floor (95%) is unchanged by design. Do not lower it to absorb misses.
3. Rescue adds worker calls on a bad build (up to ceil(missing/2) extra requests per affected chunk, parallel by `DCA_CODEX_PARALLEL`). Check total build time against the sessions' 12-minute wait using the `timing total` line.
4. Housekeeping: a worker's own log is kept beside the refusal marker only for requests that returned no reply. Replies that were present but incomplete leave no log, so a diagnosis of "why did the worker omit these" needs a rerun.
