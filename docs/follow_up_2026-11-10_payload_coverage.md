# Follow-up: 9:35 payload refused on coverage floor (2026-11-10)

Status: decided 2026-11-11 (operator). Monitoring: close this follow-up after about
5 clean trading days of payload builds, counted from 2026-11-11. Reopen it if a build is
refused on coverage again.

What happened: the Robinhood payload job for the 0935 slot got 429 Too Many Requests on many chunks, so 60/122 tickers (49%) validated. The publisher's 95% coverage floor (DEFAULT_MIN_COVERAGE) refused to publish, so Gate 5 had no market data and the slot placed no orders. The 10:30 build was refused the same way at 80%. The 11:30-15:30 builds, on the same settings, published 120-122 of 122. Evidence: cron_tracking/robinhood_payload/2026-11-10/*.refused.json and the chunk_logs folder.

## Mitigations merged 2026-11-11

- PR #2218: retry a refused build once, log every failed worker to `<payload>.failures.jsonl`
  (attempt, HTTP status, running Codex and payload-worker counts, last usage snapshot), and
  lower `DCA_CODEX_PARALLEL` from 8 to 6.
- PR #2221: back off before retrying a chunk that died with a 429. It replaced PR #2216, which
  was closed unmerged.

First result: on 2026-11-11 all seven builds (09:35 through 15:30) validated 122 of 122, with
no `.failures.jsonl` and no 429 in any chunk log. One clean day is encouraging, not proof.

## Reopened 2026-11-16, retry vs deadline fix 2026-11-19

The 14:30 build on 2026-11-16 was not a 429: chunk 3 timed out at 240 s and the last 26 tickers came back empty at about
601 s, the `DCA_BUILD_DEADLINE_SECONDS=600` cutoff, so 96/122 was refused. The cause of the slow earlier chunks is still
unknown (a lock-step wave stalled by one timed-out chunk is the working hypothesis; not changed here). What is fixed: the
whole-build retry restarted its own 600 s deadline, so such a refusal could be rebuilt for up to another 600 s and land after
the executor stopped waiting. The deadline now counts from the first attempt, and the retry is skipped
(`not retrying: the build deadline ...`) when it cannot start a useful attempt before then. Fast refusals (the 11-10 429
shape) still get their one rebuild.

## Decisions (2026-11-11)

1. **Why the job hit rate limits at 09:36-09:40: treated as fixed by the mitigations above.**
   The likely cause was the 09:36 burst of 8 parallel Codex workers with no back-off and no
   whole-build retry. No further change now. If a 429 comes back, read that slot's
   `.failures.jsonl` first: it shows whether failures track the number of concurrent workers
   or Codex's usage meter, which decides the next lever (fewer workers or a later start).
2. **Keep the hard 95% coverage floor.** A ticker missing from the payload already scores
   NO_DATA and can never be bought, so a lower floor would only cost missed buys, not bad
   ones. But the floor is also the guard against a broken build (stale quotes, a failing
   source), and a healthy build clears it (122/122 on 2026-11-11). If refusals come back
   despite the mitigations, the agreed next step is a two-tier rule rather than a flat cut:
   publish 80-95% coverage marked partial with an alert, and refuse below 80%. That needs its
   own PR and review; it is not built.
3. **No executor-side rebuild.** PR #2218 already retries a refused build once inside the
   launchd job, and a retried 09:36 build finishes well before the 0935 window closes at 10:20
   (PR #2227 opens it at 09:31 and has the run wait for the payload), so the executor only has
   to wait. Letting the executor rebuild would add a second way to build the payload, run from
   a sandbox whose shell has repeatedly been unavailable, and break the single-builder setup.
