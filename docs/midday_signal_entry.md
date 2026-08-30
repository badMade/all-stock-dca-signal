# Gate 5 signal generation for the 12:30 PM midday slot

`scripts/generate_midday_candidates.py` is the signal-generation entry point for
cron `a6239dae` (the 12:30 PM ET midday live buy, `run_type=midday_dca_live`).

Before it existed, that slot's Gate 5 aborted every weekday with *"no
signal-generation entry point exists for this slot; nothing to buy"* — the run
cleared every read-only gate, fetched the week's order history, and then stopped
because there was no candidate producer to call.

It is the sibling of `docs/market_open_signal_entry.md` (0935ET). Both run on the
shared core in `scripts/stream_candidates.py`; this page covers only what is
different about midday. Read the market-open page, or the shared module's
docstring, for the market-data payload contract and the fail-closed input rules —
they are identical.

```bash
python3 -m scripts.generate_midday_candidates \
    --trading-day 2026-08-26 \
    --market-data $ROOT/market_data_1230ET.json \
    --deployed-last-7d-usd 850.00
```

`$ROOT` is this slot's own evidence directory,
`dca_paths.stream_root('midday_dca', '<YYYYMMDD>_1230ET')` — the same one Gate 4
already writes `orders_week.json` under. `stream_root()` joins the text it is given verbatim -- it does not normalize the date -- so the COMPACT `YYYYMMDD` form is required: a dashed `<YYYY-MM-DD>` suffix silently creates a sibling directory that the pointer and artifact consumers never scan (Codex review: "Document the compact artifact directory suffix").

## Why it calls `evaluate()`, not `run()`

The 1230ET prompt's Gate 5 named the open question directly: whether
`MiddayDCAExecutor.run(now, data, broker=...)` should be wired in as the producer
is a human decision, because `run()` owns budget arithmetic, the same-day top-up
guard, placement, drift detection and the artifact tree — Gates 6 through 9 —
which the prompt's own gate sequence also owns. Wiring it in would put two
independent implementations of those gates on the same account in one fire.

Calling `MiddayDCAExecutor.evaluate()` avoids the question entirely. It is the
same real signal path — the shared 0x→5x ladder, per-ticker calibration, the
dynamic $1/$2 base, the minimum-drop floor, the decision-price resolution and
this slot's trend validation — with no broker, no credentials, no network, no
orders, and no artifact writes. Gates 6, 6.5, 6.7 and 7 stay exactly where the
prompt already has them, and Gate 6.5 remains the last word on size.

`config/midday_dca.json` ships `live_execution_enabled=true` (midday is one of
the currently-functioning live streams). That still gates placement *inside*
`run()`, which this command never calls, so it neither enables nor suppresses
anything here.

## Two things that differ from the 0935ET entry point

### 1. Trend validation applies — so the payload needs an hour of bars

`config/midday_dca.json`'s `confidence_rule` and `MIDDAY_PROFILES`'
`validate_trend: True` mean a candidate whose price and drop qualify is auto-buy
eligible **only** when at least `trend_min_bars` (6) usable intraday bars resolve
over the 60 minutes before 12:30. One whose trend cannot be validated is flagged
`manual_review_candidate`: it stays in `candidates` and `manual_review_tickers`,
and is deliberately excluded from `cands` and `same_day_top_up_candidates`.

**Fetch intraday bars from 11:30 ET onward, not just around the decision time.**
A truncated fetch does not error — it quietly moves real buys into manual review.
This is the opposite of 0935ET, which is trend-*exempt* precisely because its
60-minutes-back bar would be pre-market and cannot exist.

### 2. `--deployed-last-7d-usd` is required

The prompt's Gate 5 makes the 7-day deployment kill switch the producer's
responsibility, and says why: *"nothing downstream of Gate 5 in this prompt
enforces this … a producer that omits this step can carry a deep-drop 5x
candidate all the way to Gate 7 uncapped."* So the flag has no default — silently
assuming zero would disable the guard on exactly the day it exists to catch — and
a NaN or negative value is refused, since either compares False against every
threshold check.

Pass the trailing-7-day **afternoon-scheduled** deployment, not a sum across all
seven slots. It comes from the repo's own afternoon **run-state artifacts**, not
from broker order rows: `dca_risk.is_afternoon_scheduled_evidence` takes a
run-state document carrying a validated `run_classification`, and a raw
`get_equity_orders` row has neither that classification nor the deployed-amount
fields — applying the predicate to broker rows rejects every one and yields 0,
silently disabling the guard on a week with real deployment.

```bash
python3 - <<'PY'
import datetime, glob, json, zoneinfo
import dca_paths, dca_risk, dca_runner

today = datetime.datetime.now(zoneinfo.ZoneInfo('America/New_York')).date()
roots = [dca_paths.stream_root('ce16ba6b', 'scheduled_runs'), dca_paths.stream_root('intraday_dca')]
states = [json.load(open(path, encoding='utf-8')) for root in roots for path in glob.glob(str(root) + '/**/run_state.json', recursive=True)]
window_start = today - datetime.timedelta(days=dca_risk.KILL_SWITCH_WINDOW_DAYS - 1)
expected_pairs = set()
for offset in range(1, dca_risk.KILL_SWITCH_WINDOW_DAYS):
    day = today - datetime.timedelta(days=offset)
    status = dca_runner.market_day_status(day)
    if not status.is_closed and not status.is_early_close:
        expected_pairs.update({(day, '14:30'), (day, '15:30')})
observed_pairs = set()
deployment_evidence_states = []
for state in states:
    trading_day = dca_risk._trading_day(state)
    classification = state.get('run_classification')
    state_day = state.get('trading_day')
    classification_day = classification.get('trading_day') if isinstance(classification, dict) else None
    scheduled_for = classification.get('scheduled_for_et') if isinstance(classification, dict) else None
    is_afternoon_evidence = dca_risk.is_afternoon_scheduled_evidence(state)
    is_validated_evidence = (is_afternoon_evidence and trading_day is not None
                             and state_day == trading_day.isoformat() == classification_day)
    is_attributable_reconciliation = (is_afternoon_evidence and trading_day is None
                                      and dca_risk.has_attributable_deployment_date(state))
    if is_validated_evidence or is_attributable_reconciliation:
        deployment_evidence_states.append(state)
    if is_validated_evidence and window_start <= trading_day < today and scheduled_for in ('14:30', '15:30'):
        observed_pairs.add((trading_day, scheduled_for))
missing_pairs = sorted(expected_pairs - observed_pairs)
if missing_pairs:
    raise SystemExit(f'incomplete trailing-7-day afternoon evidence; missing scheduled fires: {missing_pairs}')
print(round(dca_risk.seven_day_deployed(dca_risk.deployed_by_day(deployment_evidence_states), today), 2))
PY
```

Both roots are required: `is_afternoon_scheduled_evidence` accepts scheduled
evidence at or after 2:30 PM ET, which includes the 2:30 intraday slot whose run
states live under `intraday_dca` — not just the 3:30 final stream. The predicate
filters what does not qualify, so listing a root that usually contributes nothing
is safe; omitting an eligible one silently undercounts. The command reconciles the
complete expected set of `(market day, scheduled_for_et)` pairs: 14:30 and 15:30
for every completed full market day in the trailing window. Today, weekends,
holidays, and early-close days are excluded. It prints the figure only after every
pair is observed; a missing artifact exits nonzero and must abort the run rather
than substitute $0.00. This prevents duplicate artifacts for one slot or a single
daily artifact from masking a missing afternoon fire.

The guard is applied by handing `near_close_dca.graded_buy_multiplier` the real
inputs — the allocation and fraction come from `load_strategy_config`, i.e. the
same `config/budgets.json`-derived threshold the final-buy stream uses — so the
cap fires inside the real ladder function rather than being recomputed here. The
one piece of sizing math the prompt singles out as unenforced anywhere else does
not get a second implementation.

Note the direction of the effect: the kill switch only ever *caps*. A run through
this entry point sizes at or below what `MiddayDCAExecutor.run()` would produce,
never above. The document reports both facts separately — `kill_switch_evaluated`
(the producer applied the guard) and `kill_switch_engaged` (the threshold is
actually crossed, read back through `near_close_dca.kill_switch_engaged`).

## The trend window must be *spanned*, not just sampled

`MorningDCAExecutor._trend_validated` asks two things of the 60 minutes before
the decision anchor: are there at least `trend_min_bars` (6) usable bars in it,
and is the last close below the first. It does **not** ask whether those bars
*span* the window. Six consecutive 5-minute bars covering only the final 25
minutes satisfy both tests, so a truncated payload yields
`trend_validated: true` and an `auto_buy_eligible` row at full ladder size on a
fraction of the evidence the config asks for.

That rule belongs to the live executor and is shared by every trend-validating
stream, so this entry point does not change it — tightening it would alter live
eligibility on the in-repo executor path too, which is a sizing-policy decision
with money attached. What the entry point does instead is refuse to **publish**
such a row: it is kept in `candidates` with the executor's own verdict intact,
withheld from `cands` and `same_day_top_up_candidates`, and named in
**`withheld_incomplete_trend_window_tickers`**.

- The check is per-ticker and withholds rather than aborting. It cannot
  distinguish "the caller fetched too narrow a range" from "this symbol did not
  trade early in the window", and does not try to — both mean the downtrend was
  never observed over 60 minutes — so one thinly-traded symbol must not take the
  whole universe down with it.
- One interval of tolerance applies at the window start, because a provider that
  stamps bars at interval *end* puts a complete window's first bar one interval
  late.
- These rows are **not** `manual_review_candidate`s. The fix is a wider
  re-fetch and a re-run, not a human judgement call, so they are reported
  separately and must not be routed to the same queue.
- The list is always empty on the trend-exempt 0935ET slot, where a full window
  would start pre-market and is impossible by construction.

`buy_count > 0` with an empty `cands` is exactly this case (or its sibling,
`withheld_contradictory_base_tickers`). Check both before reading a short list
as a quieter tape.

## The `cands` list

The document's `cands` entries are the prompt's `$ROOT/cands.json` payload,
already carrying every field its Gate 5 requires:

```json
{"ticker": "AAPL", "desired_usd": 5.0, "current_price": 95.0,
 "prior_close": 100.0, "prior_low": 98.0, "selected_base_amount_usd": 1.0,
 "multiplier": 5.0, "drop_pct": 5.0,
 "sizing_base_usd": 1.0, "sizing_min_drop_pct": 4.4288, "sizing_max_drop_pct": null}
```

`prior_close`, `prior_low` and `selected_base_amount_usd` are required rather than
nice-to-have: Gate 7's final pre-placement quote refresh re-checks "still below
prior close" **and** re-derives the dynamic $1-vs-$2 base, and has no other source
for either. The multiplier is not the only half of `base × multiplier` that can go
stale between Gate 5 and Gate 7.

`prior_low` can legitimately be `null`, with `unknown_prior_low: true` alongside
it. The live Robinhood adapter stamps `low=0.0` on a quote-derived previous-close
bar as an explicit "unknown" sentinel, which the executor turns into no prior low
at all. Such a row is still a real, correctly sized buy, and the rule for it is
narrow: with an unknown low the base can only ever be the conservative
below-close one, and **no refreshed price can upgrade it** — the comparison that
would raise it to the full base is exactly the one that cannot be made. So the
base is already final, Gate 7's refresh reduces to the prior-close check alone,
and the size cannot go stale in the unsafe direction. A row claiming the full
base *with* an unknown low is a contradiction; those are withheld from `cands`
and `same_day_top_up_candidates` and named in
`withheld_contradictory_base_tickers` for manual handling rather than published.

Every field of a `cands` row must be carried through each later file unchanged.
Gate 7's final base validation compares the refreshed price against `prior_low`,
and where `unknown_prior_low` is true there is no prior low to compare against —
that row's base is the conservative one by construction and cannot be upgraded by
any later price, so the flag is what tells Gate 7 to check `prior_close` alone
rather than abort or guess. A row arriving at placement without these fields
should be dropped, not placed on assumptions.

The three `sizing_*` fields appear only for a calibrated ticker, and are read
through `dca_runner._configured_calibration_bounds` — the same function
`plan_orders()` validates them against — rather than re-derived from
`config/per_ticker_sizing.json`. A second derivation could disagree with the
validator about exactly the rows these fields exist to make placeable. When the
selected base is not one of the ticker's configured bases the fields are withheld
and the row reads as uncalibrated, which is the conservative direction.

`same_day_top_up_candidates` is the same list in Gate 6.5's own
`{ticker, desired_usd, current_price}` shape. Per that gate's contract you must
still re-fetch each survivor's quote immediately before placement and overwrite
`current_price` — the price here is provisional.

`buy_count == 0` is a legitimate quiet tape **only when `provider_error_count` is
0**. Per-ticker fetch failures are reported, not fatal — the command still exits 0
— so a zero-candidate document from a broken fetch is otherwise indistinguishable
from a healthy quiet tape. Alert on a nonzero `provider_error_count` instead of
suppressing the notification. `manual_review_tickers` is different and is not a
provider error: those candidates qualified on price but could not have their trend
validated. Report them; never place them.

## Wiring it into the 12:30 task

The live task's prompt text lives in Cowork, not in this repo, and
`claude/dca-executor-prompts/live-mirror/1230ET-*.txt` is already a *proposed*
rewrite rather than a verified capture (see
`claude/dca-executor-prompts/README.md`). Its Gate 5 now names this command; until
a human applies that body through `update_scheduled_task` and the mirror is
re-captured, the live 12:30 task still aborts at Gate 5 exactly as before.

Gate 5 in that file now reads: fetch the universe's 5-minute bars from **11:30 ET**
through the 12:30 anchor, the previous session's daily bar, and a current quote;
save them as `$ROOT/market_data_<YYYY-MM-DD>_1230.json`; run the command above with
the trailing-7-day afternoon deployment; save `cands` to `$ROOT/cands.json`. Gates
6, 6.5, 6.7 and 7 are unchanged.

## Scope

This covers **1230ET only**.

All **seven** roster slots now have a Gate 5 entry point:

| Slot | Command | Page |
|---|---|---|
| 0935ET | `scripts/generate_market_open_candidates.py` | `docs/market_open_signal_entry.md` |
| 1030ET | `scripts/generate_morning_candidates.py` | `docs/morning_signal_entry.md` |
| 1130/1330/1430ET | `scripts/generate_intraday_candidates.py --slot …` | `docs/intraday_signal_entry.md` |
| 1230ET | `scripts/generate_midday_candidates.py` | `docs/midday_signal_entry.md` |
| 1530ET | `scripts/generate_buy_candidates.py` | `docs/strategy.md` |

The first four run on the shared core in `scripts/stream_candidates.py`; 1530ET
predates it and calls `near_close_dca.evaluate_source_signal`, that stream's own
engine. Each command is scoped to its own slot: pointing one at another slot
scores against the wrong logic.
