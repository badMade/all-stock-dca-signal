# Safeguards & Budget Rules

The binding safety rules for the graded DCA strategy. These override
convenience: if a rule and an automation conflict, the rule wins and the run
should skip rather than trade. Machine-readable copies live in
[`config/budgets.json`](../config/budgets.json) and
[`config/notifications.json`](../config/notifications.json).

## Broker / account

- **Robinhood Agentic account `<ROBINHOOD_ACCOUNT_NUMBER>` is the only execution broker/account**
  for live and manual runs.
- **Alpaca, Finance, and Yahoo are read-only** — validation, market-data, and
  account context only; never execution — unless the user explicitly changes this
  later.

## Live execution roster

**The prior three-stream Perplexity-era roster is still the account's ONLY
currently-functioning live execution.** `config/scheduled_tasks.json` lists
five live-execution tasks (`2ba19526` 9:35 market-open, `42173154` 10:30
morning, `7be82b97` 11:30/1:30/2:30 intraday, `a6239dae` 12:30 midday,
`10ce362c` 3:30 final) that place real orders today, and Perplexity's live
execution role is **not** retired — do not retire it, or the account is left
with zero functioning scheduled execution.

A seven-slot Claude roster is the intended eventual replacement, running as
Cowork scheduled tasks on the Mac Studio (weekdays, ET):

| Slot | Time (ET) | Role |
|---|---|---|
| 1 | 9:35 AM | market-open |
| 2 | 10:30 AM | morning |
| 3 | 11:30 AM | intraday |
| 4 | 12:30 PM | midday |
| 5 | 1:30 PM | intraday |
| 6 | 2:30 PM | intraday |
| 7 | 3:30 PM | final (decision anchor; matches `config/trading_window.json`'s authoritative `decision_time_et: "15:30"`) |

**Correction, 2026-08-02:** the ref_id-prefix mechanism described in an
earlier version of this paragraph (`morning-dca-`/`midday-dca-`/`graded-dca-`
legacy prefixes vs. this roster's own `dca-runner-` prefix) belonged to the
**superseded** generation of the seven Claude prompts
(`claude/dca-executor-prompts/1-executor-*.txt` … `7-executor-*.txt`, which
never place orders at all and are kept only for history — see
`claude/dca-executor-prompts/README.md`). The **canonical, actually-live**
generation is `claude/dca-executor-prompts/live-mirror/*.txt`, captured
2026-07-30/31, and its Gate 4 (sole-executor interlock) works differently: it
classifies every same-window order by `placed_agent` — `'user'`/`'recurring'`/
`'drip'` is Geoff's own manual activity (noted, never aborts); anything else,
including a missing or unrecognized value, is treated as a competing
executor and **aborts and alerts**. There is currently no recognized-Perplexity
carve-out in that classification, which means Gate 4 alone — before Gate 5,
the still-missing signal-generation entry point, is ever reached — is
sufficient to abort and alert on every live-mirror fire where Perplexity has
already traded that slot's window, which today is effectively every slot,
every day.

**Standing dual-executor design (Geoff, 2026-08-02), superseding the
"temporary overlap, then hard cutover" framing this section used to have:**
Perplexity and Claude are meant to coexist indefinitely, coordinating
first-come-first-serve per slot via the one thing they actually share — the
Robinhood account's own order history — rather than being handed off in one
flip of a switch once Gate 5 ships. Long-term intent is Claude-primary,
Perplexity-backup; today Perplexity wins every slot by construction, since
none of the seven Claude prompts has yet been deployed to invoke its
committed Gate 5 producer (see below). See
`claude/PERPLEXITY-CLAUDE-COORDINATION.md` for the full design, the Gate 4
addition needed to recognize Perplexity as an expected, compatible executor
instead of alerting on it as an anomaly, and the one empirical fact (what
value Perplexity's own orders actually carry in `placed_agent`) that must be
confirmed against real order history before that addition is more than a
placeholder. That document also carries the paste-ready Perplexity-side
mirror of the same check. Gate 5 itself (see `PRODUCTION_READINESS.md`) is
untouched by this in a different sense than it used to be: all seven slots
now have a real, tested signal-generation entry point committed in this repo
(`scripts/generate_market_open_candidates.py`, `generate_morning_candidates.py`,
`generate_intraday_candidates.py`, `generate_midday_candidates.py`, and
`generate_buy_candidates.py` — see `PRODUCTION_READINESS.md`'s "Gate 5 half of
this gap is closed" status), but a producer existing is not the same as a slot
trading. A slot does not start placing orders merely because its entry point
exists — the *live task's prompt* must name the command, and that prompt text
lives outside this repo, so none of the seven Claude prompts can place a
single order today regardless of what Gate 4 decides until each one is
deployed to name its committed producer. As of 2026-07-30 the `GATE5_DISABLED`
verdict alerts on every fire (a blocked entry point read as indistinguishable
from an evaluated, empty universe when four of seven slots fired silently the
same day, and a silent executor is worse than an aborting one) -- that verdict
still applies to any slot whose deployed prompt has not yet been updated to
invoke its producer, even though the producer itself is no longer missing.
The seven Claude
prompts run, if created, **alongside** the five Perplexity-era tasks above,
not instead of them (see `claude/dca-executor-prompts/0-setup-card.md`) —
and, per the standing design above, are meant to keep running alongside them
even after Gate 5 ships, not to trigger retiring Perplexity outright. Every
rule elsewhere in this document (pre-authorization, circuit breakers, budget
caps, one-order-per-ticker-per-stream, sole-executor ownership) continues to
apply to the five Perplexity-era tasks that are actually placing orders
today.

**Partially pooled now (2026-08-02), full parity still pending Gate 5.** Geoff
directed that the caps be pooled now rather than waiting for the seven-slot
cutover: `morning_dca.py`'s and `midday_dca.py`'s own budget ledgers were
raised from an isolated $90/day, $450/week half-share each to the SAME shared
$180/day, $900/week Normal-tier ceiling `near_close_dca.py`'s afternoon stream
already uses, and both now seed that ledger from every sibling stream's
same-day/week artifacts (`cap_seed_roots` in `config/morning_dca.json` /
`config/midday_dca.json`), not just their own. The afternoon final run already
pooled correctly before this (its "corrected daily cap" folds in morning/
midday/manual spend). This is a real, live-code fix for the two in-repo
Python streams, but it is NOT full parity with the eventual seven-slot design:
morning and midday still only check against the flat shared Normal tier and
never independently escalate to Elevated/High-volatility/Stress — only the
3:30 final run does that, from its fuller same-day view.

**In-repo executors for market-open and intraday (2026-08-02).** The second
half of that gap is now closed in code: market-open (`2ba19526`) and intraday
(`7be82b97`) previously had no in-repo Python at all, so their trading-window
cutoff, budget ledger, per-order ceiling, and same-day top-up guard existed
only as prose an external scheduler/agent had to self-enforce. They now have
`market_open_dca.py` (`MarketOpenDCAExecutor`) and `intraday_dca.py`
(`IntradayDCAExecutor`, one stream across the 11:30/1:30/2:30 slots), both
subclassing the same `MorningDCAExecutor` pipeline the 10:30 and 12:30 streams
run on, so every one of those rules is enforced by the same tested code path.
Writing real artifacts under `market_open_dca/` and `intraday_dca/` — roots
every sibling stream already scanned and always found empty — also closes
the prior-allocation gap for these slots that the now-removed GATE5 proposal
doc's §1.4 identified (see PRODUCTION_READINESS.md gap #1), in both
directions.

**They ship disabled.** `config/market_open_dca.json` and
`config/intraday_dca.json` both set `live_execution_enabled: false`: the
executors evaluate fully and write every artifact but never call the broker.
Until that flag is flipped, these slots' real orders still come from the
external scheduler/agent exactly as before, and the in-repo executors are a
shadow run whose decisions can be diffed against what actually traded. Flip
the flag only after a full trading day of that comparison, and expect the
per-order and cap behavior to match before trusting it with money. Each run
records `live_execution_enabled` on its `run_state` so a shadow run is
distinguishable from a live run that merely found no candidates. Once the seven-slot
roster is wired, the daily and weekly caps in
[Budget caps](#budget-caps) become fully **shared totals across all seven
slots combined** with dynamic tier escalation available to every slot, not
just the final one: whichever slot spends first exhausts that much of the
shared cap, and a later slot the same day can find less headroom left, or
none. Earlier slots therefore have effective priority within a day. This
falls out of the existing "never exceed the selected daily cap" rule — it is
not a new rule, just a new consequence of having more slots share one cap.

`docs/schedule_manifest.md` and `docs/run_classification.md` still describe
the prior three-stream (morning/midday/afternoon) framing of the currently-
live Perplexity-era roster and have not yet been reconciled with the
seven-slot model's naming above; that reconciliation is separate from, and
does not change, which roster is actually placing orders today.

**Perplexity compatible-executor carve-out (2026-08-07, expires 2026-08-14).**
Six of the seven Cowork-scheduled Claude tasks (935, 1030, 1130, 330, 130,
230 -- all but 1230) now implement `claude/PERPLEXITY-CLAUDE-COORDINATION.md`
§3's Gate 4 addition: a competing-executor order tagged `placed_agent='agentic'`
no longer aborts the run outright. It stands down per-ticker instead (that
ticker excluded from this slot's candidate list; everything else proceeds
normally), and only a rogue (non-`'agentic'`, non-Geoff) order still triggers
the full abort-and-alert. This was directed by Geoff on 2026-08-07 despite the
value being **accepted, not confirmed**: the order data that prompted the
request (330's 3:30 PM abort) is at least as consistent with a duplicate
firing of that same Claude task as with a genuine Perplexity fill, since both
would carry the same `'agentic'` tag and there is currently no way to tell
them apart. See the coordination doc's 2026-08-07 update note for the full
caveat. **Re-review by 2026-08-14** -- the carve-out reverts automatically to
the prior full-abort behavior in each task's prompt text after that date
unless renewed. The 1230 slot is unchanged (§5.5: its Gate 4 runs in
`dca_runner.py` code, not prompt text, and its live placement is off anyway).

## Market-data source policy

- The **Finance connector is the PRIMARY market-data source** for quotes, prior
  closes, spreads, and trend inputs.
- **Alpaca and Robinhood provide validation / context** on top of the primary
  source.
- **Yahoo Finance via `yfinance` is a FALLBACK ONLY**, used when the Finance
  connector data is unavailable, stale, incomplete, or unresolved. Fallback data
  **must be labeled as fallback** and must **not** weaken confidence, spread,
  trend, top-up, budget, whitelist, buy-only, regular-hours-only, or any other
  safety rule.
- **Robinhood remains the only execution broker** regardless of which source
  produced the market data.
- **GFUZ exception:** GFUZ may use **Robinhood as its PRIMARY** source when the
  Finance connector cannot resolve it (see
  `config/tickers.json:special_handling.GFUZ` and
  [morning_dca.md](morning_dca.md#gfuz-special-handling)).

See the `market_data_source_policy` block in
[`config/budgets.json`](../config/budgets.json) and
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

## Trading constraints

- **No sells.** Buys only.
- **No options, no crypto, no margin changes.**
- **No order replacement, no order cancellations, no account modifications.**
- **No trading outside regular market hours.**
- **One strategy order per ticker per stream per day.**
- Orders are **market buys** with a `dollar_amount` notional and
  `time_in_force = gfd`.
- **Submission path (approved Fri Jul 10 2026 11:20 AM ET):**
  - **Scheduled live tasks are pre-authorized.** They **must not** request
    per-order confirmation and **must not** call Robinhood `review_equity_order`
    (which forces a preview + confirmation). They call `place_equity_order`
    directly **only after every internal validation, signal, budget, spread,
    confidence/trend, and top-up check passes**. This pre-authorization was
    approved for the original three-stream model and extends unchanged to all
    seven Claude-executed slots in the
    [Live execution roster](#live-execution-roster).
  - **Manual / ad hoc runs still use the review + confirmation path** —
    `review_equity_order` must pass before any placement.
- None of the above may change unless the user explicitly requests it later.

## Circuit breakers

Hard limits that skip an order rather than trade when any is hit:

- **Max per order: $10** (5x × $2 base) — the absolute per-order ceiling for
  every fractional (notional dollar) order, across all live streams. The one
  carve-out is a `whole_share` ticker's single-share fire, bounded instead by
  `circuit_breakers.whole_share_max_per_order_usd` because such a ticker has no
  purchasable unit smaller than one share (see
  `docs/whole_share_accumulation.md`); every other limit below still applies to
  it unchanged.
- **Max 8 buy attempts per day, account-wide** — shared across every stream and
  manual buying combined (`config/budgets.json`'s
  `circuit_breakers.max_orders_per_day`). Counts attempts, not just fills, so a
  rejected or cancelled order still uses one of the day's 8 slots.
- Never exceed the selected **daily cap**, **weekly cap**, or **available buying
  power**.
- **Whitelisted (active-universe) tickers only.**
- **Skip** when data confidence fails, the bid/ask spread is too wide, the
  intraday trend cannot be validated down (flag `manual_review_candidate` instead
  of buying), or a same-day top-up's current price is not strictly below the prior
  same-day buy reference price.
- **No more than one strategy order per ticker per stream.**

See the `execution_authorization_policy` and `circuit_breakers` blocks in
[`config/budgets.json`](../config/budgets.json).

## Budget caps

Base amount is **$2**. Daily and weekly caps are selected per run by tier,
keyed **solely** on the count of 3x-or-greater signals (multiplier >= 3.0) in
the Gate 7 candidate list -- a value fixed before sizing runs, not an output
of the sizing decision. Tier selection never keys on planned spend.

| Tier | Select when | Daily cap | Weekly cap |
|---|---|---|---|
| Normal | 3x+ signals <= 5 | $180 | $900 |
| Elevated | 3x+ signals 6-10 | $240 | $1,200 |
| High-volatility | 3x+ signals 11-17 | $300 | $1,500 |
| Stress | 3x+ signals > 17 | $360 | $1,800 |

- The selected daily and weekly caps are absolute ceilings.
- **Stress** tier capacity is usable only to the extent prior unused weekly
  rollover reserve is available. The **weekly rollover reserve is capped at $900.**
- Allocations are prioritized by higher multiplier first, then larger percent
  drop, then ticker (alphabetical) for a deterministic tie-break; capped tickers
  are flagged.
- A run never exceeds the selected daily cap, the selected weekly cap, available
  buying power, or the broker/order-review limit.
- **Dynamic risk kill-switch:** when capital deployed by the scheduled-live
  afternoon stream in any trailing 7-day window exceeds **40% of the monthly
  cash allocation** (`monthly_cash_allocation_usd`, default $3,600), the graded
  ladder is **capped at 1x** — the strategy keeps buying dips at the $2 base but
  stops scaling up until the window rolls back under the threshold. A rolling
  30-day cash-burn rate is tracked for visibility. Manual and other streams are
  never counted toward this guard. See [risk_management.md](risk_management.md)
  and the `risk_guard` block in [`config/budgets.json`](../config/budgets.json).

## Test window

- Live testing is approved **through Fri Oct 30, 2026, inclusive**, per
  `config/trading_window.json` (extended 2026-07-26 from the original Aug 28,
  2026 end date to avoid a silent hard stop while Phase 2's real market
  calendar is pending — see `PRODUCTION_READINESS.md`). The window
  deliberately stops before the **2026-11-01 EDT→EST transition**: the three
  live tasks' cron schedules (and the external trigger gate below) are
  fixed-UTC/EDT-only, so trading through that DST change would fire live
  trades at the wrong ET time (or have them rejected outright) until the
  external scheduler's cron is made DST-aware — see
  `config/trading_window.json`'s `description` field.
- `near_close_dca.py`'s date gate independently blocks full NYSE closures
  (e.g. Labor Day, Thanksgiving) via a computed holiday calendar
  (`nyse_holidays()`), regardless of what `config/trading_window.json`
  approves — see [schedule_manifest.md](schedule_manifest.md).
- This is the **code-level gate** enforced by `near_close_dca.py`'s date check.
  The live task's separate trigger gate (`programmatic_trigger_check.sh`) lives
  outside this repo (scheduler-side) and was last documented as enforcing
  weekdays **2026-07-02 → 2026-08-28**, **3:30–3:35 PM ET** (retimed from
  3:20 PM to align with the canonical 3:30 PM ET final live task); confirm/extend
  it independently, since extending `config/trading_window.json` alone does not
  change that external script.
- A one-time reminder (`f7411889`) fires 2026-08-15 to decide on continuation —
  reschedule it too now that the window runs through Oct 30.

## Operational rules

- Every one of the seven live tasks **must write standardized same-day state
  files** and **must not fall back to interactive confirmation** — each runs
  unattended.
- **Per-slot state-file guarantee:** every one of the seven scheduled live
  tasks **always** writes its scheduled `run_state.json` under
  `scheduled_runs/YYYYMMDD_<slot HHMM>ET/` (e.g. `..._0935ET/` through
  `..._1530ET/`) and updates a latest-run pointer on every fire, **even
  when no order is placed**. This guarantee previously applied to a single
  3:30 PM afternoon task under `scheduled_runs/YYYYMMDD_1530ET/`; it now
  applies independently to each of the seven slots. The six non-final slots
  (9:35 through 2:30) share one `cron_tracking/scheduled_runs/` namespace, so
  each writes its own slot-specific pointer filename,
  `latest_scheduled_run_<slot HHMM>ET.json` (e.g.
  `latest_scheduled_run_0935ET.json`), directly under that shared root rather
  than a single unqualified `latest_scheduled_run.json` every later slot in
  the day would otherwise silently overwrite (Codex review, PR #232: "Give
  each slot an unambiguous latest-run pointer"). The 3:30 PM slot's own,
  separate `claude_roster_1530` namespace (below) has no such collision, so
  it keeps the plain `latest_scheduled_run.json` name. If Robinhood review
  passes but submission is blocked by required post-review user confirmation,
  the run_state stays valid and carries
  `order_submit_status='not_submitted_confirmation_required'`, `reviewed_order`
  details where available, candidate details, and **no `order_id`**. Such a run
  is valid scheduled-run evidence that the task ran but **not** evidence of an
  order/fill: the post-trade slippage sync (`67aa802b`) reports no order placed
  without marking the lineage invalid. (Until 2026-08-02 the 3:40 PM watchdog,
  `a46bf007`, separately alerted on the same condition; it was removed from the
  schedule entirely that day — see [Afternoon completion
  rule](#operational-rules) below.) See [run_classification.md](run_classification.md)
  (not yet reconciled to the seven-slot model — see the roster note above).
- **Manual runs do not count as scheduled-run evidence.** Every run_state
  artifact must carry a `run_classification` block distinguishing scheduled from
  manual runs. The legacy `10ce362c` afternoon task's scheduled runs write under
  `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_<slot HHMM>ET/` and update
  `latest_scheduled_run.json`; manual runs write under `manual_runs/<timestamp>/`
  and **never** update that pointer. The seven-slot Claude executor roster
  deliberately does NOT share that `ce16ba6b` root: six slots write under
  `cron_tracking/scheduled_runs/YYYYMMDD_<slot HHMM>ET/`, and the 3:30 PM slot
  (which shares its window with the still-live `10ce362c` executor) writes
  under its own separate `cron_tracking/claude_roster_1530/scheduled_runs/...`
  namespace instead, so its guaranteed disabled-verdict evidence can never
  collide with `10ce362c`'s real run evidence (Codex review, PR #232: "Keep
  the disabled task out of the legacy evidence stream"). None of these seven
  paths has a classification/pointer/watchdog reader wired to it yet, which
  is part of the deferred `run_classification.md` / `config/scheduled_tasks.json`
  reconciliation (see the roster note above). Downstream reporting excludes all manual
  order IDs and any `run_origin='manual'` / `manual_run=true` state, and never
  infers scheduled status from broker orders alone. See
  [run_classification.md](run_classification.md) and
  [`config/run_classification.json`](../config/run_classification.json).
- **Automatic failure recovery (live trading tasks):** each currently-live
  scheduled trading task (see [Live execution roster](#live-execution-roster)
  — today, the five Perplexity-era tasks, since the seven Claude tasks still
  end every fire at the `GATE5_DISABLED` verdict and place no orders (this
  verdict alerts as of 2026-07-30, but still never reaches a trade); this
  extends to those seven too once Gate 5 is wired and enabled)
  treats a mid-run failure caused by **internal persistence, parsing, wrapper-normalization,
  transient connector throttling, or raw-file write** problems **after data was
  fetched** as a **recovery trigger, not a terminal stop**. The task reruns the
  required read-only inputs **once**, **persists all raw responses first**,
  **recomputes only from the persisted files**, and continues through the same
  pre-authorized guarded path **only if every safeguard above still passes** —
  no rule is loosened for recovery. The recovery reuses the same scheduled run
  directory with `recovery_rerun=true` and is **never** reclassified as manual.
  If the recovery attempt **also fails**, or a **safety / account / order
  rejection** occurs, the task writes `run_state.json` with the blocking reason
  and **escalates** rather than retrying further or dropping to an interactive
  path. See the `failure_recovery_policy` block in
  [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and
  [run_classification.md](run_classification.md#recovery-reruns).
- **Manual missed-morning recovery (added Jul 20 2026):** if the **10:30 AM
  scheduled live morning task did not run at all**, a human may place the missed
  morning buys by hand under
  `.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*` (example
  `20260720_1048ET`). These artifacts are **manual** (`run_origin='manual'`,
  `manual_run=true`, `same_day_top_up_context_only=true`,
  `valid_scheduled_evidence=false`): prior-allocation / same-day top-up context
  **only**, never proof the 10:30 scheduled task ran and never scheduled
  evidence. Every later same-day slot (**11:30 AM, 12:30 PM, 1:30 PM, 2:30 PM,
  3:30 PM**) must scan this path and fold any same-day recovery fills into the
  top-up guard as prior allocation. See
  [run_classification.md](run_classification.md#manual-missed-morning-recovery-added-2026-07-20).
- **Afternoon completion rule (rewritten 2026-08-02):** the 3:05 PM monitor-only
  signal check (`e3e6f09e`, already paused) and the 3:40 PM read-only live-run
  watchdog (`a46bf007`) were both **removed from the schedule entirely** at the
  user's request ("Remove the 3:05 single check" / "remove the 3:40 Live-run
  watchdog (read-only)"); the watchdog no longer exists to anchor a 3:40 PM
  completion boundary. All afternoon-window (**2:30 PM and 3:30 PM slot**)
  **trading** now completes **no later than 3:30 PM ET**, when the final live
  execution (`10ce362c`) fires. Same-day **artifact-writing** now runs later:
  the post-trade slippage + compact Notion latest-run sync (`67aa802b`) moved
  from 3:35 PM ET to **5:00 PM ET daily**, and the Friday weekly Notion
  performance dashboard refresh (`041d9d90`) moved from 5:00 PM ET to
  **5:30 PM ET** the same day specifically to clear the collision `67aa802b`'s
  new daily 5:00 PM slot would otherwise create every Friday. The month-end
  volatility risk refresh (`c9fb1fb8` / `17a3b2a9`) moved from its own prior
  **5:00 PM ET** to **5:15 PM ET** the same day for the same reason -- it
  independently collided with `67aa802b`'s new 5:00 PM slot on the final
  eligible market day of the month. These three tasks are now the only
  DCA-related tasks approved to run after 3:30 PM ET.
- **Cost-control policy:** daily tasks are compact / local-first; heavy analysis
  runs Fridays and monthly; avoid manual full runs unless automation fails.

## Notifications

- **No Slack.** This applies to all seven Claude-executed live slots: none of
  them post routine run confirmations to Slack. The only Slack paths anywhere
  in this project are the env-gated, off-by-default drift and failure-alert
  webhooks documented in `config/notifications.json`.
- Notifications go to the **in-app / message center**, and **email only where a
  task explicitly says so** (no routine dashboard emails).
