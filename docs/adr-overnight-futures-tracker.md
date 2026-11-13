# ADR: Overnight futures tracker as an observe-only gap forecast

- **Status:** Accepted (2026-11-12, operator "accept the ADR with the proposed defaults"; every open
  question takes its proposed default). Not wired into any live path. Implementation follows the plan
  below, one draft PR per phase.
- **Scope:** research and reporting only, inside `dca-sizing-signals/`. The stdlib-only core, the
  ladder, `config/`, the live prompts, `sizing_adjustment_reader.py` and all three entry points
  (`run_dca.run_slot()`, the `MorningDCAExecutor` hierarchy, `dca_runner.py`) are untouched. Nothing
  here writes live sizing, moves a slot or places orders.
- **Source research:** `docs/research/2026-11-11-overnight-futures.md` ("the research"). Section
  references below (for example "research 1.2") point into that file; this ADR does not repeat its
  evidence.

## Context

- E-mini futures (ES, NQ, YM) lead price discovery for US indexes (Hasbrouck 2003). By the 9:30 ET
  open auction, the overnight move is mostly priced in. The evidence that an overnight move predicts
  anything a 9:35 buy-only order can exploit is weak and fragile (research 1.3).
- The overnight move does carry **dispersion** information: a large gap, or one with no scheduled
  catalyst, means a wider distribution for the 9:35 price. The ladder already sees "current vs last
  close", so the new information is whether that gap is large *for the night's context*.
- A naive "futures minus prior index close" is biased by the fair-value premium (carry minus
  dividends), which decays to expiry and steps up at each roll. A model trained on it learns the roll
  calendar as false signal (research 1.2).
- The repo has no futures data, no gap table and no expiry, rebalance or ex-dividend flags.
  `near_close_dca.py` has no ex-dividend handling, so an ex-date price drop at the open can read as a
  dip.

### What the research assumed, checked against the code

| Research assumption | What the code says (2026-11-12) | Effect on this ADR |
|---|---|---|
| `ForecastProvider` can carry P10/P50/P90 and an intraday horizon | `dca_shared_interfaces/forecasting.py`: `ForecastResult` has one `point_estimate` and an integer `horizon_trading_days`; its docstring says extra detail belongs in a provider-specific payload | D2: keep the interface unchanged; quantiles go in this provider's own table |
| `SizingAdjustmentReader` has a per-source weight that can be set to zero | `sizing_adjustment_reader.py` has no weights; it combines whichever suggestion files it is given, `most_conservative` by default | D2: observe-only is structural (no `SizingSuggester`, no suggestion file), not a zero weight |
| Yahoo is excluded by project policy | `docs/yfinance_replacement_plan.md`: yfinance is allowed for tasks that place no orders | D6: not used here, but because it lacks per-contract intraday futures history, not because of policy |
| The store uses idempotent upserts | `market_data_ingest/prices/store.py` is append-only and point-in-time (`as_of`, `ingested_at`, `run_id`, `content_hash`) | D5: follow the existing append-only convention |
| Ex-dividend dates need a new source | `corporate_actions` already stores `cash_dividend` rows with `ex_date` | D7: read ex-dates from the existing store |

## Decision

### D1. Purpose: gap-size and dispersion context, not direction

The tracker forecasts how wide the 9:35 (and 10:30) price distribution is relative to the prior
close. It never claims "buy now" or "wait". The point estimate is the fair-value-adjusted implied
open, which is a near-sufficient statistic for the gap; the value added is in the band.

### D2. Interface: `ForecastProvider` only, no interface change

- `OvernightGapForecastProvider` implements `ForecastProvider` and deliberately does **not** implement
  `SizingSuggester`, the same posture as Wondrous Octopus. It therefore cannot reach live sizing
  through any existing interface.
- `ForecastResult` is used as is: `horizon_trading_days = 0` means "this session",
  `point_estimate` is the implied 9:35 price relative to prior close (P50), `source =
  "overnight_futures"`.
- P10, P90, the gap-volatility scalar, the slot (`0935`, `1030`) and the input hash go in the
  provider's own `overnight_gap_forecast` table (research 1.7), not in `ForecastResult`.
- It publishes **no** suggestion file, and `sizing_adjustment_reader.py` is not told it exists.
  Turning it into a suggester later needs its own ADR (D8).

### D3. Package layout and boundaries

| New code | Location | Role | May import |
|---|---|---|---|
| `FuturesDataAdapter`, `FuturesSessionStore` | `dca-sizing-signals/market_data_ingest/futures/` | Load per-contract bars; append-only DuckDB writes | `market_data_ingest` only |
| `FairValueCalculator`, `FuturesOvernightTracker`, `CatalystCalendar`, `OvernightGapForecastProvider` | `dca-sizing-signals/overnight_futures/` | Pure computation over rows passed in by the caller | `dca_shared_interfaces` only |

- `overnight_futures` is added to `_OWN_PACKAGES` and `FORBIDDEN_IMPORTS` in
  `tests/test_architecture_boundaries.py`. It must not import `ml_forecaster`, `wondrous_octopus`,
  `connector_signals`, `comovement_signals`, `market_data_ingest` or `near_close_dca`. Every existing
  package gets `overnight_futures` added to its own forbidden set, so independence holds both ways.
- Rows arrive from the caller, as in `comovement_signals`, so the computation stays pure and testable
  without a database.

### D4. Definitions

Use the research's definitions (research 1.2, "Recommended definitions") without change:

- **Overnight move:** ln(F at 09:29:00 ET / F at 16:00:00 ET), one contract for the whole window,
  minus the change in fair value over the window.
- **Fair value:** CME formula, Cash x [1 + r x (days/360)] - expected dividend points to expiry.
- **Also:** post-close move (16:00 to 16:15 ET), implied and realized open gap, reversal types A and
  B, and gap fill by slot (09:35, 10:30, 12:30, 13:30).

Timestamp rules (research 1.6): every row carries `event_ts_utc` and `available_ts_utc`. Joins use
`available_ts_utc <= decision_ts`. Times are stored in UTC and converted with a tz database, never a
fixed offset. Exchange calendars for NYSE and CME decide half-days and holidays. Contracts are never
spliced inside an overnight window. Rolls follow the observed volume or open-interest crossover, and
the rule used is logged per roll.

### D5. Storage

Its own DuckDB file beside the price store, following the price store's conventions: append-only,
point-in-time (`as_of`, `ingested_at`, `run_id`, `content_hash`), with a "known at" view for reads.
Tables follow the research sketch (research 1.7): `futures_contract`, `futures_bar_1m`,
`futures_overnight_session`, `open_gap`, `reversal_event`, `catalyst_event`,
`overnight_gap_forecast`. Corrections append a new row; nothing is updated in place.

### D6. Data sources

| Need | Source | Note |
|---|---|---|
| Historical ES/NQ/YM per-contract bars | Databento CME Globex MDP 3.0, from June 2010 | Recommended primary (research 1.5); cost and licensing are open questions 2 and 3 |
| Index levels for anchors | Robinhood MCP `get_index_quotes` / `get_index_historicals` | Index-only, confirmed by the 2026-11-12 probe (open question 4). SPX and NDX are listed directly; the Dow is listed only as DJX (1/100 scale, per the symbol's usual definition; not checked against a published spec) |
| ETF open prints (SPY, QQQ, DIA) | The existing `market_data_ingest/prices` source | Reuses current ingestion |
| Ex-dividend dates | Existing `corporate_actions` (`cash_dividend`, `ex_date`) | No new source |
| Macro and earnings timing | BLS/BEA calendars, ALFRED first-release vintages, EDGAR acceptance time | Never current FRED values in a backtest |

yfinance is not used. It is permitted for tasks that place no orders, but it has no per-contract
intraday futures history with roll control, which D4 requires.

### D7. Calendar flags (descriptive only)

Logged per session to the forecast table and, once it exists, the Phase 0 outcome ledger
(`docs/adr-outcome-ledger-pit-harness.md`):

- index futures and options expiry (third Friday; settles in the open auction);
- roll window and days to expiry;
- S&P rebalance effective date (distorts the prior-close anchor);
- NYSE half-day;
- ex-dividend date for each active ticker, from `corporate_actions`.

These flags change nothing in the ladder. Teaching the ladder to discount an ex-date drop is a
separate decision for a separate ADR, because it changes live sizing on all three entry points.

### D8. Evaluation gate before any later wiring

Nothing in this ADR wires the forecast anywhere. A later ADR may propose a `SizingSuggester` wrapper
only after both of these hold:

1. **At least 6 months of live shadow,** with forecasts written before 09:35 ET each session.
2. **Out-of-sample results meeting the research thresholds** (research 1.6, "Evaluation"):

| Metric | Baseline | Pass |
|---|---|---|
| Pinball loss, P10/P50/P90 of the 9:35 price vs prior close | Wondrous Octopus without overnight features | Significant improvement (Diebold-Mariano or block bootstrap) |
| P10 to P90 coverage | Nominal 80% | 77 to 83%, also by volatility regime |
| Open-gap MAE | Zero gap; raw futures gap | Beats both |
| Gap-direction hit rate, 9:35 to 10:30 | 50% and the unconditional up-day rate | Reported only; never gates unless above about 55% |
| Reversal rates A and B | None | Reported with confidence intervals |

Splits are walk-forward: train 2010 to 2018, validate 2019 to 2021, test 2022 onward. Gap-size
buckets are pre-registered in the trial registry, because they are underpowered (research 1.6).

### D9. Fail closed

- Any missing input (bar, anchor, rate or dividend points) produces **no forecast row**. It is never
  filled with a default. `fv_quality = 'missing'` rows are stored but never forecast from.
- A missing catalyst flag is `unknown`, never "no catalyst".
- The provider never raises past the `ForecastProvider` boundary. On any upstream gap it returns no
  forecast, and callers treat that as "no information".
- Nights whose anchor is not a full NYSE session (half-days, holidays) are flagged and excluded from
  evaluation.

### D10. Dividend instruments

Dividend futures (SDA, FEXD), TRS and options as dividend hedges are institutional background
(research Parts 2 and 3). The retail system uses none of them. Only ex-date awareness (D7) carries
over.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Directional signal (fade or follow the gap at 9:35) | Evidence is practitioner-only, inconsistent and not out-of-sample (research 1.3) |
| Raw futures change instead of fair-value-adjusted | Roll-calendar sawtooth bias (research 1.2) |
| Add quantile and intraday-horizon fields to `ForecastResult` | Changes a shared contract that every sibling depends on; the docstring already routes extra detail to a provider payload |
| Inject overnight features into Wondrous Octopus | Couples two independent components; compare offline against the frozen baseline first |
| Implement `SizingSuggester` now with a small bound | No evidence yet that it improves anything; the shadow period comes first (D8) |
| Move the 9:35 slot after large gaps | Gap-fill evidence is folklore until `filled_by_1030` is measured; the overnight-premium literature suggests no cost-basis gain from waiting |

## Consequences

- **Positive:** a point-in-time futures and gap dataset that the outcome ledger can score against.
  Descriptive expiry, rebalance and ex-date flags become available to every later study. No live risk,
  because no interface reaches sizing.
- **Negative:** a vendor bill and licence terms to manage (open questions 2 and 3); a second DuckDB
  file to back up; at least 6 months before any result can affect sizing.
- **Neutral:** the core runtime stays stdlib-only. Any new dependencies live in
  `dca-sizing-signals/requirements.txt` only.

## Open questions

| # | Question | Decision (accepted proposed default) |
|---|---|---|
| 1 | How is "same session, 9:35 slot" expressed through `ForecastResult`? | `horizon_trading_days = 0`; slot in the provider table (D2) |
| 2 | Vendor for history, and how to load it without a new Python dependency? | Databento batch download in CSV encoding, loaded with DuckDB's CSV reader; the `databento` client only if a later need justifies it |
| 3 | Licence posture for storing raw futures bars locally? | Store only 1-minute bars and anchor-time trades needed by D4; confirm terms before the first download |
| 4 | Does the Robinhood MCP return futures quotes? | **Answered 2026-11-12: no.** See "Robinhood probe" below. Index levels only; every overnight futures price comes from the vendor (question 2) |
| 5 | Rate for fair value? | 3-month T-bill from ALFRED first-release vintages; SOFR term as a sensitivity check |
| 6 | Expected dividend points? | Trailing-yield approximation adjusted by known ex-dates from `corporate_actions`; record the method per row |
| 7 | Single-stock mapping? | Out of phase 1; index-level forecasts only. Later: overnight-only beta on point-in-time constituents |
| 8 | Weekends and holidays? | `hours_in_window` as a feature; flag and evaluate weekend gaps separately |
| 9 | Live shadow length? | 6 months minimum (D8) |

### Robinhood probe (open question 4, 2026-11-12)

The operator asked for one read-only check: index and search tools only, no orders. It ran at
about 22:30 ET on Thursday 2026-11-12, while ES and NQ trade on Globex.

**Facts**

- **No futures asset type.** `search` accepts only `instrument`, `currency_pair` and
  `market_index`. A request for `futures` is refused as an unsupported asset type, and the
  tool's own description says futures "will be added".
- **No ES, NQ or YM** in search results or in the 31-entry `get_indexes` list.
- **The list has:** SPX, NDX, DJX, XSP, XND, VIX, and FX, metal, commodity and crypto
  reference rates. It has no DJI.
- **The values are not live overnight.** `get_index_quotes` returned these exchange
  timestamps for Thursday 2026-11-12:

  | Index | Value | Exchange timestamp (ET) |
  |---|---|---|
  | SPX | 7592.4 | 16:58 |
  | NDX | 28289.19 | 17:15 |

  Neither value moved with the overnight session.

**Consequences**

- All overnight futures prices come from the vendor (D6 and question 2). Robinhood cannot stand
  in for the vendor, so the licence step stays the gate for real data.
- Robinhood can supply the cash-close anchors: SPX and NDX directly, and DJX for the Dow. A YM
  comparison against DJX needs the scale factor, which must be checked against a published
  definition before use.
- VIX is available. It is a possible later volatility feature, outside this ADR's scope.
- The search tool says futures will be added. Re-probe before relying on this answer if that
  changes.

## Implementation plan (one draft PR per phase)

1. **Boundaries first:** add `overnight_futures` to the architecture-boundary test, with an empty
   package, so the rules hold before any code lands.
2. **Pure computation, tests first:** `FairValueCalculator` (CME worked example as a fixture: cash
   1146.00, r 5.7%, 78 days, dividends 3.47 gives 1156.68) and `FuturesOvernightTracker` on synthetic
   rows, covering roll nights, half-days, DST weeks, missing inputs and weekend windows.
3. **Ingestion:** `market_data_ingest/futures/` with the append-only store, loaded from a small
   licensed sample before any full backfill.
4. **Offline evaluation:** walk-forward run, written to the trial registry, against D8.
5. **Live shadow:** a daily task that places no orders writes forecasts before 09:35 ET; the outcome
   ledger scores them.

## Verification

- `pytest dca-sizing-signals/tests` passes, including the boundary test with `overnight_futures`
  registered both ways.
- A grep shows no import of `overnight_futures` from the core or from `sizing_adjustment_reader.py`.
- On a known roll night, the fair-value-adjusted overnight move has no step at the roll, while the raw
  change does.
