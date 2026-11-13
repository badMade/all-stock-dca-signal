# Prediction markets: methods for stock forecasts

Companion to `docs/adr-prediction-markets.md` (ingestion). This doc covers how stored probabilities become forecast inputs.
Status: reference. Source: deep-research report, 2026-11-12. Studies are cited as reported there; not re-verified.

## 1) Evidence gaps

| Gap | Implication |
|---|---|
| CEO-succession and executive-departure markets: no calibration study. New, multi-outcome, thin (Polymarket intl NVIDIA successor about $45k lifetime volume). | Use as scenario weights only. |
| Index ladders vs option-implied distributions: no peer-reviewed comparison. Ladders are near-replicable with options. | A persistent gap is more likely fees or microstructure than information. |
| Long-dated markets: collateral lock-up and longshot bias compress prices toward 50%. | "By 2028" odds are less reliable than near-dated ones. |
| Play money vs real money: older work found comparable accuracy. Metaculus and GJ have good long-horizon records but seldom cover single companies. | Use for macro and policy regimes. |
| Risk-neutral vs physical: prices embed risk premia, fees, collateral cost (Kalshi pays about 3.5% interest on positions) and longshot bias. | For crash-correlated events, Yes prices exceed real-world probabilities. Shrink before use. |

## 2) Event-study priors: CEO succession

| Study | Finding |
|---|---|
| Clayton, Hartzell & Rosenberg (2005), J. Business | 872 turnovers. Volatility rises after all turnovers (28.9% to 30.7%); forced 33.3% to 43.2%. Regression: +17-24% in year one, persists about 2 years. |
| Huson, Malatesta & Parrino (2004), JFE | 1,344 successions. Positive announcement returns on average; forced turnovers +1.3% OROA; larger gains for outsiders. |
| Borokhovich, Parrino & Trapani (1996) | Forced turnovers: outside successor positive returns, inside successor negative. |
| Denis & Denis (1995), JF; Warner, Watts & Wruck (1988), JFE | Average announcement effects small; positive effects concentrate in forced exits or when an outsider is named at once. |
| Johnson et al. (1985), JAE vs Jenter, Matveyev & Roth (ECGI WP 2023) | Conflict on founders: Johnson positive on founder deaths; Jenter -2.32% 3-day CAR on sudden deaths, more negative for founders. |

Use: the sign of the mean effect is ambiguous. The robust use is a volatility and dispersion adjustment. A forced founder exit with no designated successor is in the high-volatility bucket.

## 3) Methods

### 3a) Scenario mixture

E[P] = sum over i of p_i * V_i, where V_i is your valuation conditional on outcome i.

Market-implied spread: regress the stock's market- and sector-adjusted return on the change in event probability over windows where only the event news moved (dP ≈ (V1 - V0) * dp). Align venue trade timestamps to NYSE prices.

### 3b) Pooling across venues and sources

- Pool in log-odds: logit(p) = sum of w_j * logit(p_j).
- Weight by liquidity or spread tightness; down-weight thin books.
- Do not extremize cross-venue prediction-market prices; they are arbitraged and not independent. Shrink thin-market prices toward a base rate instead.

### 3c) "By date X" to a hazard rate

- Constant hazard: lambda = -ln(1 - q) / tau, where q = P(event by T) and tau = days to T.
- Probability over horizon h: 1 - exp(-lambda * h).
- Multi-outcome successor market: P(candidate i named within h) ≈ P(any named within h) * P(i | named). Assumes timing and identity are independent; outside hires are likely slower.

### 3d) Combine with other data

- Options: implied-vol term structure and skew around the next earnings date.
- Analyst estimates and revisions.
- SEC 8-K Item 5.02: authoritative resolution source for officer changes (point-in-time anchor on `filed_date`).
- News timestamps.

Rule: options set the return distribution. Prediction markets decide which mixture components exist and their weights. Then check whether implied vol already prices the turnover-literature volatility uplift.

### 3e) Avoid double-counting

Corporate-event markets lead the stock slightly, not by days. Use event probabilities for volatility, tail scenarios and sizing. Use them for expected return only with an independent view that the event market and the stock disagree.

## 4) Backtest pitfalls (lookahead)

- 24/7 trading: align to the 16:00 ET close using the last mid strictly before 16:00, not the UTC daily candle close.
- Markets created after the news (Polymarket's NVIDIA successor market opened 2026-11-09, after the scandal broke). Filter on `created_ts`.
- Rules get edited: store and hash rules text.
- Keep voided, delisted and duplicate markets.
- Thin-market spikes are not information (Kalshi briefly priced Tim Cook as next NVIDIA CEO at 91% before falling to about 10%).
- Use bid/ask mids, not last trade; record the spread.
- Never scrape narrative text as data (Polymarket's auto-summary claimed "Jay Puri leads at 83%" while its own price showed 9%).

## 5) Priority by evidence quality

1. Fed and CPI markets on Kalshi (strongest evidence).
2. Earnings-beat and fundamental-threshold markets on Polymarket and Kalshi (good working-paper evidence).
3. Index ladders and Cboe/Nasdaq binaries, mainly as a cross-check against options.
4. CEO, M&A and policy markets as scenario weights only.

## 6) Adjustments before use

- Mid-quotes, not last trade.
- Shrink sub-10c and above-90c prices for longshot bias.
- Down-weight markets under about $100k volume or with wide spreads (volume is inflated by wash trading; prefer liquidity and spread).
- Pool across venues in log-odds without extremizing.

## 7) Worked example: NVIDIA, 2026-11-12 (dated snapshot, not a recommendation)

### Facts (as reported)

- 8-K, event date 2026-11-11: board accepted Jensen Huang's resignation as President, CEO and director, effective immediately. CFO Colette Kress is interim CEO and stays CFO. Harvey C. Jones becomes first board chair. Internal and external candidates; no timetable.
- Q3 FY2027 earnings due 2026-11-17.
- Departure markets: Polymarket year-end exit went about 16% to 75% to 88% before the 8-K; Kalshi departure by Dec 1 about 80%. Both resolved Yes. They moved alongside WSJ and Reuters reporting, not ahead.
- NVDA fell as much as about 14% intraday on 2026-11-12 and closed down about 4%.

### Successor odds snapshot

| Candidate | Kalshi KXNVDACEO (to 2029-01-01; interim doesn't count; $304k vol) | Polymarket intl (to 2028-12-31; $45k vol) | Pool, equal weight | Pool, volume-weighted (≈0.87/0.13) |
|---|---|---|---|---|
| Colette Kress | 30% | 16.7% | ≈23% | ≈28% |
| Ian Buck | 28% | 32% | ≈30% | ≈29% |
| Rene Haas | 8% | 10% | ≈9% | ≈8% |
| Jay Puri | not shown | 9% | n/a | n/a |

Intraday Kalshi readings moved a lot (Buck 35% to 39c, Kress 29% to 20c). Polymarket priced a successor announcement by 2026-12-31 at about 45%. Polymarket outcomes sum to about 99c over 19 names with no "Other" bucket; check how an unlisted hire would resolve. Polymarket US (the user's link) not loaded; separate, thinner book.

### Method illustration

- Timing hazard: 45% within 49 days gives lambda ≈ 0.0122/day, about 31% within 30 days and 67% within 90 days. P(Buck within 90 days) ≈ 0.67 * normalized P(Buck | named).
- Scenario tree: (i) technologist insider (Buck and similar, about 30-35%); (ii) finance/operations insider (Kress, Puri, about 30-35%); (iii) outsider (Haas, Su, Srouji, others, remainder). Assign your own conditional value per branch. Outsider branch has widest dispersion; for forced exits the inside-successor branch has historically had negative announcement returns.
- Volatility prior: about 1.2x pre-scandal NVDA vol. Compare with Dec-Mar implied vol; if implied already exceeds it, event markets add no vol information.
- Update: when a successor is named, collapse the tree to one branch and expect a second repricing. Watch 8-K Item 5.02.
- Double-counting: the 88% exit probability was in the stock before the 8-K. No extra departure discount.

### Watchlist

Kalshi `KXNVDACEO`, `KXNVDACEOINT`; Polymarket successor-by-date and NVDA earnings markets; NVDA option implied vol; EDGAR 8-K Item 5.02.
