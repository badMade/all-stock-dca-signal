# Overnight Futures, Index Arbitrage, and Dividend Hedging: Research Basis for an all-stock-dca ADR

Status: research input only. Not an ADR, not a decision, not wired into anything.
Date: 2026-11-11
Origin: Claude research task (session https://claude.ai/code/session_01KWTqCe24973qRoX66Rbqmc)
Feeds: the accepted ADR [`docs/adr-overnight-futures-tracker.md`](../adr-overnight-futures-tracker.md) (written 2026-11-12; its "What the research assumed" table corrects four assumptions below against the code).

**Bottom line:** Build the overnight futures tracker as a new, suggest-only `ForecastProvider` that forecasts open-gap size and early-session volatility. Do not build it as a directional alpha signal. The evidence is strong that E-mini futures lead price discovery and that overnight information is already mostly priced in by 9:30 AM ET. Evidence that overnight moves predict anything a 9:35 AM buy-only DCA order can exploit is weak and fragile. Parts 2 and 3 are mostly institutional background. Three things carry over to a retail DCA: fair-value-adjusted gap context, awareness of expiry and rebalance days, and ex-dividend date awareness.

## TL;DR

- **Part 1:** Measure the overnight move as fair-value-adjusted ES/NQ/YM change from the 4:00 PM ET cash close to 9:29 AM ET. Wrap it in new objects (`FuturesSessionStore`, `FuturesOvernightTracker`, `OvernightGapForecastProvider`) behind the existing `ForecastProvider` interface. Use it to widen or narrow the P10/P90 bands at 9:35 AM, not to shift P50 direction. Validate on Databento CME Globex data, which starts in June 2010, with as-of joins and pinball-loss evaluation before any gating is considered.
- **Part 2:** Index arbitrage keeps futures within a fair-value band set by financing (now SOFR-referenced, also traded directly through CME AIR Total Return futures), expected dividends, and costs. Hasbrouck (2003) finds E-minis lead price discovery. Expiry and rebalance flows concentrate in the opening and closing auctions: index futures and options expire in the open auction on the third Friday, and Nasdaq estimates rebalance-day close volumes are typically six times larger than normal, with about 40% of market-on-close flow coming from index funds.
- **Part 3:** Dividend futures (CME SDA, Eurex FEXD) hedge the realized amount of ordinary dividends with cleared, CCP-margined exposure. TRS hedges the full total return including timing and withholding, but carries counterparty risk and SIMM margin. Single-stock options are a poor, indirect dividend hedge with early-exercise risk. Special dividends are excluded from S&P dividend index settlement, which creates a structural hedge gap. For the DCA project, only ex-date awareness is actionable.

## Key Findings

| # | Finding | Evidence grade | Confidence |
|---|---|---|---|
| 1 | E-mini futures dominate price discovery for the S&P 500 and Nasdaq-100 relative to ETFs and floor futures | Peer-reviewed (Hasbrouck 2003, Journal of Finance 58(6)) | High |
| 2 | Raw "futures minus prior close" misstates the implied open by the fair-value premium (carry minus dividends) | CME published formula | High |
| 3 | US equity returns have historically accrued disproportionately overnight; the futures overnight drift is concentrated around the European open (2:00 to 3:00 AM ET, 1998 to 2019, about 3.6% annualized) | Peer-reviewed (Boyarchenko, Larsen, Whelan, RFS 2023; NY Fed SR 917) | High for existence, Medium for persistence |
| 4 | Gap-fill rates depend heavily on definition and gap size; small gaps usually fill same day, gaps over 1% mostly do not | Practitioner backtests only, mutually inconsistent | Low to Medium |
| 5 | First half-hour return (prior close to 10:00 ET) predicts the last half-hour return in SPY, 1993 to 2013 | Peer-reviewed (Gao, Han, Li, Zhou, JFE 2018) | Medium; small R-squared, does not directly help a 9:35 AM buy |
| 6 | S&P 500 annual dividend futures settle on ordinary gross dividends only; specials are excluded by index rules | CME Rulebook Ch. 365; S&P DJI methodology | High |
| 7 | 2020 showed dividend futures absorb large realized cuts: futures implied 32% of SX5E dividends cut, bank dividend futures fell 93% | Practitioner (Citi Private Bank) plus ECB/ESRB primary documents | Medium-High |

---

## Part 1: Overnight Stock Futures Tracker and Forecast Integration

### 1.1 Contracts and sessions

**Facts**

| Item | ES (E-mini S&P 500) | NQ (E-mini Nasdaq-100) | YM (E-mini Dow, $5) | Micros |
|---|---|---|---|---|
| Underlying | S&P 500 | Nasdaq-100 (not the Nasdaq Composite) | Dow Jones Industrial Average | MES, MNQ, MYM on the same indexes |
| Multiplier / tick | $50 x index; 0.25 pt = $12.50 | (verify on CME spec page) | (verify on CME spec page) | One-tenth of the E-mini size |
| Venue | CME Globex | CME Globex | CBOT on CME Globex | CME / CBOT |
| Weekly session | Sunday 6:00 PM ET to Friday 5:00 PM ET | Same | Same | Same |
| Daily maintenance | 5:00 to 6:00 PM ET (4:00 to 5:00 PM CT) | Same | Same | Same |
| Intraday halt | 4:15 to 4:30 PM ET | Same | Same | Same |
| Expiry cycle | March, June, September, December (H, M, U, Z) | Same | Same | Same |
| Last trade | 9:30 AM ET, third Friday of contract month; settles to a special opening quotation | Same | Same | Same |

- Futures keep trading for about one hour after the 4:00 PM ET cash close. CME's daily settlement is based on trading around the cash close, not on the 5:00 PM ET end of the Globex session.
- **Roll conventions conflict across sources.** ExpectancyIQ says open interest moves to the next quarter on the second Thursday, eight days before expiry, and CrossTrade gives the same Thursday. The Upcomers help center instead says CME's official equity-index roll date is the Monday before the third Friday, with volume usually moving first. A backtest should roll by observed volume or open-interest crossover, not by a fixed calendar rule. Log the rule used per roll.

**Continuous-contract pitfalls (inference, high confidence)**

- Back-adjusted series (difference or ratio) rewrite all past prices at each roll. Any feature computed on a back-adjusted series and stored as a level is not point-in-time. Store raw per-contract prices and compute overnight returns within a single contract (same `instrument_id` at both endpoints).
- On roll days, the front-to-next spread reflects carry and dividends to the next expiry (a fair-value difference), not market direction. Never splice contracts inside an overnight window.
- After the 9:30 AM third-Friday expiry, the expiring contract has no overnight session to measure. Use the next contract for that night.

### 1.2 Fair value versus raw futures change

**Facts**

- CME formula: Fair Value = Cash x [1 + r x (x/360)] - Dividends, where x is days to expiration and dividends are expected index-point dividends to expiry. CME's worked example: cash 1146.00, r 5.7%, 78 days, dividends 3.47 points, giving fair value 1156.68.
- Implied open = Prior day close + (Futures price - Fair value), per StockCharts' explanation of pre-market media quotes.

**Why naive "futures minus prior index close" is wrong (inference, high confidence)**

- Futures trade at a premium (or discount) equal to carry minus dividends. With short rates near 4 to 5% (assumption) and the S&P 500 dividend yield at 1.07% at the November 9, 2026 close per Multpl (an estimate based on S&P data), the premium is positive and largest just after a roll. At about 90 days it can amount to tens of index points.
- The premium decays toward zero at expiry and steps up at each roll. A naive change therefore carries a sawtooth bias correlated with the roll calendar, which a model will happily learn as false signal.
- Dividend points are lumpy around ex-date clusters, so a flat dividend-yield assumption misstates fair value in specific weeks. The error is larger for YM: a price-weighted index of 30 stocks, where one member's ex-date moves fair value noticeably.

**Recommended definitions (recommendation)**

| Quantity | Definition | Rationale |
|---|---|---|
| Overnight futures move (primary) | ln(F at 9:29:00 ET / F at 16:00:00 ET), same contract, minus the change in fair value over the window (near zero for a single night except on ex-dates) | Aligns with the cash-close anchor; excludes the 4:00 to 4:15 PM post-close drift from "overnight" |
| Post-close move (secondary feature) | 16:00 to 16:15 ET futures change | Captures after-close earnings reactions of mega-caps separately |
| Implied open gap | Prior index close x exp(overnight futures move adjusted for fair value), divided by prior close, minus 1 | Comparable to the realized gap |
| Realized open gap | Index official open / prior official close - 1; ETF proxy SPY, QQQ, DIA (open auction price / prior close) | ETFs give a tradable opening print; note that index "open" values can be stale in the first minutes because not all constituents have opened |
| Overnight false reversal (type A) | sign(futures move from 16:00 to 03:00 ET) is not equal to sign(futures move from 16:00 to 09:29 ET), conditioned on abs(first leg) > threshold | Captures overnight direction reversed before the open |
| Open-gap false reversal (type B) | sign(realized gap) is not equal to sign(close / prior close - 1), conditioned on abs(gap) > threshold | Captures gap reversed by the close |
| Gap fill by slot | Price touches the prior close by 9:35, 10:30, 12:30, 13:30 ET | Matches the project's buy slots directly |

Use 9:29 ET rather than 9:30 to avoid using any print at or after the cash open.

### 1.3 Empirical predictive value

**Facts (peer-reviewed or central bank)**

| Study | Sample | Finding | Relevance |
|---|---|---|---|
| Boyarchenko, Larsen, Whelan, "The Overnight Drift," NY Fed Staff Report 917 (2020, rev. 2022); Review of Financial Studies 36(9), 3502-3547 (2023) | S&P 500 futures, 1998 to 2019 | Largest positive returns accrue 2:00 to 3:00 AM ET (the European open), about 3.6% annualized. The drift is stronger after intraday sell-offs. The timing shifts with US/Europe DST asynchrony, supporting an inventory/liquidity explanation | Overnight returns have structure, but it is a small unconditional drift, not a gap predictor |
| Lou, Polk, Skouras, "A Tug of War," JFE 134(1), 192-213 (2019) | US cross-section | All momentum profits accrue overnight; other anomaly profits accrue intraday, with partly offsetting overnight premia; driven by investor clienteles | Overnight and intraday components behave differently and persist; relevant to single-stock beta mapping, not index gap timing |
| Kelly and Clark, Journal of Asset Management 12(2), 132-145 (2011) | US index ETFs, 1999 to 2006 | Returns are higher in non-trading hours than in trading hours | Supports the "overnight premium" literature; implies buying at the open has historically paid a premium relative to the close |
| Cliff, Cooper, Gulen, "Return Differences between Trading and Non-Trading Hours: Like Night and Day," SSRN working paper (rev. September 26, 2008) | US equities, roughly the prior decade | The US equity premium over that decade came from overnight returns, driven in part by high opening prices that then declined in the first hour of trading | Same; also directly relevant to buying near the open |
| Gao, Han, Li, Zhou, "Market Intraday Momentum," JFE 129(2), 394-414 (2018) | SPY, 1993 to 2013 | First half-hour return (prior close to 10:00 ET) predicts the last half-hour return; scaled slope 6.94; R-squared about 1.6% | The first half-hour embeds the overnight gap; the predicted window (15:30 to 16:00) is after all project buy slots |
| Hasbrouck, Journal of Finance 58(6), 2375-2400 (2003) | S&P 500 and Nasdaq-100 | Most price discovery occurs in E-mini futures; ETFs and cash follow | Futures-led price discovery is the mechanism linking overnight futures to the open |

**Practitioner gap-fill evidence (low quality, conflicting)**

| Source | Definition / sample | Reported figures |
|---|---|---|
| thetrading.tools | SPY since 1993, same-day fill | 0.1 to 0.25% gaps fill same day about 78 to 80%; gaps over 1% fill same day about 26 to 33%; about half of 2%+ gaps still open after five sessions |
| SwingTradeBot | SPY "gaps" (its own gap definition, likely gaps beyond the prior range) | 19.9% filled within 1 session; 54.1% within 5; 68.9% within 10; 77.1% within 20 |
| StatOasis | SPY daily, 8,397 gaps, 1993 to 2026, eventual fill | Down-gaps fill 98%, up-gaps 94%, median same day; direction close to noise, size carries the signal |
| Trade That Swing | SPY, rolling 6-month windows | 59% of gap-ups and 69% of gap-downs filled in one window; 50% and 46% in another |

**Inferences**

- The practitioner numbers disagree because they define "gap" and "fill" differently (any open different from prior close vs. a gap outside the prior day's range; same day vs. eventual). The only robust qualitative pattern is that **fill probability falls sharply with gap size.** That pattern is partly arithmetic: small gaps sit inside normal intraday range.
- None of the practitioner studies report out-of-sample tests, transaction costs, or multiple-testing corrections. Day-of-week splits come from samples of a few dozen events and should be treated as noise.
- **Robust:** futures lead the cash open; overnight and intraday return components differ structurally; gap size scales with realized and implied volatility. **Fragile:** directional gap-fade or gap-continuation rules; day-of-week effects; "false reversal" rates. **Not found:** any peer-reviewed measurement of "false reversal" rates for ES/NQ/YM as defined here. The project should measure them itself and report them descriptively.
- Directional value at 9:35 AM is likely minimal for index-level information: by 9:30 the open auction has incorporated the futures move (Hasbrouck). What remains is information about **dispersion**: a large gap signals a higher-volatility session, so the 9:35 price sits in a wider distribution relative to the prior close and the prior low.

**ES vs NQ vs YM divergence (inference, medium-low confidence)**

- NQ carries more mega-cap tech concentration and higher beta, so after-close earnings of a few Nasdaq-100 heavyweights drive NQ-specific overnight moves. YM is price-weighted, so high-priced members dominate its moves.
- Cross-index divergence (for example NQ - ES overnight spread) is a reasonable **sector-tilt feature** for mapping to single stocks, but no peer-reviewed evidence was found that it predicts reversals. Treat it as a hypothesis to test, not a signal.

### 1.4 Catalyst drivers and point-in-time sources

| Catalyst class | Typical timing (ET) | Measurable proxy | Point-in-time source (free or low cost) |
|---|---|---|---|
| US macro: CPI, NFP, PCE, retail sales, claims | 8:30 AM | Release flag plus surprise (actual minus consensus where available) | BLS/BEA release calendars (scheduled times); FRED/ALFRED vintages for values as first published; consensus requires a vendor |
| FOMC decision / minutes | 2:00 PM (decision) | Prior-day flag; next-morning overnight move | Federal Reserve FOMC calendar |
| Mega-cap earnings after close / before open | ~4:00 to 4:30 PM, or ~7:00 AM | Index-weight-weighted earnings flag | SEC EDGAR 8-K (Item 2.02) acceptance timestamps; company IR calendars |
| Asian session | ~7:00 PM to 2:00 AM | Nikkei / Hang Seng futures returns | Exchange data; vendor feeds |
| European open | ~2:00 to 3:00 AM (shifts with DST) | ES return in that hour; Euro Stoxx 50 futures | Eurex data via vendor |
| Foreign central banks (ECB, BoJ, BoE) | Varies | Decision-day flag | Central bank calendars |
| Geopolitical / unscheduled | Any | Overnight absolute move beyond a z-score threshold with no scheduled flag | Derived ("unexplained" residual) |
| Liquidity / positioning | Roll weeks, expiry, rebalance, month-end | Calendar flags | CME expiry calendar; S&P DJI rebalance announcements |

Lookahead risk: EDGAR acceptance time is a usable point-in-time stamp. Consensus figures and revised macro values are not: use ALFRED first-release vintages, never current FRED values, for any backtest.

### 1.5 Data sources

| Source | Coverage | Cost (as found) | Timestamp quality | Verdict |
|---|---|---|---|---|
| Databento (CME Globex MDP 3.0, GLBX.MDP3) | All CME/CBOT futures since June 6, 2010; trades, MBP-1, OHLCV-1s/1m | Historical usage-based from $0.50/GB; $125 free credit; CME live plans separate | Exchange-sourced; nanosecond timestamps (finer after Nov 2015 per secondary source) | **Recommended primary** |
| CME DataMine | Official CME historical data | Paid; pricing not verified in this research | Official | Fallback / audit source |
| FirstRate Data, Kibot, Norgate, Barchart | Continuous and per-contract bars | Low-cost one-time or subscription; not verified | Vendor-dependent; check back-adjustment and session definitions | Secondary; verify roll method |
| Polygon/Massive | Strong on US equities/ETFs (SPY/QQQ/DIA opens); futures coverage not verified | Subscription | Good for equities | Use for ETF open prints |
| Stooq | Free daily data | Free | Daily only; no session boundaries | Not suitable for overnight windows |
| Robinhood MCP `get_index_quotes` / `get_index_historicals` | Index values; coverage of futures **not verified** | Included | Unknown | ADR open question; assume index-only until tested |
| Yahoo Finance | Excluded by project policy | | | Do not use |

### 1.6 Model integration design

**Feature timestamping rules (recommendation)**

1. Every row carries `event_ts_utc` (when the market event occurred) and `available_ts_utc` (when the project could have known it). Joins use `available_ts_utc <= decision_ts`.
2. Decision timestamp for the 9:35 slot is 09:35:00 ET, converted to UTC per date. CME publishes in CT and NYSE in ET; both observe US DST together, but Europe does not on certain weeks (Boyarchenko et al. use exactly this asynchrony). Store all times in UTC and derive ET with a tz database, never with a fixed offset.
3. Holidays and half-days: use an exchange calendar for both CME and NYSE. On NYSE early-close days (1:00 PM ET), the "cash close" anchor moves; on CME holiday schedules, Globex may trade while NYSE is closed. Skip or flag nights whose anchor is not a full session.
4. Roll days: compute the overnight move within one contract; flag `is_roll_window` and `days_to_expiry`.
5. Monday gaps span a weekend: include `hours_in_window` as a feature, or model weekends separately.

**Mapping index moves to single stocks**

- Expected stock gap = beta_overnight x index overnight move (fair-value adjusted), with beta estimated **from overnight returns only** on a rolling window that ends before the decision date. Lou, Polk, Skouras show overnight and intraday components behave differently, so close-to-close beta is the wrong input.
- Use NQ for Nasdaq-100 constituents and ES otherwise; optionally regress on both.
- **Survivorship bias:** estimate betas only for stocks that were index members or in the project universe as of each historical date. Using today's constituents inflates historical fit.

**How it should feed Wondrous Octopus and ml-forecaster**

- Recommended: the new provider emits a separate `OvernightGapForecast` (P10/P50/P90 of the 9:35 price relative to prior close, plus a gap-volatility scalar). Downstream combination (if any) happens only in the existing reader layer.
- Do not inject the features into Wondrous Octopus internals in phase 1. Compare a candidate model with features against the frozen baseline offline first.

**Evaluation (recommendation)**

| Metric | Baseline | Pass threshold for ADR "proceed" |
|---|---|---|
| Pinball loss on P10/P50/P90 of 9:35 price vs prior close | Wondrous Octopus without overnight features | Statistically significant improvement (Diebold-Mariano or block bootstrap) out-of-sample |
| Quantile calibration (coverage of P10-P90 band) | Nominal 80% | Within 77 to 83% out-of-sample, also by volatility regime |
| MAE of open gap | Naive "zero gap" and raw-futures gap | Beats fair-value-adjusted implied open (sanity: implied open should be near-perfect) |
| Gap-direction hit rate after 9:35 to 10:30 | 50% and the unconditional up-day rate | Reported, not used for gating unless above about 55% out-of-sample |
| False-reversal rates (types A, B) | Descriptive | Reported with confidence intervals |

Sample sizes (inference): about 250 sessions per year gives roughly 4,000 sessions from mid-2010 to late 2026. Gaps above 1% are perhaps 10 to 20% of sessions, so a few hundred events. Detecting a 5-percentage-point hit-rate edge at 55% vs 50% needs roughly 1,000 events at conventional power. **Conditional gap-size buckets will be underpowered**; pre-register them and use walk-forward splits (train 2010 to 2018, validate 2019 to 2021, test 2022 onward) so 2020 does not dominate.

### 1.7 Architecture fit

**Constraints treated as fixed:** suggest-only; no ML component controls live DCA sizing; boundary interfaces are only `SizingSuggester`, `ForecastProvider`, `SuggestionPublisher`; components are independent and only `SizingAdjustmentReader` knows multiple suggesters exist; fail-closed everywhere; lookahead bias is a design constraint; DuckDB store.

**Proposed new objects (all new, none modifying existing code)**

| Object | Role | Interface | Failure behavior |
|---|---|---|---|
| `FuturesDataAdapter` | Loads per-contract ES/NQ/YM bars from the chosen vendor | Internal | On error, writes nothing; downstream sees missing data |
| `FuturesSessionStore` | Writes sessions, fair value inputs, rolls to DuckDB | Internal | Idempotent upserts keyed by (symbol, contract, session_date) |
| `FairValueCalculator` | Computes fair value from rate (SOFR or T-bill), dividend points, days to expiry | Internal, pure function | Returns null if inputs missing; tracker then marks row `fv_quality = 'missing'` |
| `FuturesOvernightTracker` | Computes overnight moves, implied/realized gaps, reversals, gap-fill by slot | Internal | Produces descriptive tables only |
| `CatalystCalendar` | Point-in-time catalyst flags | Internal | Missing flags default to "unknown," never to "no catalyst" |
| `OvernightGapForecastProvider` | Emits quantile forecasts for 9:35 (and 10:30) prices | Implements existing `ForecastProvider` | Returns "no forecast" on any upstream gap; never raises past the boundary |

**Existing objects that might need modification**

- Probably none. The one possible exception: if `ForecastProvider` has no field for a forecast horizon (for example "9:35 today" vs "next close"), a backward-compatible optional field would be needed. Treat this as an ADR question and prefer a separate provider registration over changing the interface.
- `SizingAdjustmentReader` is the only place that would learn this provider exists. Keep its default weight at zero (observe-only) until evaluation passes.

**DuckDB table sketch**

```sql
CREATE TABLE futures_contract (
  contract_id      VARCHAR PRIMARY KEY,  -- e.g. 'ESZ26'
  root             VARCHAR,              -- ES, NQ, YM, MES, MNQ, MYM
  underlying_index VARCHAR,              -- SPX, NDX, DJIA
  expiry_ts_utc    TIMESTAMP,
  multiplier       DOUBLE
);

CREATE TABLE futures_bar_1m (
  contract_id      VARCHAR,
  bar_start_utc    TIMESTAMP,
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
  source           VARCHAR,
  ingested_ts_utc  TIMESTAMP,
  PRIMARY KEY (contract_id, bar_start_utc)
);

CREATE TABLE futures_overnight_session (
  root               VARCHAR,
  session_date       DATE,           -- NYSE date of the following open
  contract_id        VARCHAR,        -- single contract for whole window
  anchor_close_utc   TIMESTAMP,      -- 16:00 ET prior day
  pre_open_utc       TIMESTAMP,      -- 09:29 ET
  f_anchor DOUBLE, f_pre_open DOUBLE, f_eu_open DOUBLE,
  fair_value_anchor DOUBLE, fair_value_pre_open DOUBLE,
  rate_used DOUBLE, div_points_to_expiry DOUBLE, days_to_expiry INTEGER,
  overnight_ret_fv_adj DOUBLE,
  hours_in_window DOUBLE,
  is_roll_window BOOLEAN, is_half_day BOOLEAN, is_expiry_day BOOLEAN,
  fv_quality VARCHAR,
  available_ts_utc TIMESTAMP,
  PRIMARY KEY (root, session_date)
);

CREATE TABLE open_gap (
  instrument       VARCHAR,          -- SPX, NDX, DJIA, SPY, QQQ, DIA, or ticker
  session_date     DATE,
  prior_close DOUBLE, official_open DOUBLE,
  implied_gap DOUBLE, realized_gap DOUBLE,
  px_0935 DOUBLE, px_1030 DOUBLE, px_1230 DOUBLE, px_1330 DOUBLE, close DOUBLE,
  filled_by_0935 BOOLEAN, filled_by_1030 BOOLEAN, filled_by_close BOOLEAN,
  available_ts_utc TIMESTAMP,
  PRIMARY KEY (instrument, session_date)
);

CREATE TABLE reversal_event (
  root VARCHAR, session_date DATE,
  reversal_type VARCHAR,             -- 'A_overnight', 'B_gap_close'
  threshold DOUBLE, first_leg_ret DOUBLE, full_ret DOUBLE, reversed BOOLEAN,
  PRIMARY KEY (root, session_date, reversal_type, threshold)
);

CREATE TABLE catalyst_event (
  catalyst_id VARCHAR PRIMARY KEY,
  catalyst_type VARCHAR,             -- CPI, NFP, FOMC, EARNINGS, ECB, ...
  entity VARCHAR,                    -- ticker or agency
  scheduled_ts_utc TIMESTAMP,
  actual_ts_utc TIMESTAMP,           -- e.g. EDGAR acceptance time
  value_first_release DOUBLE, consensus DOUBLE,
  source VARCHAR, available_ts_utc TIMESTAMP
);

CREATE TABLE overnight_gap_forecast (
  instrument VARCHAR, session_date DATE, horizon VARCHAR,  -- '0935', '1030'
  p10 DOUBLE, p50 DOUBLE, p90 DOUBLE, gap_vol DOUBLE,
  model_version VARCHAR, inputs_hash VARCHAR,
  generated_ts_utc TIMESTAMP,
  PRIMARY KEY (instrument, session_date, horizon, model_version)
);
```

**Relevance to the 9:35 AM slot and the existing ladder (inference)**

- At 9:35, the ladder compares current price with last close and previous low. A gap is already embedded in "current vs last close." The new information is **whether that gap is large relative to the night's catalyst and volatility context**. A 1% gap-down on CPI morning differs in distribution from an unexplained 1% gap-down.
- The open auction and first minutes are the noisiest prices of the day, and the index "open" can be stale. A 9:35 fill sits inside this window. The forecaster's natural job is to say "today's 9:35 distribution is wide," not "buy now."
- Gap-fill by 10:30: practitioner sources claim most same-day fills complete by midday, but this is unverified. The project should measure `filled_by_1030` directly. Even if large gap-downs partly fill by 10:30, a buy-only DCA that buys at 9:35 rather than 10:30 trades a small expected price difference for timing risk. The evidence does not justify moving slots.
- Kelly and Clark plus the overnight-drift literature imply that, historically, open prices have been on average not cheaper than prior closes over long samples. There is no robust evidence that systematically delaying buys after gaps improves long-run DCA cost basis.

**Open ADR questions**

1. Does `ForecastProvider` support intraday horizons, or does this require a separate provider type?
2. Which vendor is canonical (Databento recommended), and what is the licensing posture for storing raw futures bars locally?
3. Does the Robinhood MCP return futures quotes or only index levels? Test before designing around it.
4. Which rate goes into fair value: SOFR term, 3-month T-bill, or implied carry backed out from the futures-cash basis at the close?
5. Source for expected dividend points (CME SDA/SDI prices, vendor estimates, or trailing-yield approximation)?
6. Is the forecast observe-only for a fixed period (recommend at least 6 months live shadow) before `SizingAdjustmentReader` can weight it?
7. Is the universe beta map computed on point-in-time constituents?
8. How should weekends and holidays be handled: separate model or a duration feature?

---

## Part 2: Program Trading and Index Arbitrage Mechanics

### 2.1 Basket execution

**Facts**

- NYSE has historically defined program trading as index arbitrage or a coordinated trade in a basket of 15 or more stocks; the older definition also required a total value of at least $1 million. A 2007 NYSE proposal removed the $1 million floor and kept index arbitrage in the definition.
- Index arbitrage buys the cheap side and sells the rich side of futures vs the cash basket when the basis leaves its fair-value band (Econlib; NYSE definitions).
- Index futures and index options expire in the **open** auction on the third Friday; single-stock options expire against the **close** auction (Nasdaq).

**Inferences and practitioner knowledge (medium confidence)**

| Approach | How it works | Trade-off |
|---|---|---|
| Full replication basket | Trade all constituents in index weights via program algorithms (VWAP, arrival-price, close-targeting) | Minimal tracking error; highest cost on illiquid tail names |
| Optimized subset | Trade a subset chosen to minimize tracking error per unit cost | Lower cost; residual tracking error becomes basis risk |
| ETF as basket proxy | Trade SPY/QQQ against futures; use creation/redemption via authorized participants to close ETF vs NAV gaps | Fast and cheap; creation/redemption settlement is now T+1 |
| MOC / MOO auctions | Execute at official open or close prints to match index valuation points | Exposed to auction imbalance; prices can move on imbalance publications |
| Basis trade / EFP / BTIC | Exchange futures for physical, or Basis Trade at Index Close, to lock the basis without leg risk | Requires counterparties and negotiated basis |

### 2.2 Execution lag risks

| Risk | Mechanism | Typical mitigation |
|---|---|---|
| Leg risk | Futures fill instantly while a 500-name basket takes seconds to minutes | Execute futures last, use BTIC/EFP, or trade the ETF |
| Latency | Futures lead cash (Hasbrouck), so arbitrageurs on slower links trade stale prices | Co-location; wider trigger bands |
| Partial fills | Some names do not fill; residual becomes an unhedged stock bet | Completion algorithms; accepting tracking error |
| Tracking error | Optimized baskets diverge from the index | Size bands to tracking error |
| Auction imbalance | Large MOC/MOO imbalances move official prints | Offset orders; imbalance monitoring |
| Expiry effects | Settlement against the special opening quotation; triple witching concentrates flows | Rolling early; avoiding settlement-print exposure |
| Rebalance days | Index funds trade at the close of the effective date | Pre-positioning, crossing networks |

**Facts:** Nasdaq estimated that total daily share volume is typically 44% higher on witching days. On index rebalance days, it estimated about 40% of market-on-close flow was likely index-fund driven and close volumes were typically six times larger.

### 2.3 Setting basis and fair-value bands

- Center: CME fair value = Cash x [1 + r x (days/360)] - Dividends.
- Band width (inference): round-trip commissions and fees, market impact of the basket, financing spread above the reference rate (balance sheet cost), dividend forecast uncertainty, and tax/withholding differences. Different desks have different bands because funding costs differ; the futures price drifts within the union of those bands.
- **Financing after LIBOR (fact):** CME's AIR S&P 500 Total Return futures on SOFR (launch announced for August 26, 2024) price as (Equity Index - Accrued Financing) + Financing Spread Adjustment, with financing accrued daily at SOFR. CME describes the TRF spread as equivalent to the spread charged above or below the benchmark rate in an equity index swap. The AIR suite reached a record open interest of more than 1 million contracts (about $365 billion notional) on September 11, 2025, with 2025 average daily volume of 25,000 contracts, up 80% year-over-year (CME press release, September 16, 2025). Figures cover the EFFR and SOFR variants together.
- Inference: the AIR spread makes the implied repo/financing component of the basis directly observable and tradable, which narrows the uncertainty in band setting and lets desks hedge financing separately from dividends.

### 2.4 Hedging dividends within index arbitrage

- A long-basket / short-futures arbitrage receives dividends on stocks and is short them in the futures price (fair value subtracts expected dividends). Any shortfall in realized vs expected dividends is a loss.
- Risks: forecast error in amount, timing (ex-date shifting across a futures expiry), special dividends (not in futures fair value models that assume ordinary dividends), withholding tax (for non-US holders), and cuts in stress.
- Hedges: dividend futures (SDA, SDI, FEXD), dividend swaps, or TRS (see Part 3).

### 2.5 Post-2008 and recent market-structure changes

| Change | Date | Effect on index arbitrage (inference unless stated) |
|---|---|---|
| Eurex lists EURO STOXX 50 Index Dividend Futures | 2008 | Moved dividend risk from OTC swaps onto CCPs after the 2008 counterparty shock |
| Uncleared margin rules and ISDA SIMM | Phased from 2016 | Raised the cost of uncleared TRS and dividend swaps relative to futures |
| LIBOR to SOFR transition | 2021 to 2023 | Financing legs reference SOFR; AIR SOFR TRF launched 2024 |
| T+1 settlement in US | May 28, 2024 (SEC compliance date) | Shorter window for basket settlement, securities lending recalls, and ETF creation/redemption; cross-border baskets face mismatched settlement cycles |
| CME SPAN 2 for equity products | Go-live October 18, 2024; migration completed January 2025 | Margin for equity index futures now uses a VaR-style framework (at least 5-year lookback, 99.5% confidence) |

### 2.6 Link back to Part 1

- **Futures-led price discovery (fact, Hasbrouck 2003):** overnight information is impounded in ES/NQ first. At 9:30 the open auction aligns cash to futures; arbitrage flows enforce it. This is why implied open is a near-sufficient statistic for the gap and why the gap itself carries little remaining directional edge.
- **Open auction effects (inference):** on third-Friday expiry mornings, index derivatives settle on the opening print, so open-auction imbalances can create transient dislocations exactly in the 9:30 to 9:35 window.
- **Close effects (inference):** rebalance and MOC flows concentrate at 4:00 PM, which shapes the "prior close" anchor of the next gap. A rebalance-day close distorted by index flows can produce a next-morning gap that partly reflects reversal of that distortion.
- **Overnight drift mechanism (fact, Boyarchenko et al.):** the inventory/liquidity explanation links closing order imbalance to overnight returns, which ties Part 2 flows to Part 1 behavior.

---

## Part 3: Dividend Risk Hedging in Index Arbitrage

### 3.1 Instruments

**Facts**

| Feature | CME S&P 500 Annual Dividend Index futures (SDA) | Eurex EURO STOXX 50 Index Dividend Futures (FEXD) |
|---|---|---|
| Contract size | $250 x S&P 500 Annual Dividend Index | EUR 100 per index point |
| Tick | 0.05 points = $12.50 | 0.1 points = EUR 10 |
| Tenors | Nearest 11 Decembers | Annual December cycle, up to about 10 years |
| Settlement | Index value on the third Friday of December; accumulation of all ordinary gross dividends on constituents that went ex since the previous reset | Cumulative total of relevant gross dividends; final settlement at 12:00 CET |
| Last trade | 9:30 AM ET, third Friday of December | Final settlement day |
| Related | Quarterly SDI ($1,000 x index); Nasdaq-100 (NDA) and Russell 2000 (RDA) annual; options on SDA | Options (OEXD); sector and Select Dividend 30 dividend futures; single-stock dividend futures on more than 80 Eurozone and pan-European companies |
| Margin | CME SPAN 2 applies to CME-cleared equity products from October 2024; SDA not named explicitly in advisories found but is not in any excluded category (inference) | Eurex Prisma (not researched in depth here) |

**S&P DJI dividend classification (fact)**

- Special dividends are outside the company's normal payment pattern. In index calculations they trigger a price adjustment, not inclusion as an ordinary dividend.
- S&P DJI generally treats the third consecutive instance of a non-ordinary dividend (judged by ex-date timing, not amount) as ordinary for index purposes. A return of capital is treated as a special dividend.
- Why it matters (inference, high confidence): SDA settles on **ordinary** gross dividends only. An arbitrageur long stocks receives specials, while a futures fair-value model may or may not include them. A special dividend therefore creates an unhedgeable gap between the stock-side cash flow and the dividend-future payout, and reclassification after the third consecutive instance changes whether future payments are hedgeable by SDA.

### 3.2 Comparison by instrument

| Dimension | Total return swap (TRS) / dividend swap | Index dividend futures (SDA, FEXD) | Single-stock dividend futures (Eurex) | Single-stock options |
|---|---|---|---|---|
| Risk hedged | Full total return: amount, timing within the swap term, and (by contract) withholding/tax rate | Realized amount of ordinary gross dividends in a calendar-year window | Realized amount for one stock | Indirectly: dividends enter through put-call parity and forward price |
| Timing risk | Contract-specific; can match exact pay dates | Only whether ex-date falls inside the annual window | Same as index | Ex-date versus expiry matters |
| Tax / withholding | Can specify gross or net, a key reason TRS persists | Gross dividends; withholding differences become basis | Gross | Not addressed |
| Special dividends | Negotiable | Excluded under S&P rules (SDA); per index provider rules (FEXD) | Per Eurex contract rules (adjustments for non-regular distributions) | Contract adjustments by OCC for large specials |
| Basis risk | Low if referencing the same index | Index composition vs hedged basket; gross vs net | Low for that name | High; vega and rate noise dominate |
| Liquidity / term | OTC, customizable | Exchange-traded; FEXD deepest; SDA curve out to 11 Decembers | Thinner; concentrated in large names | Deep for large names but not a dividend instrument |
| Margin / capital | Uncleared: ISDA SIMM. Equity index delta goes in the "Indexes, Funds, ETFs" bucket (standard, preferred) or is allocated back to individual equities (alternative, if bilaterally agreed, chosen per portfolio), SIMM v2.8+2506 effective December 6, 2025 | CCP margin (CME SPAN 2; Eurex Prisma) | CCP margin | CCP margin (OCC) |
| Counterparty risk | Bilateral, mitigated by IM/VM | CCP | CCP | CCP |
| Early exercise | None | None | None | American calls may be exercised early before ex-date, removing the dividend leg from a hedge |

**Early-exercise rule (established theory, high confidence):** exercising an American call just before the ex-date is optimal only when the dividend exceeds the remaining time value of the call (roughly, the dividend exceeds the interest on the strike to expiry plus the value of the corresponding put). Deep in-the-money calls on high-dividend stocks are most at risk. A short-call hedger who is assigned early loses the expected dividend offset.

### 3.3 Comparison across payout cycles

| Payout profile | Example markets | Seasonality / clustering | Best hedge instrument (recommendation) | Main residual risk |
|---|---|---|---|---|
| Quarterly, smooth (US) | S&P 500, Nasdaq-100 | Ex-dates spread across the year with mild clustering in mid-quarter months | SDA for annual amount; SDI for quarter-level; TRS if withholding matters | Specials excluded from SDA; buyback-heavy payout means dividends are a small, steady share of total return |
| Semiannual / annual, concentrated (Europe) | EURO STOXX 50 | Strong concentration of ex-dates in roughly April to June (inference from market practice; not quantified in sources found) | FEXD (deepest dividend futures market) | Regulatory cuts (2020 ECB bank ban); scrip-dividend elections |
| Annual / semiannual (Asia) | Nikkei 225, Hang Seng, KOSPI | Japan concentrated at March and September fiscal year-ends (inference; not verified here) | Local dividend futures where listed; otherwise TRS | Liquidity; FX; withholding |
| Stress-period cuts | 2008, 2020 | Cuts concentrated in financials and energy | Dividend futures hedge realized amount; TRS hedges total return | Futures can gap sharply on announcements |

**Stress evidence (facts)**

- On March 27, 2020 the ECB recommended that significant euro area banks suspend dividends and buybacks until at least October 1, 2020, covering 2019 and 2020 profit distributions not already paid.
- ECB Working Paper No 2787 (Andreeva, Bochmann and Schneider, February 2023): the recommendation caused bank share prices to fall about 7% in the two weeks after the announcement, and banks that had planned to pay dividends fell about 9% relative to non-payers.
- Citi Private Bank (January 2021): by end-2020 European futures indicated 32% of SX5E dividends were cut, and bank dividend futures were down 93% in 2020.
- UK banks (Barclays, Santander UK, Lloyds, NatWest, Standard Chartered, HSBC) cancelled 2020 dividends after Bank of England PRA pressure.

**Inference:** in 2020, a long-stock / short-futures arbitrage book in Europe lost on dividends it expected but did not receive, unless it was hedged with dividend futures. Dividend futures performed their intended function but with extreme mark-to-market moves, so margin liquidity (not hedge effectiveness) was the binding constraint.

### 3.4 Recommendation for an institutional reader (one per decision)

- Hedging realized US ordinary dividends on a basket that tracks the S&P 500: **SDA.**
- Hedging European dividends: **FEXD**, because it is the deepest market and clears.
- When withholding or exact payment timing must be matched: **TRS**, accepting SIMM margin and counterparty risk.
- Single-stock options: **do not use as a dividend hedge.**

---

## Caveats

**Facts vs assumptions vs inferences**

- **Facts** are limited to items attributed to CME, Eurex, S&P DJI, ISDA, the SEC, ECB/ESRB, NY Fed, and peer-reviewed papers above.
- **Assumptions:** short rates (about 4 to 5%), European April to June dividend concentration, and Japanese fiscal-year clustering are stated from general market knowledge and were not verified against data in this research. The S&P 500 dividend yield used (1.07% at the November 9, 2026 close; 1.05% on October 31, 2026) is a Multpl estimate based on S&P data, not an official S&P DJI figure; it was last above 1.2% in March 2026 (1.21%).
- **Inferences:** every design recommendation, sample-size estimate, and statement about relevance at 9:35 AM.

**Known gaps and uncertainty**

- No peer-reviewed measurement of "false reversal" rates for ES/NQ/YM was found. All gap-fill statistics are practitioner backtests with inconsistent definitions, no out-of-sample testing, and probable data-mining.
- The overnight drift and overnight-return premium are documented through roughly 2019 to 2022 samples; persistence after publication is not established.
- The roll-date convention conflicts across sources (second Thursday vs Monday before expiry).
- SDA's inclusion in SPAN 2 is inferred from CME's equity-scope filing, not confirmed by name.
- Robinhood MCP futures coverage is unknown.
- NQ and YM contract multipliers are not quoted here; take them from the CME specification pages.

**Bias flags**

- **Lookahead:** back-adjusted continuous contracts, revised macro data, today's index constituents, and full-sample betas are the four main leakage paths.
- **Survivorship:** single-stock beta maps and gap-fill statistics built on current constituents overstate fit.
- **Regime concentration:** 2020 and 2022 contain a disproportionate share of large gaps; walk-forward evaluation must stratify by regime.

---

## Recommendations (one per decision)

| Decision | Recommendation |
|---|---|
| Purpose of the tracker | Volatility and gap-size context, not directional alpha |
| Overnight move definition | Fair-value-adjusted, single contract, 16:00 to 09:29 ET |
| Primary data vendor | Databento CME Globex MDP 3.0, 1-minute bars plus trades around anchors |
| ETF open prints | Existing equity vendor (not Yahoo) for SPY/QQQ/DIA |
| Integration point | New `OvernightGapForecastProvider` through the existing `ForecastProvider` interface; no existing object modified |
| Default weight | Zero (observe-only) in `SizingAdjustmentReader` for at least 6 months of live shadow |
| Primary metric | Out-of-sample pinball loss and P10 to P90 coverage of 9:35 price vs prior close |
| Buy-slot timing | Do not move the 9:35 slot based on gap-fill folklore; measure `filled_by_1030` first |
| Calendar flags | Add expiry (third Friday), roll window, S&P rebalance effective date, and ex-dividend date flags as descriptive features |
| Dividend instruments | No use in the retail system; background only |

## Relevance to a Retail Buy-Only DCA System

| Finding | Actionable for all-stock-dca? | How |
|---|---|---|
| Fair-value-adjusted implied gap and gap-size volatility at 9:35 | **Yes** | Context for quantile bands; observe-only first |
| Gap-fill by 10:30 | **Measure, then decide** | Descriptive table; no slot changes without evidence |
| Expiry (third Friday) and quad witching | **Yes, as flag** | Open auction settles index derivatives; expect noisier 9:35 prints |
| S&P rebalance days | **Yes, as flag** | Distorted closes affect next-day "last close" anchor |
| Ex-dividend dates of held stocks | **Yes** | Price drops by about the dividend at the open on ex-date; the ladder should not read that as a dip signal |
| Overnight drift / overnight premium | **Background** | Implies no systematic advantage to buying at the open; supports leaving the schedule alone |
| Index arbitrage band mechanics, AIR, SPAN 2, SIMM | **No** | Institutional background |
| TRS vs dividend futures vs options | **No** | Institutional background |
| Special-dividend classification | **Minor** | Large specials cause ex-date price drops; same flag as ex-dates |

## Sources

- Hasbrouck (2003), Journal of Finance: https://onlinelibrary.wiley.com/doi/abs/10.1046/j.1540-6261.2003.00609.x
- Boyarchenko, Larsen, Whelan, NY Fed Staff Report 917: https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr917.pdf
- Overnight drift (Fed in Print): https://fedinprint.org/item/fednls/92064
- Lou, Polk, Skouras (RePEc): https://ideas.repec.org/p/ehl/lserod/119010.html
- Gao, Han, Li, Zhou, Market Intraday Momentum: https://c.mql5.com/forextsd/forum/173/intraday_momentum_-_the_first_half-hour_return_predicts_the_last_half-hour_return.pdf
- Intraday momentum summary: https://quantum-bias.com/blog/first-last-half-hour-intraday-momentum
- Intraday price formation in US equity index markets: https://www.researchgate.net/publication/305397790_Intraday_price_formation_in_US_equity_index_markets
- Overnight returns (arXiv): https://arxiv.org/html/2507.04481v1
- Overnight returns thesis (DiVA portal; search the title on diva-portal.org, URL omitted because its numeric ID trips the repo secrets guard)
- CME fair value: https://www.cmegroup.com/trading/equity-index/fairvalue.html
- StockCharts on futures fair value: https://articles.stockcharts.com/article/articles-dancing-2018-11-infatuation-with-morning-futures-fair-value/
- StoneX, E-mini S&P 500: https://futures.stonex.com/blog/what-are-emini-sp-500-futures
- Futures market hours 2026: https://alpha-futures.com/posts/futures-market-hours-2026
- thetrading.tools gap analysis: https://www.thetrading.tools/gap-analysis
- SwingTradeBot SPY gaps: https://swingtradebot.com/equities/SPY/gaps
- StatOasis S&P 500 opening gaps: https://statoasis.com/overfit/research/sp500-opening-gaps
- Trade That Swing gap fill: https://tradethatswing.com/sp-500-spy-es-gap-fill-strategy-and-statistics/
- Databento futures: https://databento.com/futures
- Databento overview (NexusFi): https://nexusfi.com/a/data/databento-futures-market-data
- Program trading definition change (Lexology): https://www.lexology.com/library/detail.aspx?g=99d12b2b-ff3b-4f59-a947-cde87df5215b
- Econlib, Program Trading: https://www.econlib.org/library/Enc/ProgramTrading.html
- Nasdaq, triple witching: https://www.nasdaq.com/articles/the-powerful-impact-of-triple-witching-2021-06-10
- Quadruple witching: https://pabloespinal.net/blog/what-quadruple-witching-day-means-for-your-stocks/
- Index rebalancing: https://mtoday.net/markets-news/how-index-rebalancing-works.html
- CME AIR S&P 500 TR SOFR: https://www.cmegroup.com/markets/equities/sp/adjusted-interest-rate-sp-500-total-return-sofr.html
- CME AIR SOFR launch (2024): https://www.cmegroup.com/media-room/press-releases/2024/7/25/cme_group_to_launchadjustedinterestratesp500totalreturnsofrfutur.html
- CME AIR record OI (2025): https://www.cmegroup.com/media-room/press-releases/2025/9/16/cme_group_adjustedinterestratetotalreturnfuturesreachrecordopeni.html
- SEC T+1 investor bulletin: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/new-t1-settlement-cycle-what-investors-need-know-investor-bulletin
- CME SPAN 2 advisory (2025): https://www.cmegroup.com/content/dam/cmegroup/notices/clearing/2025/01/chadv25-034.pdf
- CME SPAN 2 rule filing (2024): https://www.cmegroup.com/content/dam/cmegroup/market-regulation/rule-filings/2024/10/24-425.pdf
- CME Rulebook Ch. 365 (SDA): https://www.cmegroup.com/rulebook/CME/IV/350/365/365.pdf
- CME S&P 500 Annual Dividend Index: https://www.cmegroup.com/markets/equities/sp/sp-500-annual-dividend-index.html
- CME options on SDA FAQ: https://www.cmegroup.com/articles/faqs/frequently-asked-questions-options-on-sp-500-annual-dividend-index-futures.html
- CME equity index dividend futures: https://www.cmegroup.com/markets/equities/us-index/equity-index-dividend-futures.html
- Eurex dividend derivatives: https://www.eurex.com/ex-en/markets/did
- Eurex FEXD (EURO STOXX 50 Index Dividend Futures product page, under https://www.eurex.com/ex-en/markets/did; direct URL omitted because its numeric ID trips the repo secrets guard)
- Eurex OEXD (EURO STOXX 50 Index Dividend Options product page, under https://www.eurex.com/ex-en/markets/did; direct URL omitted for the same reason)
- Dividend future (Wikipedia, background only): https://en.wikipedia.org/wiki/Dividend_future
- S&P DJI equity indices policies and practices: https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-equity-indices-policies-practices.pdf
- ISDA SIMM v2.8+2506: https://www.isda.org/a/f2RgE/ISDA-SIMM_v2.82506_PUBLIC.pdf
- ECB dividend recommendation (A&O Shearman): https://finreg.aoshearman.com/COVID-19-European-Central-Bank-Recommends-Suspens
- Citi Private Bank, European dividends: https://www.privatebank.citibank.com/newcpb-media/media/documents/insights/ES_Dividends_0121.pdf
- CNBC, UK banks scrap dividends: https://www.cnbc.com/2020/04/01/uk-banks-scrap-dividends-as-recession-fears-build-across-europe.html
