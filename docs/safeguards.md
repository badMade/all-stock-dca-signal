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

Every one of these seven Claude prompts ends every fire at Gate 5 with the
`GATE5_DISABLED` verdict — no callable signal-generation entry point exists
yet; see `claude/GATE5-PROPOSAL.md` — so none of them can place a single
order today. As of 2026-07-30 this verdict alerts on every fire (see
`claude/GATE5-PROPOSAL.md` and each prompt's Gate 5/Gate 9): a blocked entry
point read as indistinguishable from an evaluated, empty universe when four
of seven slots fired silently the same day, and a silent executor is worse
than an aborting one. They run, if created, **alongside** the five
Perplexity-era tasks above, not instead of them (see
`claude/dca-executor-prompts/0-setup-card.md`). Because all seven slots share
their exact windows with a Perplexity-era task, each prompt's Gate 4 (sole-
executor interlock) also expects to see that task's real same-window order.
Rather than treating any same-window order as evidence of a rogue second
executor, the 10:30, 12:30, and 3:30 prompts inspect each one's ref_id
against their OWN slot's specific legacy prefix: `morning-dca-<trading_day>-
<ticker>` (`42173154`, `morning_dca.py`), `midday-dca-<trading_day>-
<ticker>` (`a6239dae`, `midday_dca.py`), and `graded-dca-<trading_day>-
<ticker>` (`10ce362c`, `near_close_dca.py`) respectively — distinct from
this roster's own `dca-runner-<slot_id>-<trading_day>-<ticker>` (`dca_runner.py`,
once Gate 5 is wired) and from each other. Only when EVERY order in the window
carries that slot's own legacy prefix is it classified
`GATE4_EXPECTED_OVERLAP` — which, like `GATE5_DISABLED`, alerts on every
fire as of 2026-07-30 (see above; Codex review: "Continue past expected
overlap to report blocked Gate 5"); an unrecognized
ref_id or one carrying this roster's own prefix stays on the real
abort-and-alert path (Codex review, PR #232: "Recognize the real legacy
stream ref-id prefixes"; "Distinguish legacy orders before suppressing
Gate 4 alerts"). The 9:35, 11:30, 1:30, and 2:30 prompts have no such
exception: their real legacy counterparts (`2ba19526`, `7be82b97`) carry
`no_python_entry_point=true` in `config/scheduled_tasks.json` — run
directly by an external scheduler/agent with no committed, documented
ref_id convention — so those four prompts cannot safely attribute a
same-window order to them at all, and alert on every collision exactly as
Gate 4 always has. Where it applies, the `GATE4_EXPECTED_OVERLAP`
classification is conditioned on Gate 5 staying disabled and must revert
to real abort-and-alert behavior the moment a human wires Gate 5 and
retires the old tasks. Each prompt still writes `run_state.json` evidence on every fire; the
3:30 PM slot writes it to its own separate `claude_roster_1530` namespace,
never into the legacy `ce16ba6b` root the real `10ce362c` executor uses, so
its guaranteed disabled-verdict evidence can never overwrite or be mistaken
for that executor's real run evidence (Codex review, PR #232: "Keep the
disabled task out of the legacy evidence stream"). Retire the five old tasks
only once a human finishes `claude/GATE5-PROPOSAL.md` and wires a real
signal-generation entry point into the seven Claude prompts — that is the
point the seven-slot roster becomes real live execution and this section
should be updated to say so. Until then, every rule elsewhere in this
document (pre-authorization, circuit breakers, budget caps,
one-order-per-ticker-per-stream, sole-executor ownership) continues to apply
to the five Perplexity-era tasks that are actually placing orders.

Once the seven-slot roster is wired and the old tasks are retired, the daily
and weekly caps in [Budget caps](#budget-caps) will be **shared totals across
all seven slots combined**, not per-slot allowances: whichever slot spends
first exhausts that much of the shared cap, and a later slot the same day can
find less headroom left, or none. Earlier slots therefore have effective
priority within a day. This falls out of the existing "never exceed the
selected daily cap" rule — it is not a new rule, just a new consequence of
having more slots share one cap.

`docs/schedule_manifest.md` and `docs/run_classification.md` still describe
the prior three-stream (morning/midday/afternoon) framing of the currently-
live Perplexity-era roster and have not yet been reconciled with the
seven-slot model's naming above; that reconciliation is separate from, and
does not change, which roster is actually placing orders today.

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

- **Max per order: $10** (5x × $2 base) — the absolute per-order ceiling across
  all live streams.
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

Base amount is **$2**. Daily and weekly caps are selected per run by tier, based
on the day's planned spend and the count of 3x-or-greater signals (multiplier
>= 3.0). Pick the highest tier triggered by **either** dimension.

| Tier | Select when | Daily cap | Weekly cap |
|---|---|---|---|
| Normal | planned spend <= $129 **and** 3x+ signals <= 5 | $180 | $900 |
| Elevated | planned spend > $129 **or** 3x+ signals 6-10 | $240 | $1,200 |
| High-volatility | planned spend > $213 **or** 3x+ signals 11-17 | $300 | $1,500 |
| Stress | planned spend > $272 **or** 3x+ signals > 17 | $360 | $1,800 |

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
  order/fill: slippage reports no scheduled fills/order IDs (and does not
  invalidate the run), and the watchdog alerts on no order placed without marking
  the lineage invalid. See [run_classification.md](run_classification.md)
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
  [run_classification.md](run_classification.md#manual-missed-morning-recovery-added-2026-07-20).
- **Afternoon completion rule:** all afternoon-window (**2:30 PM and 3:30 PM
  slot**) **trading and artifact-writing** tasks complete **no later than
  3:40 PM ET**, when the read-only watchdog
  (`a46bf007`) begins its final verification. The watchdog's own post-3:40 PM
  verification and the Friday weekly Notion performance refresh at **5:00 PM ET**
  are the only approved post-3:40 PM exceptions.
- **Cost-control policy:** daily tasks are compact / local-first; heavy analysis
  runs Fridays and monthly; avoid manual full runs unless automation fails.

## Notifications

- **No Slack.** This applies to all seven Claude-executed live slots: none of
  them post routine run confirmations to Slack. The only Slack paths anywhere
  in this project are the env-gated, off-by-default drift and failure-alert
  webhooks documented in `config/notifications.json`.
- Notifications go to the **in-app / message center**, and **email only where a
  task explicitly says so** (no routine dashboard emails).
