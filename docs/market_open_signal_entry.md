# Gate 5 signal generation for the 9:35 AM market-open slot

`scripts/generate_market_open_candidates.py` is the signal-generation entry point
for cron `2ba19526` (the 9:35 AM ET market-open live buy, `run_type=market_open_dca_live`).

Before it existed, that slot had no callable entry point. Every executor prompt's
Gate 5 says "Run the repo's own signal generation for this slot ... Use the repo
entry point, never a reimplementation", and for 0935ET there was nothing to run:
the only Gate 5 entry point in the repo, `scripts/generate_buy_candidates.py`, is
scoped to 1530ET and calls a signal function this slot does not use. So a session
that cleared every safety gate still ended at Gate 5 with `GATE5_DISABLED`, placed
nothing, and alerted — every single fire. That is the gap this closes, for this
slot only.

## What it is

A no-network, no-broker, no-credentials command that runs the **real**
`market_open_dca.MarketOpenDCAExecutor` over market data you have already fetched,
and prints the resulting candidate list as JSON.

```bash
python3 -m scripts.generate_market_open_candidates \
    --trading-day 2026-08-26 \
    --market-data $ROOT/market_data_0935ET.json
```

`$ROOT` is this slot's own evidence directory, resolved through the shared helper
rather than written out:

```bash
python3 -c "import dca_paths; print(dca_paths.stream_root('market_open_dca', '<YYYYMMDD>_0935ET'))"
```

Compact date, no dashes — that is the directory the repo's own code writes
(`morning_dca.run_state_dir`) and the one `config/market_open_dca.json`'s
`state_file_guarantee` names. Never a bare repo-relative `artifacts/` directory,
and never a literal `/home/user/workspace/cron_tracking/...` path: `dca_paths`
owns the default and honours `$DCA_CRON_TRACKING_ROOT`, so a hand-written path
splits this evidence from the tree every other consumer scans the moment either
changes.

Optional flags:

| Flag | Meaning |
|---|---|
| `--config-dir DIR` | Defaults to the resolved `config/`. A directory that is missing or incomplete aborts rather than silently falling back to hardcoded defaults. |
| `--already-covered-tickers T1 T2` | Gate 4's compatible-stand-down case: tickers a compatible executor already placed an order for in this same window. They are never scored and never published. |

Exit status 0 means the document on stdout is valid. Anything else — malformed
input, a failed fetch reported in `source_errors`, an unusable `--config-dir` — is
Gate 5's *abort and alert* branch, not its *end silently* branch.

## Why it could not just reuse the 1530ET entry point

`scripts/generate_buy_candidates.py` calls `near_close_dca.evaluate_source_signal`,
which is the real engine for exactly one stream: the 3:30 PM final buy. Market-open
runs on `morning_dca.MorningDCAExecutor.evaluate_signal` as specialized by
`MarketOpenDCAExecutor` — a different method, a different result dataclass
(`MorningSignal`, not `SourceSignal`), a single combined market-data provider
instead of a Finance/Robinhood pair to reconcile, and this slot's own
trend-validation exemption. Pointing 0935ET at the 1530ET module would have been a
fourth, non-matching implementation of the signal logic, which is exactly what
Gate 5 forbids.

This module instead constructs the executor and calls its own evaluation:

```python
executor = MarketOpenDCAExecutor(config=config)
signals = executor.evaluate(now, provider)
```

Everything that decides what to buy and how much — the shared 0x→5x ladder,
per-ticker calibration, the dynamic $1/$2 base, the minimum-drop floor, the
decision-price resolution, the trend exemption — comes from that call. There is no
second copy to drift.
`scripts/tests/test_generate_market_open_candidates.py::test_document_matches_the_executor_called_directly`
is what keeps it that way.

## What it does not do

Gate 5 and nothing past it:

- No budget tiers (Gate 6), no same-day top-up sizing (Gate 6.5,
  `scripts/same_day_top_up_gate.py`), no spread check (Gate 6.7), no placement
  (Gate 7). Its output is a candidate list, not an order list.
- No run artifacts. It calls `evaluate()`, not `run()`, so it needs no broker and
  writes nothing under the artifact tree. The prompt's own Gate 9 still owns this
  slot's `run_state.json` evidence.
- No kill-switch input. The 7-day kill switch is funded by afternoon-scheduled
  evidence only (`dca_risk.is_afternoon_scheduled_evidence`) and is inert on this
  stream inside the live executor too, so this entry point deliberately has no
  `--deployed-last-7d-usd` flag. Accepting one would make it size differently from
  the executor it exists to mirror. See `docs/risk_management.md`'s stream-hygiene
  section.

### `live_execution_enabled` does not gate this command

`config/market_open_dca.json` ships `live_execution_enabled=false`. That flag gates
**placement inside `MarketOpenDCAExecutor.run()`**, which this module never calls.
The flag's value is echoed into the output document so a reader can see it, but a
`false` value does not make the candidates hypothetical — they are this slot's real
buy list. Who may act on it is the prompt's Gate 7 question.

Two separate flags still describe the slot as executor-disabled, and they must be
flipped together, by a human, after a trading day of shadow output has been diffed
against what actually traded the slot (see `docs/safeguards.md#live-execution-roster`):

- `config/market_open_dca.json` → `live_execution_enabled: false`
- `config/scheduled_tasks.json` → task `2ba19526` → `no_python_entry_point: true`

`test_config_invariants.py` pins them to move together, so that neither a scheduler
routing on one flag nor a reader trusting the other can end up believing live
placement happens through the in-repo executor when it does not.

## Input file shape

The same envelope `scripts/generate_buy_candidates.py` reads, narrowed to the one
source this slot runs on:

```json
{
  "sources": {
    "robinhood": {
      "AAPL": {
        "previous_close_bar": {"timestamp": "2026-08-25T16:00:00-04:00",
                               "open": 101.0, "high": 102.0, "low": 99.5,
                               "close": 100.0, "volume": 8421.0},
        "intraday_bars": [{"timestamp": "2026-08-26T09:30:00-04:00",
                           "open": 96.0, "high": 96.4, "low": 95.5,
                           "close": 95.8, "volume": 12000}],
        "quote": {"price": 95.6, "as_of": "2026-08-26T09:34:50-04:00"}
      }
    }
  },
  "source_errors": [],
  "ticker_errors": {"robinhood": []}
}
```

Mapping from the Robinhood MCP responses:

| Field here | Robinhood source |
|---|---|
| `previous_close_bar` | `get_equity_historicals` (`interval=day`), the last **completed prior session** bar: `begins_at`→`timestamp`, `open_price`/`high_price`/`low_price`/`close_price`→`open`/`high`/`low`/`close` |
| `intraday_bars` | `get_equity_historicals` (`interval=5minute`, `span=day`), 09:30 ET through the 09:35 decision anchor, same field mapping |
| `quote` | `get_equity_quotes`: `last_trade_price`→`price`, the quote's own as-of timestamp→`as_of` |

Rules worth knowing before you build the file — all of them fail closed, and all of
them are inherited verbatim from the 1530ET entry point's parser, so a file valid
for one is valid for the other:

- Prices must be finite and strictly positive. `previous_close_bar.low` may be `0`
  to mean "prior low unknown" (the live adapter's own sentinel; it selects the
  smaller $1 base).
- Timestamps must be timezone-aware.
- `previous_close_bar` must be dated **exactly** the previous market session per
  `config/market_calendar.json` — not merely some earlier date.
- Duplicate intraday timestamps, duplicate JSON keys, and internally inconsistent
  OHLC bars are rejected.
- A `quote` is honored only when its `as_of` is present, not in the future, and
  within one 5-minute interval of the decision moment; otherwise the completed
  09:30 bar's close is the decision price, exactly as live.
- `sources.robinhood` is required and is the **only** permitted source key. A
  `finance` payload is rejected rather than ignored: the executor evaluates one
  provider, so supplying a second one would look like a dual-source cross-check
  that never happened.
- Listing `"robinhood"` in `source_errors` **aborts**. This slot has one source, so
  a failed fetch leaves nothing to score, and a zero-candidate document would be
  indistinguishable from a genuinely quiet tape that Gate 5 ends silently on.
- `ticker_errors.robinhood` is the per-symbol counterpart: a fetch *exception* for
  one ticker (as opposed to a ticker the source simply does not resolve, which you
  spell by leaving it out). It scores `NO_DATA` with the provider error recorded.

Note what this slot does **not** need: the 60-minutes-back lookback bar the 1530ET
entry point requires. At 09:35 that bar would be at 08:35, pre-market, and cannot
exist — which is precisely why this slot is exempt from the 60-minute downtrend
filter (`config/scheduled_tasks.json:2ba19526.trend_validation_exemption`).

## Output document

```json
{
  "slot": "0935ET", "runner_slot_id": "0935", "stream": "market_open",
  "cron_id": "2ba19526", "trading_day": "2026-08-26", "decision_time_et": "09:35",
  "source": "robinhood", "trend_validation_exempt": true,
  "live_execution_enabled": false,
  "evaluated_count": 58, "buy_count": 2,
  "manual_review_count": 0, "manual_review_tickers": [],
  "provider_error_count": 0, "provider_error_tickers": [],
  "no_data_count": 3, "no_data_tickers": ["GFUZ", "..."],
  "candidates": [ { "ticker": "...", "action": "BUY", "buy_multiplier": 2.5,
                    "suggested_amount_usd": 5.0, "selected_base_amount_usd": 2.0,
                    "target_price": 95.0, "drop_vs_previous_close_pct": 5.0,
                    "execution_status": "auto_buy_eligible", "warnings": [] } ],
  "same_day_top_up_candidates": [ {"ticker": "...", "desired_usd": 5.0, "current_price": 95.0} ],
  "planner_candidates": [], "planner_incompatible_tickers": ["..."],
  "planner_config_mismatch": false,
  "already_covered_tickers": []
}
```

The lists a caller acts on:

- **`cands`** is the Gate 6.5 handoff — take the buy list from here, not from
  `same_day_top_up_candidates`. Both name the same tickers at the same amounts,
  but Gate 6.5's drop-only re-check needs `prior_close`, `prior_low`,
  `unknown_prior_low` and `selected_base_amount_usd`, and drops any row missing
  them; the thin list carries none of those, so handing it over would discard
  every candidate on every fire. `cands` is a superset that already contains
  `{ticker, desired_usd, current_price}` in the shape
  `scripts/same_day_top_up_gate.py`'s `--candidates` input requires.
- **`same_day_top_up_candidates`** remains for a caller that genuinely wants only
  the three top-up-gate fields. Per Gate 6.5's contract the price in either list
  is provisional — re-fetch each survivor's quote immediately before placement.
- **`planner_candidates`** is an optional convenience for a caller driving
  `dca_runner.plan_orders()`. It is deliberately *not* a filter on the list above:
  `plan_orders()` validates multipliers against only the global
  `config/sizing_ladder.json`, with no awareness of the per-ticker calibration the
  executor's own sizing respects, so a calibrated ticker's real, correct BUY is
  routinely reported in `planner_incompatible_tickers`. On the 1530ET path those two
  lists are coupled because its Gate 6 *is* `plan_orders()`. The market-open prompt's
  Gate 6 is not — it sizes from paginated broker history and `config/budgets.json` —
  so coupling them here would empty the one file Gate 6.5 consumes, on ordinary days,
  for a tape that genuinely qualified.

  It *is*, however, built from the same withheld-filtered rows `cands` is. A row
  suppressed for a contradictory base or an incomplete trend window must not
  reappear here — `planner_candidates` is a placement input, so leaving a
  withheld row in it would let a `plan_orders()` caller place the exact order the
  withholding suppressed (Codex review: "Exclude withheld rows from planner
  candidates"). The two lists differ only in the planner-compatibility filter,
  never in the safety withholding.

`buy_count == 0` is a legitimate quiet tape **only when `provider_error_count` is
0**. Per-ticker fetch failures are reported, not fatal — the command still exits
0 — so a zero-candidate document from a broken fetch looks exactly like a quiet
tape unless the caller checks. Alert on a nonzero `provider_error_count` (and on
an unexpectedly large `no_data_count`) instead of ending silently.

## Wiring it into the 9:35 task

The live 9:35 task's prompt text lives in Cowork, not in this repo;
`claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt` is a
read-only mirror of it, and `claude/dca-executor-prompts/1-executor-0935-cron_35_13.txt`
is the roster copy. Editing either file changes nothing about what the scheduled task
does — the live task must be repasted (see `claude/dca-executor-prompts/0-setup-card.md`).

The updated Gate 5 body — along with the four other sections that changed with it
— is in `claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt`.
Apply that file, not a paraphrase of it: a second copy of prompt text in this doc
is exactly the kind of duplicate that drifts. Its header comment lists everything
that moved. In summary, Gate 5 now resolves `$ROOT` through `dca_paths`, fetches
the universe's bars and quotes into `$ROOT/market_data_<YYYY-MM-DD>_0935.json`,
runs the command above, and saves the document to
`$ROOT/candidates_<YYYY-MM-DD>_0935.json`. Four rules in it are worth knowing
before you apply it:

- **Pass `--already-covered-tickers`** whenever Gate 4 ended in the
  compatible-executor continue branch. Omitting it republishes tickers that
  executor already bought in this same window, which
  `one_order_per_ticker_per_stream` forbids at any size and Gate 6.5 will not
  catch — that gate sizes repeats across *different* slots.
- **A provider-error run is not a quiet tape.** End silently on `buy_count: 0`
  only when `provider_error_count` is also 0; otherwise alert.
- **Gate 6.5 applies a drop-only re-check** against the refreshed quote, using
  the fields each row already carries. A row on the full ($2.00-scale) base
  qualified below *both* the prior close and the prior low, so it is dropped
  unless the fresh price is still below both; a row on the reduced base, or with
  `unknown_prior_low`, qualified on the prior close alone and is dropped only if
  the fresh price is no longer below it. Never resized — dropping is the
  conservative direction and needs no sizing math in the prompt.

  An earlier revision told the executor to *re-run this command* on the refreshed
  quotes instead. **That does not work, and must not be restored:** the entry
  point evaluates at the slot's fixed 09:35 anchor, and both
  `_StaticMarketData.get_quote` and `MorningDCAExecutor._current_price` reject a
  quote timestamped after that anchor — so a re-run silently falls back to the
  same completed 09:30 bar and returns byte-identical sizing while appearing to
  have refreshed. The drop-only rule closes the gap that re-run was meant to
  close (a recovered ticker can no longer carry its $2.00-base size into
  placement) without pretending a refresh happened.

  Because the step only drops, it can never exceed what Gate 6 authorized, so no
  second budget pass is needed.

  **Two known limitations remain open for a human decision**, both recorded in
  the prompt itself so nobody reads the step as safer than it is:

  1. The rule catches a candidate that recovers past its close/low *references*,
     not one that recovers *within* them. A ticker that qualified on a 5% drop
     and has since recovered to 2% is still below both, so it survives carrying
     its 5x-sized amount into placement. This is not a regression from wiring
     Gate 5 up — the refresh step has always kept Gate 5's amount and dropped
     only on lost qualification — but it is now reachable on every fire that
     places. Closing it needs the ladder re-evaluated at the refresh moment,
     i.e. a real post-anchor evaluation mode in the entry point, since a second
     decision anchor changes which bars are complete and therefore which
     multiplier applies. That is a live-sizing change, not prompt wording. The
     conservative interim alternative is one line: drop every row whose
     refreshed price is not at least as deep as the drop that sized it.
  (A second limitation recorded here previously — that the candidates file
  carried no `prior_logged_reference_price`, leaving `same_day_top_up_gate.py`
  on the broker's average *fill* price when a hand-placed market-open recovery
  buy happened earlier the same day — is **closed**. It was recorded as needing
  plumbing rather than wording, and that was wrong: the 1230ET prompt already
  closed the same gap in wording alone, and the mechanism it uses is generic.
  Gate 6.5 in all six executor-class prompts now performs a mandatory scan of
  its own slot's prior-allocation roots — for this slot the three
  `market_open_dca.default_market_open_prior_allocation_roots()` returns — and
  passes the validated `prior_logged_reference_price` together with the
  `prior_logged_reference_order_id` the gate requires to bind it. The gate
  honours the override only when that ID matches the broker's own latest
  qualifying order for the ticker, so an unpaired price is ignored rather than
  applied, and the broker-fill fallback survives only for a completed scan that
  found no validated match, or found one carrying no logged price.)
- **Gate 9 writes the artifact set** (`run_state.json`, `run_summary.json`,
  `market_open_signals.csv`, header-only-when-empty `submitted_orders.csv`, plus
  the `latest_market_open_run.json` pointer) on every outcome, into that same
  `$ROOT`. It previously only notified, which left a successful run's real orders
  invisible to reconciliation, drift detection and every later slot's
  prior-allocation scan.

Gates 6, 6.7, 7 and 8 are unchanged, and Gate 6.5 remains the last word on size.

## The trend-window coverage check does not apply here

The sibling slots withhold an auto-buy row whose bars do not reach back to the
start of the 60-minute trend window, naming it in
`withheld_incomplete_trend_window_tickers` (see
`docs/midday_signal_entry.md`). At 09:35 that window would start at 08:35,
pre-market, so a full window is impossible by construction and the rule would
withhold every candidate on every fire. It is keyed off the profile's own
`validate_trend` flag, so this slot is exempt from it exactly as it is exempt
from the trend rule itself, and the list is always empty here.

`withheld_contradictory_base_tickers` **does** apply, so `buy_count > 0` with an
empty `cands` is still possible on this slot.

## Scope

This covers **0935ET only**.

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

