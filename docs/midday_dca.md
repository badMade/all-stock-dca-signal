# Midday DCA Stream

The **midday** DCA stream is a single **12:30 PM ET live Robinhood execution**
that grades each ticker's price weakness against **yesterday's regular-session
close** on the shared **$2-base 0x-to-5x** live ladder (as of the Fri Jul 10 2026
live-automation update every live stream — morning, midday, afternoon — shares
this one ladder; the former morning-only 0x-to-3x ladder is historical/superseded).
It is implemented by [`midday_dca.MiddayDCAExecutor`](../midday_dca.py) and
governed by [`config/midday_dca.json`](../config/midday_dca.json).

| Mode   | Task         | ET         | Trades    | run_type           | schema            | Artifact root                     |
| ------ | ------------ | ---------- | --------- | ------------------ | ----------------- | --------------------------------- |
| `live` | `a6239dae`   | 12:30 PM   | **yes**   | `midday_dca_live`  | `midday-live-v1`  | `.../cron_tracking/midday_dca/`   |

`MiddayDCAExecutor` subclasses `MorningDCAExecutor` and reuses the entire
evaluate/build/write pipeline, overriding only the stream-identity attributes and
adding the same-day top-up guard.

## 12:30 PM live Robinhood execution

Uses **Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>`** as the sole execution broker (Alpaca stays
read-only for validation/context), the **$2 base afternoon 0x-to-5x ladder**, the
same active universe, and its own `midday` budget
ledger. Writes under `.../cron_tracking/midday_dca/YYYYMMDD_1230ET/`
(`run_id='midday_live_YYYYMMDD_1230ET'`) and updates that root's
`latest_midday_run.json`. The 12:30 PM decision time gives a real intraday trend
window.

### Confidence rule (no trend, no auto-buy)

A candidate whose price/drop passes the ladder is auto-bought **only when the
intraday trend can be validated** — at least `trend_min_bars` (default **6**)
usable intraday bars (finite, positive close, timestamp at or before the 12:30
decision time). If price/drop passes but the intraday trend **cannot** be
validated, the run does **not** auto-buy; the candidate is classified
`execution_status='manual_review_candidate'` and `trend_validated=false`. There is
**no** loosened trend-unavailable execution path.

## Same-day top-up guard (prior-allocation context only)

Morning **live** and **manual** same-day buys are **prior-allocation context
only** — never midday scheduled evidence. Per ticker the guard applies:

- **No earlier same-day allocation** → normal allocation (buy the full midday
  desired amount).
- **Earlier allocation and midday decision price strictly below** the earlier
  run's reference price → buy only the **incremental** dollar difference
  (midday desired − amount already bought); `$0` if that is `≤ 0`.
- **Earlier allocation but midday price not below** the reference price → `$0`.
- **Earlier amount known but no comparable price** (missing prior reference or
  current midday price) → conservative `$0` with `top_up_manual_review` flagged
  (a `manual_review_candidate`).

**Reference price preference.** A prior run's reference price is resolved in this
order: the run's logged `target_price`, then `decision_price`, then the confirmed
`average_fill_price` (from `orders.csv`, or a manual override's
`filled_orders.csv`). A missing reference (none available) → no automatic top-up
(`manual_review_candidate`).

Only earlier runs generated **before 2:30 PM ET** contribute; scheduled afternoon
runs are excluded as prior context. Prior allocations are scanned across
prior_allocation_roots (the morning-live root, which recursively includes the manual missed-morning
recovery root .../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*); when a
ticker appears in more than one root the amounts are summed and the most recent
valid same-day reference price wins (ordered by generated_at), so midday must beat
that most recent earlier reference before it tops up.

**Missed 10:30 morning run recovery (added Jul 20 2026).** When the 10:30
scheduled morning live task did not run and a **manual** recovery placed the
missed buys under `.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`
(e.g. `20260720_1048ET`), the 12:30 midday run **scans that path** and folds any
same-day manual recovery fills into this top-up guard as prior allocation. Those
manual artifacts (`run_origin='manual'`, `manual_run=true`,
`same_day_top_up_context_only=true`, `valid_scheduled_evidence=false`) are
prior-allocation / top-up context **only** — never proof the 10:30 scheduled task
ran and never midday scheduled evidence. See
[morning_dca.md](morning_dca.md#manual-missed-run-recovery-added-jul-20-2026).

Per-candidate the run_state records `prior_same_day_amount`,
`prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`,
`midday_desired_amount`, `midday_decision_price`, `incremental_order_amount`,
`top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. The run also
lists `top_up_offset_tickers` and `top_up_manual_review_tickers`.

## Why it is fully separate from the other streams

The midday stream is **independent** of the afternoon 3:30 PM live task
(`10ce362c`, which supersedes the deleted `ce16ba6b`) and of the morning stream:

| Axis | Afternoon live (`10ce362c`) | Morning stream | Midday stream |
| --- | --- | --- | --- |
| Ladder | 0x → **5x** | 0x → **5x** | 0x → **5x** |
| Budget ledger | `StrategyState` / `BUDGET_TIERS` | namespace `morning` | namespace `midday` |
| Artifact root | `.../ce16ba6b/` | `.../morning_dca/` | `.../midday_dca/` |
| Run type | `scheduled_live_test` @ `15:30` | `morning_dca_*` @ `09:45`/`10:30` | `midday_dca_live` @ `12:30` |
| Pointer | `latest_scheduled_run.json` | `latest_morning_run.json` | `latest_midday_run.json` |

The midday stream never writes into the morning or `ce16ba6b` trees, never
updates `latest_scheduled_run.json`, and its `run_classification` always carries
`stream="midday"` and `valid_afternoon_evidence=false`. Afternoon consumers
(`67aa802b`, `041d9d90`, `0656657a`) glob only
`.../ce16ba6b/scheduled_runs/**`, so they never traverse the midday root.
(`a46bf007`, formerly also in this list, was removed from the schedule
entirely on 2026-08-02.)

**Prior-allocation context only:** midday fills are read by the afternoon
**same-day top-up guard** as prior-allocation context, but never as afternoon
scheduled evidence, slippage, watchdog confirmation, scheduled cost basis, or
scheduled fill counts. See
[strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).

## Manual override reruns

An out-of-band manual buy of the midday stream on account `<ROBINHOOD_ACCOUNT_NUMBER>` is written
under `.../cron_tracking/midday_dca/manual_reruns/<timestamp>/` as `run_state.json`
plus a confirmed-fill `filled_orders.csv`. Such a run is a **manual invocation** of
the same executor: `run_origin='manual'`, `manual_run=true`, `run_type` stays
`midday_dca_live`, and it uses a non-scheduled `run_id`
(`manual_midday_live_YYYYMMDD_HHMMSSZ_<suffix>`), so it validates against the
manual branch of
[`schemas/midday_run_classification.schema.json`](../schemas/midday_run_classification.schema.json).

Manual override reruns are **excluded** from every scheduled metric — evidence,
slippage, reliability, fill counts, watchdog confirmation, and cost basis — and
are used **only** as prior-allocation context for the **afternoon** 3:30 PM
same-day top-up guard (the afternoon guard scans the midday root, which contains
`manual_reruns/`). They do **not** affect the midday run itself (they post-date the
12:30 PM decision).

Example — 2026-07-09 manual override fills (excluded from scheduled evidence,
affecting afternoon top-up eligibility only):

| Ticker | Amount | Avg fill price |
| --- | --- | --- |
| `AMZN` | $3.00 | 242.1099 |
| `GOOG` | $4.50 | 352.1778 |
| `GOOGL` | $4.50 | 354.9800 |

### Manual missed-12:30 recovery (added Jul 20 2026)

Separate from a manual **override** rerun, this covers the case where the **12:30
scheduled midday task did not run or failed**. A human may then place the missed
midday buys by hand under a dedicated path:

- Pattern: `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/YYYYMMDD_*`
- Example: `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/20260720_1259ET`
  (Jul 20 2026: placed at 12:59 ET after the 12:30 scheduled midday run failed).

Fills in that recovery:

| Ticker | Amount | Avg fill price |
| --- | --- | --- |
| `AAPL` | $1.00 | 324.389900 |
| `TRIN` | $2.00 | 17.399900 |
| `ASML` | $2.00 | 1739.506300 |
| `IRDM` | $2.00 | 46.669900 |

The artifact carries `run_origin='manual'`, `manual_run=true`,
`same_day_top_up_context_only=true`, and `valid_scheduled_evidence=false`. It is
prior-allocation / same-day top-up context **only** and must be available to the
**3:30 afternoon** run as such. It is **never** midday scheduled evidence, never
proof the 12:30 scheduled task ran, and never counted in scheduled reliability,
fill counts, slippage, cost basis, or watchdog confirmation. See
`config/scheduled_tasks.json`'s `midday_dca_manual_recovery_2026_07_20`.

### Manual midday run (Jul 21 2026, 1:45 PM ET)

A human placed a manual midday run at **1:45 PM ET on 2026-07-21** under
`/home/user/workspace/cron_tracking/midday_dca/manual_runs/20260721_1345ET`. It
carries `run_origin='manual'`, `manual_run=true`, `stream='midday_manual'`,
`same_day_top_up_context_only=true`, `valid_scheduled_evidence=false`, and
`valid_afternoon_scheduled_evidence=false`. It is same-day top-up context **only**:
it is **never** proof the 12:30 or 3:30 scheduled tasks ran, and it is **excluded**
from scheduled slippage, scheduled fill counts, scheduled reliability, scheduled
cost-basis evidence, watchdog proof, and scheduled afternoon evidence. It may be
used by the later same-day **3:30** top-up logic as prior-allocation context only.
Filled orders (total **$9.00**):

| Ticker | Amount | Order id | Avg price | Quantity |
| --- | --- | --- | --- | --- |
| `MSFT` | $2.00 | `6a5fb151-043b-4063-b317-40b666bb4bb7` | 399.136400 | 0.005010 |
| `GOOGL` | $2.00 | `6a5fb151-de49-4f8d-bd0e-2e98aeece284` | 349.168900 | 0.005727 |
| `GOOG` | $2.00 | `6a5fb151-330b-4d78-9fcc-3da0151ab289` | 348.485500 | 0.005739 |
| `MA` | $1.00 | `6a5fb151-653c-4dc9-ae1e-11515afcae27` | 540.544500 | 0.001849 |
| `IBM` | $2.00 | `6a5fb151-cdee-41e9-889e-8ff65762d394` | 211.790000 | 0.009443 |

See `config/run_classification.json`'s `midday_manual_run_2026_07_21` and
`config/scheduled_tasks.json`'s `midday_dca_manual_run_2026_07_21`.

## Midday sizing ladder (0x → 5x)

Drop is measured as a fraction below yesterday's regular-session close. The
largest tier threshold ≤ the actual drop wins.

| Drop vs. prior close | Multiplier |
| --- | --- |
| < 0.1% | 0x (no buy) |
| 0.1% – < 2% | 1x |
| 2% – < 4% | 1.5x |
| 4% – < 5% | 2x |
| 5% – < 6% | 2.5x |
| 6% – < 6.5% | 3x |
| 6.5% – < 7% | 3.5x |
| 7% – < 7.5% | 4x |
| 7.5% – < 8% | 4.5x |
| ≥ 8% | 5x (no upper cutoff) |

**Corrected 2026-08-03.** This table previously showed a uniform 1%-step curve
(0.1–<1% = 1x, 1–<2% = 1.5x, 2–<3% = 2x, …) that matched **neither the code nor
any config file** — it was the superseded pre-2026-08-02 ladder shape. The rows
above are verified against `config/midday_dca.json`'s `sizing_ladder.tiers`,
which is byte-identical to `config/sizing_ladder.json` and to
`MIDDAY_SIZING_TIERS = StrategyConfig().sizing_tiers` (`midday_dca.py`). The
practical difference is large: a 3% drop is **1.5x**, not the 2.5x the old table
claimed, and 3x now requires a **6%** drop rather than 4%.

Dollars = `selected_base_amount_usd × multiplier`, where the base is the dynamic
first-allocation base (**$2** below prior close *and* prior low, **$1** below
prior close only) — not a flat $2 (actual order size, subject to the top-up
guard offset).

> **This shared table is the fallback, not the live curve.** As of 2026-10-20
> `config/per_ticker_sizing.json` is `status: active` and carries 92 entries: all
> 90 active symbols plus the two whole-share symbols, which the ladder
> evaluates identically. all 92 are volatility-calibrated from the `ticker_data` archive (RVII, the
> thinnest, has 47 sessions against the 40-session minimum), so `fallback_tickers`
> is empty. APH, TEL, GFS and the 22 tickers added 2026-10-19 were first sized off
> the global fallback ladder and calibrated on 2026-10-20.
> `near_close_dca.graded_buy_multiplier` reads a ticker's calibrated tiers first
> and falls back to the table above only for symbols with no entry — currently
> none. See [strategy.md](strategy.md#sizing-ladder-latest-active).

## Budget ledger

The midday ledger is its own budget session (namespace `midday`), separate from
the morning ledger and the afternoon `StrategyState` / `BUDGET_TIERS` ledger:

- `midday_daily_cap_usd` (`180.0`)
- `midday_weekly_cap_usd` (`900.0`)

**These are the shared Normal-tier ceiling, not an isolated per-stream share.**
Until the 2026-08-02 pooling fix they were `90.0` / `450.0` — an isolated half —
and `config/midday_dca.json`'s `budget_ledger.shared_pool_note` records the
change: they now match `near_close_dca.BUDGET_TIERS[0]`, the same ceiling every
stream draws from, mirrored in `config/budgets.json` and
`config/morning_dca.json`. That note's `artifact_paths.cap_seed_roots` (absent
before, so midday had no cross-stream cap visibility at all) now spans every
sibling stream, so this ledger seeds from what the others already spent.

The practical consequence for incident triage: midday spend above `$90` is not a
cap violation, and earlier same-day spend by a sibling stream *is* relevant to
what midday has left. Exceeding a cap adds a warning to the run artifact.

## Raw input generation and failure-state guarantee (added Jul 20 2026)

After the Jul 20 2026 12:30 run escalated (it invoked a saved runner without
first generating the same-day raw inputs and wrote no failure-state artifact),
the scheduled 12:30 task must:

**Generate raw inputs first.** Before invoking any saved runner such as
`.../cron_tracking/a6239dae/run_midday_live.sh` or `midday_live_runner.py`, the
task **creates** `.../cron_tracking/midday_dca/YYYYMMDD_1230ET/raw/` and persists
all required raw inputs for that same-day run:

- `raw/portfolio.json`
- `raw/quotes.json` (or clearly named quote batches normalized into
  `raw/quotes.json`)
- historical / trend files needed for the 60-minute trend check

If the runner requires a `RUN_DATE_ET` argument, pass the current
America/New_York date as `YYYYMMDD`. The task **must not** assume
`raw/quotes.json` or historicals already exist from a prior run. Inputs come from
Robinhood (5-minute and daily bars plus quotes, the same source as every other
Robinhood-primary slot), with no data-source fallback (the `yfinance` one was retired
2026-10-12): a Robinhood failure as a whole aborts (see
[safeguards.md](safeguards.md#market-data-source-policy)).
If raw input generation fails once, perform **one** bounded recovery attempt with
the alternate approved data source path, then stop.

**Always write the state files.** The 12:30 scheduled run **always** writes
`run_state.json`, `run_summary.json`, and `latest_midday_run.json` under the
midday root, **even when no orders are placed or execution is blocked**. If raw
inputs are missing, malformed, stale, or not generated, it writes a **valid
scheduled** midday `run_state` with `run_origin='scheduled'`, `manual_run=false`,
`stream='midday'`, `scheduled_for_et='12:30'`, `cron_id='a6239dae'`,
`valid_afternoon_evidence=false`, `order_submit_status='not_submitted_error'`,
`error_class='missing_or_invalid_raw_inputs'`, `orders_submitted_count=0`, and a
compact list of missing files or failed data sources. This failure-state artifact
is valid proof the 12:30 scheduled task **fired**, but it is **not** order or fill
evidence. The task **must not** escalate without writing this state file unless
the workspace itself is unavailable.

**Same-day pointer guard.** The refreshed `latest_midday_run.json` carries the
same-day guard fields as the morning pointers (`pointer_status`,
`same_day_required`, `trading_day`, `updated_at_utc`, `valid_scheduled_evidence`
derived from the run_state) built by
[`scripts/same_day_pointer.py`](../scripts/same_day_pointer.py). Midday
refreshes **only** `latest_midday_run.json` — `_scheduled_pointer_names` only
appends the second `latest_morning_live.json` pointer when `self.stream ==
"morning"`, so a midday run (`stream == "midday"`) never touches it. Consumers
validate it via `same_day_pointer.validate_pointer` (`expected_stream=
"midday"`) and prefer `select_same_day_run_state` over the raw pointer when a
date-specific `YYYYMMDD_1230ET/run_state.json` is available. See
[morning_dca.md](morning_dca.md#same-day-latest-pointer-guard).

**Missed-morning recovery interaction.** When same-day manual missed-10:30
recovery artifacts exist under
`.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`, load them **after**
raw market data is available and **before** order sizing. They remain
prior-allocation / top-up context only: they **must not** satisfy raw quote or
historical input requirements, **must not** be copied into the scheduled evidence
stream, and **must not** be counted as proof the 10:30 scheduled task ran. See
[run_classification.md](run_classification.md#manual-missed-morning-recovery-added-2026-07-20).

## Artifacts

A scheduled midday run writes to `.../cron_tracking/midday_dca/YYYYMMDD_1230ET/`:

- `run_state.json` — classification, ladder, budget, signal rows, buy
  candidates (with top-up fields), warnings.
- `midday_signals.csv` — one row per ticker.

and updates that root's `latest_midday_run.json`. Manual runs write under a
distinct `manual_midday_live_*` run id and never update the pointer.

## Run classification

Validated by
[`schemas/midday_run_classification.schema.json`](../schemas/midday_run_classification.schema.json).
A scheduled live run:

```json
{
  "run_origin": "scheduled",
  "run_type": "midday_dca_live",
  "stream": "midday",
  "trades": true,
  "cron_id": "a6239dae",
  "supersedes_cron_id": null,
  "scheduled_for_et": "12:30",
  "scheduled_window_start_et": "12:25",
  "scheduled_window_end_et": "12:35",
  "manual_run": false,
  "run_id": "midday_live_YYYYMMDD_1230ET",
  "state_schema_version": "midday-live-v1",
  "valid_afternoon_evidence": false
}
```

## Usage

```python
from datetime import datetime
from midday_dca import MiddayConfig, MiddayDCAExecutor
from near_close_dca import EASTERN

live = MiddayDCAExecutor(MiddayConfig(tickers=("AAPL", "MSFT", "NVDA")))

now = datetime.now(tz=EASTERN)
result = live.run(now, robinhood_data_provider)
```

## Gate 5 signal generation for the 1230ET roster slot

`scripts/generate_midday_candidates.py` is the committed, tested Gate 5 producer
for the 12:30 PM slot of the seven-slot live-mirror roster. It constructs the
real `MiddayDCAExecutor` and calls its own `evaluate()` — deliberately not
`run()`, which owns budget arithmetic, the same-day top-up guard, placement and
the artifact tree (Gates 6 through 9) that the slot's prompt already owns.

**See `docs/midday_signal_entry.md` for the full contract**: the payload shape,
the required `--deployed-last-7d-usd` figure, trend validation, the calibration
fields, and the document's own output fields. That page is authoritative; this
one covers the stream itself rather than its Gate 5 entry point.

Two notes specific to this stream, because they are the ways a correct-looking
run goes quietly wrong:

- **Trend validation applies here**, unlike at 0935ET. A candidate is auto-buy
  eligible only when at least `trend_min_bars` usable intraday bars resolve over
  the 60 minutes before 12:30, so the payload must carry 5-minute bars from
  11:30 ET onward and not just the decision bar. A truncated fetch does not
  error — it silently moves real buys into `manual_review_tickers`.
- **`--deployed-last-7d-usd` is required**, and is the trailing-7-day
  *afternoon-scheduled* deployment (`dca_risk.is_afternoon_scheduled_evidence`),
  not a sum across all seven slots. Take it from `dca_risk`'s report rather than
  re-deriving it, and check that report's `evidence` block first — see
  "Reading the deployment figure safely" below.

### Reading the deployment figure safely

`python3 -m dca_risk --state-root <cron tracking root> --as-of <YYYY-MM-DD>`
(console script `run-dca-risk`) reports `.scheduled_live.deployed_last_7d_usd`.
Resolve the state root via `dca_paths.cron_tracking_root()` and pass the **whole**
root: `build_risk_report` applies the afternoon predicate itself, so narrowing
the root by hand is a way to arrive at an under-count.

Every figure in that report is a sum over the run states it found, so a zero is
ambiguous on its own — it means "no capital deployed" and "no history was loaded"
equally well. A missing tree yields exit 0 and `deployed_last_7d_usd: 0`, and a
consumer treating that as confirmed spend leaves the kill switch disengaged and
deep-drop candidates uncapped.

The report's `evidence` block exists to tell the two apart. Abort rather than
size a run unless all of these hold:

| Field | Required |
| --- | --- |
| `state_root_exists` | `true` |
| `run_states_loaded` | `> 0` |
| `run_states_unreadable` | `== 0` |
| `run_states_unclassifiable` | `== 0` |
| `afternoon_states_seen` | `> 0` |
| `afternoon_states_undated` | `== 0` |
| `afternoon_states_invalid_notional` | `== 0` |

When `run_states_unclassifiable` is nonzero, `evidence.unclassifiable_reasons`
(keyed by the same paths as `unclassifiable_paths`) names why each one failed:
either it has no `run_classification` key at all (its top-level keys are
listed, as a hint that the field was renamed or the file predates it) or the
key is present but not an object. This is diagnostic only — it does not change
what the gate requires, and a plausible-looking reason is not grounds to trust
the file. Route the fix at whatever wrote the file, then re-run this check.

An unclassifiable file that provably carries no capital does not count toward
`run_states_unclassifiable`. It is listed separately under
`unclassifiable_states_without_deployment` /
`unclassifiable_without_deployment_paths` / `..._reasons`, which are reported
but not gated. "Provably" is strict, and the file's classification is never
consulted: `deployed_usd` is 0, no candidate has a deployed status or an
ambiguous-placement flag (even one with an unusable notional),
`deployed_usd_by_claim_date` attributes nothing, `orders_submitted_count` is
absent or exactly the integer `0`, and `order_submit_status` is absent or a
known not-submitted value. Any other signal keeps the file gated. The reason
is the same one that exempts out-of-window files: a file like this contributes
$0 whichever stream it is routed to. Gating on it kept the 12:30 slot aborting
on 2026-09-22 with `run_states_unclassifiable: 3`, four days after the
2026-09-18 Gate 9 template fix. The three malformed 9/15 to 9/17 stubs sit
inside the 30-day window until mid-October. A nonzero count here still means
some writer is emitting the wrong shape and should be fixed.

`afternoon_states_seen` is keyed on `is_afternoon_scheduled_evidence` — the same
predicate `deployed_by_day` applies — rather than on `classify_stream`, so the
two cannot disagree about what counts. The roster's own 10:30/12:30 artifacts are
valid scheduled evidence but are deliberately excluded from this figure, so a
morning-only or midday-only tree reports `afternoon_states_seen: 0` rather than
appearing to confirm a zero.

A state that clears that predicate but carries no attributable trading day —
`trading_day` missing or malformed, and no `whole_share_claim_breakdown` to date
its candidates by — is excluded from `afternoon_states_seen` and counted in
`afternoon_states_undated`. Nothing in the predicate reads `trading_day`, so such
a state would otherwise vouch for a zero that `deployed_usd_by_claim_date`
guarantees by dropping every candidate it cannot file under a date.

`afternoon_states_undated == 0` has to be asserted in its own right, which is why
it is a row above rather than a note. Excluding these states from
`afternoon_states_seen` is *not* sufficient: one healthy afternoon state
alongside an undated one still leaves `afternoon_states_seen > 0`, so the gate
passes while the undated state's real submitted notional goes missing from the
total.

A state can clear both of the above and still be unusable: an order-bearing
candidate (a real submitted/filled/pending/unconfirmed row) whose
`planned_amount_usd` is present but negative, non-finite, or nonnumeric is
silently skipped by `deployed_usd`/`deployed_usd_by_claim_date` — the state has
a real `trading_day` and counts toward `afternoon_states_seen`, but that one
candidate's real order contributes nothing to the total. `afternoon_states_
invalid_notional` counts states with at least one such candidate and, like
`afternoon_states_undated`, is NOT subtracted from `afternoon_states_seen` (its
other candidates, if any, are still real evidence) — it must be asserted `== 0`
in its own right.

One further field is reported for visibility rather than gated.
`unusable_states_outside_window` counts unreadable or unclassifiable artifacts
dated before the report's widest trailing window starts — 30 days, the
`rolling_cash_burn` horizon, not the kill switch's 7, since the report publishes
both. They cannot move any figure it carries, so blocking on them would wedge the
slot permanently on a legacy corrupt file.

Two things bound that exclusion. It is one-sided: an artifact dated *after*
`--as-of` still counts as unusable, because a reconciled whole-share row
attributes its notional to the earlier `trading_day` its claim breakdown records,
so a later run can carry in-window deployment. And it is conditional on the tree
honouring its own naming convention — a directory date is not an enforced
invariant, so if any readable state in the tree attributes capital to a day later
than its own directory claims, the heuristic is treated as untrustworthy there
and every excluded file is counted after all.

### Scope

1230ET only. The other roster slots run their own executor subclasses with
different decision anchors and ladders; pointing one slot's prompt at another
slot's producer would silently score it against the wrong logic. The first four
roster producers share one core (`scripts/stream_candidates.py`), so an eighth
slot is a `StreamSpec` plus a thin CLI module rather than a fresh build.
