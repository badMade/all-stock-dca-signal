# Prebuilt Robinhood payload (Codex on the Mac)

**Problem (2026-10-15).** With the yfinance fallback retired (692b5887), the live sessions had to pull ~67
tickers of 5-minute bars through MCP tool output themselves. They judged that infeasible inside the slot
window; the 11:30, 1:30 and 2:30 runs all aborted at Gate 5 with nothing placed.

**Why not repo code.** Repo code cannot call Robinhood: a headless client cannot obtain tokens (verified
2026-10-13), and this repo holds no credentials. The Codex CLI on the operator's Mac already holds an approved
Robinhood MCP connection (`codex mcp list` -> `robinhood-trading`).

**What runs.** `deploy/launchd/robinhood_payload.sh auto`, fired one minute after each Robinhood-primary slot's
decision time (09:35 10:30 11:30 12:30 13:30 14:30, Mon-Fri; the 15:30 slot is built by its own 15:31 agent instead; see "The 3:30 PM slot" below):

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
   **Quiet tickers (2026-10-28).** A ticker that printed no bar at all in the slot window did not trade; it is not a failed
   fetch. (It was added for 10-27 13:30, where eight empty names refused the slot at 82/90; 2026-10-31: those eight were
   exactly plan chunk 0, left out of the reply on 10-26 too, and include SPCX, LUNR and DFTX at 1-68M shares a day, so
   that was a dropped chunk, which the rescue below now re-asks. Real quiet names exist: QTOP, GFUZ, CHE under 100k. Since 2026-11-03 "quiet" also needs a thin prior session, under `LIQUID_PRIOR_VOLUME`: a liquid name with no bars is a real failure counted against the coverage floor.) `publish` leaves it in
   `ticker_errors.robinhood` (NO_DATA, never a BUY; omitting it would read as "not covered" and allow a single-source BUY),
   lists it under `quiet` in the `.meta.json` sidecar, and takes it out of the coverage denominator. More than
   `MAX_QUIET_SHARE` (25%) quiet tickers still refuses, because a feed outage also returns empty series. A ticker that
   traded but whose newest bar is far before the anchor stays a real failure. The prompts leave the sidecar's quiet names
   out of their ">5% errored" count.
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
- First-pass chunk size is `DCA_CHUNK_SIZE` (unset = the planner's default, 4 since 2026-11-02). It exists to retune without a
  code change: on 2026-11-03 the 15:31 build, still at 8-ticker chunks, got "exceeds the output limit" on nearly every reply,
  spent its deadline on retries and rescues and refused at 15/84. Both plist templates also set `DCA_CODEX_PARALLEL`,
  since 4-ticker chunks mean more worker calls: 6 from 2026-11-04, 8 since 2026-11-05 (the 122-ticker universe is 31
  chunks, and 8 workers keeps that at 4 waves). An installed plist keeps its old value until it is re-rendered and reloaded.
- A chunk that returns no reply on both attempts is rescued (2026-10-26: the 09:35 run lost chunks 4 and 10 whole,
  16 of 91 tickers, and refused at 75/91 although every other chunk validated). The floor tolerates 4 misses but a
  chunk holds 8, so one dead worker used to cost the slot. Its tickers are now asked for again in requests of
  `DCA_RESCUE_CHUNK_SIZE` (default 2), each with the usual retry and build deadline, and folded back into that
  chunk's reply before the merge, so chunk ownership is unchanged. Logs: `rescue chunk=<i> tickers=<n> size=<s>`
  and `rescue chunk=<i> replied=<n>` (tickers in the folded reply, per-ticker errors included). The floor itself is unchanged.
- A chunk that replies but leaves tickers out, or answers them with a per-ticker `error`, is rescued the same way
  (2026-10-26: the 13:30 run refused at 83/91 because DFTX, TRIN, AAAU, SPCX, EVR, LUNR, IRDM and DXYZ came back
  `no data returned for this ticker` while their live quotes were fine; their chunks had replied, so the dead-chunk
  rescue never ran). Only the absent or errored tickers are re-asked, in the same `DCA_RESCUE_CHUNK_SIZE` requests,
  logged as `rescue chunk=<i> tickers=<n> size=<s>`. A ticker that replied with data is never re-fetched, and a rescue
  reply that is again missing or an error leaves the original entry in place, so the rescue cannot make a ticker worse.
- A chunk whose every answered ticker has empty `intraday_bars` is re-asked in full (2026-10-30, the 10-29 15:31
  chunk 3), and in a chunk that answered others, an empty ticker whose `previous_close_bar.volume` is at least
  `LIQUID_PRIOR_VOLUME` (1,000,000 shares) is re-asked alone (2026-10-31): an hour with no bar cannot be real for a
  name that traded that much the session before. Thinner empty names are left alone and filed quiet. A liquid name
  that comes back empty again is a real failure (it counts against the coverage floor, not the quiet share); the rule only decides
  what is asked for again. The rule is `rescue_targets` in `scripts/robinhood_payload.py`.
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

## Refusal notification (2026-11-03)

When `publish` refuses a build (coverage floor, quiet cap, or built before the anchor) it writes the `.refused.json` marker and then raises a macOS notification titled `Payload refused: slot <slot> <day>`, whose body is the reason, `valid/universe` and up to five failed tickers (`+N more`), at most 250 characters. The launcher exports `DCA_NOTIFY_REFUSAL=1` by default; inside the publisher it is opt-in, so tests and direct callers stay silent. Set `DCA_NOTIFY_REFUSAL=0` in the job's environment to turn it off. It reuses `schedule_watchdog.notify_macos`, is best-effort (a missing, failing or hung notifier changes neither exit status 2 nor the marker, and the log names only the exception class), and a published payload never notifies. It needs a logged-in GUI session; under a headless launchd context `osascript` may fail, which is logged and harmless.

## Plan-first ingest (2026-10-26)

This program now does everything the retired 5-minute Claude courier did. After the merge, when `DCA_PLAN_FIRST_INGEST=1`,
`robinhood_payload.sh` runs a second read-only `codex exec` with `broker-prompt` (only `get_equity_orders`, paginated, and
`get_portfolio`; the account number comes from `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`), then `ingest`. `ingest` refuses a
payload older than the freshness bound (exit 6) or a payload or broker reply more than 10 minutes from now (exit 2), takes
the `plan_first/ingest` lease (exit 3 if held), and writes the recommendation files stamped with the payload's build time.
It is best-effort: a failure is logged and never changes the launcher's exit status, so Gate 5 does not depend on it.
Quotes now carry optional `bid_price`/`ask_price` (ask below bid fails the ticker). Default off; one launchd job per slot does
both steps, and there is no separate courier or publish schedule.

## Quote freshness

"Same calendar day" is not fresh (Codex adversarial review of #1704). `validate_ticker` rejects a quote whose `as_of`
predates the slot's trend-window start or falls more than 15 minutes after the decision time. A quote's `as_of` is its
last trade, so a thin ticker's may legitimately be old; the payload as a whole is instead rejected when its newest
`as_of` is earlier than one bar before the decision time (`_require_fresh_fetch`). `publish` and `check` both apply it.
A payload built long after its slot (for example a manual after-hours test build) is therefore rejected.

## The 3:30 PM slot

Step 1 of moving the 3:30 slot onto this job (2026-10-22). Nothing about the live slot changes: no launchd entry fires
it, no live prompt reads its file, and `1530ET` keeps its Finance-primary policy with the Robinhood-only outage fallback.

- **What now works:** `python -m scripts.robinhood_payload plan --slot 1530` and `deploy/launchd/robinhood_payload.sh 1530`
  plan and build `robinhood_payload_<day>_1530.json` on demand. `merge` and `check` need no change.
- **What it plans:** the universe and interval the 3:30 generator itself uses (`load_strategy_config(...).tickers` minus
  `excluded_tickers`, `robinhood_interval_minutes` = 5, `generate_buy_candidates.SLOT_SPECS["1530ET"]` decision time 15:30),
  with bars from **14:25**. The generator bypasses the trend filter at 1530ET and only needs the 60-minutes-back lookback bar
  (14:25, anchored on the completed 15:25 bar), the completed decision bar (15:25) and the previous close; the live prompt's "09:30 onward" is wider than that
  and would make each chunk reply 72 bars per ticker instead of 12.
- **The window start is exact (2026-10-30).** `validate_ticker` used to accept a series whose first bar came up to one
  interval after the planned start. On 2026-10-30 the 3:30 plan still started at 14:30 (fixed in #2059), every series
  began at 14:30, the payload checked 91/91, and the generator then scored all 91 `NO_DATA`/provider_error for want of
  the 14:25 lookback bar. A series must now include the bar at the planned start, or that ticker fails in `merge` and
  `check` with `do not cover the HH:MM trend-window start (first bar HH:MM ET)` and counts against the coverage floor.
  No published series from 2026-10-19 to 10-30, in any slot, started anywhere else.
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
  The shadow agent sets 540 s (a slow build ends by about 15:40), and since 2026-10-30 also the absolute
  `DCA_BUILD_DEADLINE_ET=15:40` (HH:MM ET on the plan's trading day; see "Build timing vs. the window" under Cutover).
- **Go/no-go for step 2b (cutover):** five consecutive shadow days with total build time under 6 minutes, no deadline
  skips, and 95% or better coverage with fresh quotes.

### Cutover (step 2b, 2026-10-30)

- **Go/no-go met.** The 15:31 agent published on all five shadow days from 2026-10-23 to 10-29 in 220-264 s, with no
  deadline skips: 90/90 or 91/91 tickers on four days, and 83/91 on 10-29, when 8 tickers were recorded as quiet.
- **What changed.** The `1530ET` prompt's Gate 5 now runs the same PREBUILT ROBINHOOD PAYLOAD step as the six slots
  (`plan --slot 1530`, `check`, the 12-minute wait, the errored and quiet-ticker rule) and no longer fetches Finance or
  Robinhood bars itself. Scheduled sessions have no Finance connector and cannot write tool output into the checkout,
  so the old step aborted at Gate 5 on 2026-10-19, 10-20, 10-21, 10-23 and 10-29.
- **Source policy is unchanged.** The payload holds `sources.robinhood` only. `config/scheduled_tasks.json`'s 1530ET
  `finance_outage_fallback: robinhood_only` (operator, 2026-10-21) already scores that absent-Finance shape off
  Robinhood alone.
- **The builder is the same agent.** `com.all-stock-dca.robinhood-payload-1530-shadow` keeps its Label, so an
  installed copy needs no change; it is no longer a shadow. `due_slot` (`auto`) still never selects 1530.
- **Build timing vs. the window (2026-10-30).** Measured 1530 builds (`timing total` in the 15:31 log, `generated_at_et`
  in each `.meta.json`): 220-264 s, published 15:34:45 to 15:35:26 on 10-23 to 10-30, so three of six published after
  15:35. That is not a miss. `scheduled_window_end_et` 15:35 bounds only the reporting fallback that attributes broker
  orders to a run whose state lost its order IDs; nothing gates the payload on it. The real limit is Gate 5's 12-minute
  re-check of `check`: the session fires at 15:30, so it never gives up before 15:42. The relative 540 s deadline ends
  an on-time build by about 15:39, but counts from the build's own start, so a job launchd fires late could still fetch
  past 15:42. The 15:31 agent therefore also sets `DCA_BUILD_DEADLINE_ET=15:40`: an absolute America/New_York cutoff on
  the plan's trading day (the earlier of the two deadlines wins). A build that starts after it logs
  `deadline 15:40 ET had already passed when the build started; fetching nothing`, skips every chunk, and the merge
  writes its refusal marker, so the session's `check` names a refusal instead of waiting on a payload that arrives
  after it stopped looking. No freshness or coverage check changed. Orders a session places after 15:35 still fall
  outside that reporting fallback's window; that is a reporting question, not a build one, and is left as is.
- **10-29 "quiet" NVDA/GOOGL/GOOG/AMZN (closed 2026-10-31).** The 15:31 build's `.meta.json` files exactly one chunk
  as quiet: chunk 3 (NVDA, QBTS, FLKR, INTC, GOOGL, GOOG, AMZN, GLW) replied with every ticker's `intraday_bars` empty,
  so the worker dropped the series for the whole request; no other ticker of that build was quiet, and no
  `chunk_logs` were kept because the chunk did reply. f74eae58 (10-30) re-asks a chunk whose every answered ticker is
  empty, which covers that exact shape. It would not have covered the same failure with one ticker of the chunk still
  answered, so the rescue now also re-asks any empty ticker whose prior session traded at least
  `LIQUID_PRIOR_VOLUME` (1,000,000) shares (`rescue_targets` in `scripts/robinhood_payload.py`); see the rescue list
  above. Quiet, freshness and coverage checks are unchanged.

## 2026-11-02: the worker prompt named a `span` the tool does not have

Every slot refused on 2026-11-02. The Robinhood MCP `get_equity_historicals` schema is `additionalProperties: false`
with `symbols` and `start_time` required and only `interval`, `end_time`, `bounds` and `adjustment_type` optional; there
is no `span`. The prompt told the worker "span day" / "span week", so calls were rejected with
`unexpected additional properties ["span"]` (15:30 chunk logs) or the worker retried without it and pulled a full-day,
auto-interval series that blew its output limit ("exceeds the available response limit", 12:30). `build_prompt` now
passes explicit RFC3339 UTC `start_time`/`end_time` for the slot window and a 7-day `start_time` for the previous daily
bar. Launchd was not late: the 15:30 build started at 15:31:04 ET (the chunk logs stamp UTC) and `caffeinate`
assertions held the Mac awake all day. The 13:30 refusal stamped 20:23 ET is a later manual replay, so its "quote too
long after the decision" reasons are an artifact of that replay. What was actually late is build duration: the 14:30
build took 1583 s (rescue waves, single chunks of 54-76 s) against the ~25-minute slot window, consistent with workers
pulling oversized series after the `span` rejection.
