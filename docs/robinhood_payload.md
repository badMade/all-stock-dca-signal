# Prebuilt Robinhood payload (Codex on the Mac)

**Problem (2026-10-15).** With the yfinance fallback retired (692b5887), the live sessions had to pull ~67
tickers of 5-minute bars through MCP tool output themselves. They judged that infeasible inside the slot
window; the 11:30, 1:30 and 2:30 runs all aborted at Gate 5 with nothing placed.

**Why not repo code.** Repo code cannot call Robinhood: a headless client cannot obtain tokens (verified
2026-10-13), and this repo holds no credentials. The Codex CLI on the operator's Mac already holds an approved
Robinhood MCP connection (`codex mcp list` -> `robinhood-trading`).

**What runs.** `deploy/launchd/robinhood_payload.sh auto`, fired one minute after each Robinhood-primary slot's
decision time (09:35 10:30 11:30 12:30 13:30 14:30, Mon-Fri; the 15:30 slot is Finance-primary and is not scheduled here; see "The 3:30 PM slot (shadow only)" below):

1. `scripts/robinhood_payload.py plan` resolves the slot, the exact universe that slot's Gate 5 generator scores,
   the earliest bar the slot's 60-minute trend needs, and the output path.
2. The universe is split into chunks of 8 and each runs through `codex exec` (read-only sandbox, 3 at a time,
   240 s per chunk). The worker may only read historicals and quotes.
3. `scripts/robinhood_payload.py merge` validates every ticker (tz-aware timestamps, consistent OHLC, the
   slot's window, newest bar near the anchor, no outlier bars, quote within 25% of its own last bar and stamped between the newest completed bar's start and 30 minutes
   after the decision, never in the future) and
   publishes `cron_tracking/robinhood_payload/<day>/robinhood_payload_<day>_<slot>.json` atomically, with a
   `.meta.json` sidecar. A ticker that fails goes to `ticker_errors.robinhood`, and the default coverage floor is 95% (it was 100% until
   2026-10-21): up to 4 of 90 failed tickers still publish, each left in `ticker_errors.robinhood` so Gate 5 scores it
   `NO_DATA` and never a BUY. More misses than that publish nothing and the slot aborts at Gate 5. The old 100% floor
   guarded against Gate 6 handing a missing high-priority ticker's cap headroom to lower-priority names; in the
   recorded runs the daily cap peaked near 42% and the weekly cap near 13%, so that reshuffle had no room to occur.
   Each worker's reply counts only for the tickers its own chunk was assigned (`chunk_<i>.json` is
   `plan["chunks"][i]`): any other key is dropped and reported, a ticker two replies carry fails, and a plan
   whose chunks do not partition its universe publishes nothing.

The payload is Robinhood data under `sources.robinhood` and nothing else. It is not a second source, and the
generators are unchanged.

**Freshness.** `publish` refuses to write before the decision time and records `generated_at_et` plus the
payload's SHA-256 in the `.meta.json` sidecar. `check` (and the launcher, before reusing an existing file)
fails with exit 6 unless the sidecar exists, matches the slot, day and checksum, was built after the decision
time, and is no older than 45 minutes (`--max-age-minutes`). The launcher sets a stale file aside as
`*.stale-<epoch>.json` and rebuilds; a session whose `check` exits 6 aborts instead of scoring it. A quote's
`as_of` is not used for this: it is a snapshot or last-trade time and is legitimately old for an illiquid ticker.

**What the live prompts do.** Gate 5 (six `live-mirror` prompts) runs `plan` then `check`; on success it copies
the payload to `$ROOT/market_data_<day>_<slot>.json` and skips the hand fetch. If the file is missing or fails
`check` after 12 minutes of re-checking, the run aborts with `market_data_unavailable`. It never hand-fetches.
It then reads `ticker_errors.robinhood` (2026-10-23, to match the 95% floor from #1878): more than 5% of the universe errored
(more than 4 of 90) aborts with `market_data_unavailable`; up to that, the run continues, names every errored ticker in its
evidence, and the generator scores each one `NO_DATA` (reported as `provider_error_tickers`, never a BUY). The existing
zero-candidates rule still applies: with any `provider_error_count` above 0 a run that places nothing alerts instead of ending silently.

**Refusal marker.** When `merge` refuses to publish (coverage below the floor, or built before the decision
anchor) it writes `<payload>.refused.json` beside the would-be payload: slot, day, time, reason, valid/universe
counts and each not-valid ticker with its reason. `check` on a missing payload reads it and prints
`publisher ran and refused at HH:MM ET: coverage 89/90 ... [not valid: DXYZ (...)]`; with no marker it prints
`no record that the publisher ran for this slot`. The live prompts already put `check`'s stderr in the Gate 9
alert, so the alert now says which of the two happened. (2026-10-21: three slots aborted on a refusal while the
13:30 alert guessed the Mac was asleep.) A marker for another slot or day, or an unreadable one, is ignored; a
successful publish deletes the slot's marker.

**Install.** Copy `deploy/launchd/com.all-stock-dca.robinhood-payload.plist.template` to
`~/Library/LaunchAgents/`, replace the three `REPLACE_WITH_*` values, and `launchctl load` it. The Mac must be on
ET (or shift `Hour`). Logs: `/tmp/dca-robinhood-payload.log`. Timing (2026-10-22): each chunk attempt logs
`robinhood_payload: timing chunk=<i> attempt=<k> seconds=<s> errors=<n>` (`errors=n/a` when no reply came back) and the build
ends with `robinhood_payload: timing total seconds=<s> chunks=<n> parallel=<p>` (fetch plus merge). Read these before
changing chunk size, concurrency or the sessions' 12-minute wait.

**Honest limits.**
- A transient miss on one ticker costs the slot. The launcher retries a chunk once (2026-10-19: the 10:30 run
  covered 52/68 tickers, 9 daily-bar replies truncated and 8 quotes stamped with a wrong offset, so nothing was
  published) and keeps the attempt with fewer per-ticker errors. The coverage floor is 95% (see above).
- A chunk that returns no reply on both attempts is rescued (2026-10-26: the 09:35 run lost chunks 4 and 10 whole,
  16 of 91 tickers, and refused at 75/91 although every other chunk validated). The floor tolerates 4 misses but a
  chunk holds 8, so one dead worker used to cost the slot. Its tickers are now asked for again in requests of
  `DCA_RESCUE_CHUNK_SIZE` (default 2), each with the usual retry and build deadline, and folded back into that
  chunk's reply before the merge, so chunk ownership is unchanged. Logs: `rescue chunk=<i> tickers=<n> size=<s>`
  and `rescue chunk=<i> replied=<n>` (tickers in the folded reply, per-ticker errors included). The floor itself is unchanged.
- Every attempt that returns no reply keeps its worker output in
  `robinhood_payload_<day>_<slot>.chunk_logs/chunk_<i>.attempt<k>.<build epoch>.log` beside the payload (the
  launcher's temp folder, and every log in it, is deleted on exit). A healthy build writes nothing there. Read
  these first when a refusal names whole chunks of tickers.
- An LLM copies the numbers. The validation catches malformed, inconsistent and outlier data; it cannot prove a
  plausible-looking bar is exact. Treat this as the trade-off for having any Robinhood path off-session.
- Needs the Mac awake and Codex logged in. A failure leaves the slot to abort, as before.
- `~/.codex/config.toml`'s default model is used. Pin one with `DCA_CODEX_MODEL` only if needed (model names
  the CLI/account accepts change between releases; `gpt-5.6-sol` worked on 0.145 and is refused on 0.160).
- First live proof (2026-10-15, 2 tickers, one chunk): 23 s, validated 2/2.

## Quote freshness

"Same calendar day" is not fresh (Codex adversarial review of #1704). `validate_ticker` rejects a quote whose `as_of`
predates the slot's trend-window start or falls more than 15 minutes after the decision time. A quote's `as_of` is its
last trade, so a thin ticker's may legitimately be old; the payload as a whole is instead rejected when its newest
`as_of` is earlier than one bar before the decision time (`_require_fresh_fetch`). `publish` and `check` both apply it.
A payload built long after its slot (for example a manual after-hours test build) is therefore rejected.

## The 3:30 PM slot (shadow only)

Step 1 of moving the 3:30 slot onto this job (2026-10-22). Nothing about the live slot changes: no launchd entry fires
it, no live prompt reads its file, and `1530ET` keeps its Finance-primary policy with the Robinhood-only outage fallback.

- **What now works:** `python -m scripts.robinhood_payload plan --slot 1530` and `deploy/launchd/robinhood_payload.sh 1530`
  plan and build `robinhood_payload_<day>_1530.json` on demand. `merge` and `check` need no change.
- **What it plans:** the universe and interval the 3:30 generator itself uses (`load_strategy_config(...).tickers` minus
  `excluded_tickers`, `robinhood_interval_minutes` = 5, `generate_buy_candidates.SLOT_SPECS["1530ET"]` decision time 15:30),
  with bars from **14:30**. The generator bypasses the trend filter at 1530ET and only needs the 60-minutes-back lookback bar
  (14:30), the completed decision bar (15:25) and the previous close; the live prompt's "09:30 onward" is wider than that
  and would make each chunk reply 72 bars per ticker instead of 12.
- **What stays off:** `due_slot` (launchd's `auto`) only ever returns the six scheduled slots, so adding a 15:31 plist
  entry is a separate, deliberate step. A test pins this.
- **Known risk for the next step:** 90 tickers are 12 chunks of 8, run 3 at a time (4 waves of up to 240 s), so a worst-case
  build ends about 16 minutes after its 15:31 start, leaving little of the 15:25-15:55 window for Gates 5 to 7. The fix
  belongs with scheduling (more parallelism or a bounded per-chunk timeout), not here; shadow builds will measure the real
  duration first.

### Shadow schedule and build deadline (step 2a, 2026-10-22)

- **Shadow agent:** `deploy/launchd/com.all-stock-dca.robinhood-payload-1530-shadow.plist.template` fires
  `robinhood_payload.sh 1530` at 15:31 Mon-Fri. It has its own Label and log (`/tmp/dca-robinhood-payload-1530-shadow.log`),
  so loading it cannot touch the six-slot job. Install it like the main one (copy to `~/Library/LaunchAgents/`, replace
  the three `REPLACE_WITH_*` values, `launchctl load`); unload it at any time. It is **operator-installed**: nothing in
  the repo loads it.
- **What it proves:** nothing reads its file, so it changes no trade. Over a few trading days it measures build duration
  (`grep "timing total" /tmp/dca-robinhood-payload-1530-shadow.log`), coverage and quote freshness for the 3:30 universe
  before the slot is moved onto it. Real six-slot builds on 2026-10-22 took 130-244 s (chunk attempts: median 49 s,
  max 70 s, no timeouts).
- **Build deadline:** `DCA_BUILD_DEADLINE_SECONDS` (default 0 = none, so the six scheduled slots are unchanged) bounds a
  build from the start of the fetch. Each attempt's timeout is capped to the time left, and no attempt starts with less than
  `DCA_MIN_ATTEMPT_SECONDS` (default 20) left; skipped chunks log `robinhood_payload: deadline chunk=N ... skipped`, the
  merge enforces the 95% coverage floor, and a late build publishes nothing instead of arriving after the slot's window.
  The shadow agent sets 480 s (a slow build ends by about 15:40).
- **Go/no-go for step 2b (cutover):** five consecutive shadow days with total build time under 6 minutes, no deadline
  skips, and 95% or better coverage with fresh quotes.
