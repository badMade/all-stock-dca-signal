# Inventory reservation suggester (research only, shadow mode)

`dca-sizing-signals/inventory_signals/` holds `InventoryReservationSuggester`. It is a
suggest-only `SizingSuggester` that can only shrink a buy. When the money already put into a
ticker (its cost basis) is above that ticker's target share, it:

1. scales the tier size down with a multiplier in `[m_min, 1.0]`;
2. asks for a deeper discount below the earlier same-day reference price before a top-up
   (the `max_buy_price` output).

Both effects get weaker as less of the trading day's forecast variance is left. Nothing in
the live DCA paths imports it or reads its file. Expect lower risk, not higher returns.

## Where the idea comes from, and what is different here

The design adapts the market-maker reservation price of Avellaneda and Stoikov (2008),
`r = s - q*gamma*sigma^2*(T - t)`, to a program that only buys:

| Market-maker idea | Here |
| --- | --- |
| Skew both bid and ask against inventory | Bid side only. An overweight name gets a deeper discount and a smaller size. An underweight name gets `q = 0` (neutral), never a negative `q`, so no buy is ever enlarged |
| Inventory measured at market value | Measured at **cost basis**. If it used market value, a name's weight would fall as its price fell, `q` would drop, and the rule would keep buying into chronic losers |
| Clock time `T - t` | **Variance time**: the share of the day's forecast variance from this slot to the close, taken from a caller-supplied intraday profile (the U shape). At 15:30 only the last interval's share is left, so the inventory term nearly disappears and the rule trades like TWAP. That keeps budget from going unspent at the close |
| Widen the spread when volatility is high | `kappa * sigma_slot_realized` is added to the discount |
| Spread income, hedging, selling | Do not apply. The program only buys |

## Formulas (`inventory_signals/reservation.py`)

For ticker `i` at slot `s`, with `C_i` the cost-basis dollars committed and `c_i* = w_i * sum(C)`:

- `q_i = clip((C_i - c_i*) / c_i*, 0, q_max)`
- `V_i(s) = sigma_d,i^2 * sum_{k >= s} w_k`. Here `w_k` is the share of daily variance in
  the interval that starts at slot `k`. The 15:30 interval runs to the close.
- `gamma = z / median(sigma_d)`. At `q = 1` with the whole day ahead, a name with median
  volatility needs a discount of `z` daily standard deviations.
- `delta_i(s) = min(cap_sigmas * sigma_d,i, b0 + kappa * sigma_slot,i + gamma * q_i * V_i(s))`,
  in log-return units.
- `max_buy_price_i = p_ref,i * exp(-delta_i(s))`. This is `None` when there is no earlier
  reference price yet, such as the day's first buy.
- `m_i = max(m_min, 1 / (1 + gamma_m * q_i))`, never above 1.0.

These are starting priors, not calibrated values: `z = 1`, `q_max = 2`, `gamma_m = 1`,
`m_min = 0.25`, `b0 = 0 bps`, `kappa = 0.5`, `cap_sigmas = 2`. The validation plan below
allows a 3 x 3 grid over `z` and `kappa` (each in {0.5, 1, 1.5}-style steps) and nothing
wider.

## Inputs and failing closed

The suggester is bound to one slot when it is constructed (`09:35` ... `15:30`). Its
`load_inputs(asof)` loader returns an `InventoryInputs` object, which supplies:

- cost basis for the whole universe;
- the slot's candidate tickers;
- daily volatility forecasts and the last slot interval's realized volatility;
- the seasonal profile;
- optional target weights (equal weight if none are given);
- optional reference prices;
- `known_at`, the time the snapshot was taken.

The suggester makes no network, broker or yfinance call. `evaluate()` returns `ok=False`
with a reason code, and no suggestions, in any of these cases:

- the loader fails;
- the inputs are for the wrong day or slot;
- `known_at` is timezone-naive, later than the clock, more than 60 minutes old, or more than
  5 minutes before the slot. A snapshot from an earlier slot leaves out that morning's buys;
- there is no committed capital;
- a candidate has no cost-basis row or no volatility;
- any number is non-finite or negative;
- the seasonal profile is malformed;
- the target weights do not cover exactly the universe or do not sum to 1.

`publish_outcome` writes nothing for a failed outcome. Every row has `scored=False` and a
`+inf` MAE sentinel, for the same reason as `comovement_signals`: the shared MAE-gated
publisher refuses it.

## Output

`inventory_reservation_suggestions.json` is written to a directory the caller picks. The
publisher refuses every other file name. The file has `schema`, `slot`, `asof`,
`shadow_mode: true`, `eval_metric: {mae: null, scored: false}`, and per-ticker
`multiplier`, `q`, `discount`, `remaining_variance` and `max_buy_price`. An older `asof`
never overwrites a newer one.

## Before this may affect sizing

Two problems with the research note's assumptions surfaced while building this. Each needs
its own reviewed change before this suggester can be consumed:

1. **`sizing_adjustment_reader.py` does not take the minimum.** The note assumed the reader
   combines suggesters by taking the smallest multiplier. Its default `most_conservative`
   instead picks the **smallest absolute adjustment**. So a neutral 0% or a small +3% from
   another suggester would override this suggester's -50%. A shrink-only signal needs a
   true min-multiplier policy for shrink signals.
2. **The reader reads single-ticker files only** (`{"ticker": ..., "multiplier_adjustment_pct": ...}`).
   It cannot read this batch file, and it cannot read the co-movement batch files either.

A threshold output (`max_buy_price`) has no slot in `SizingSuggestion`. Consuming it would
mean changing the same-day top-up gate in all three entry points (see
`docs/live_wiring.md`). That is a decision, not a cleanup.

## Validation plan (not yet run)

1. Replay point-in-time data, from the program's own slot snapshots first. Compute every
   feature only from bars that end before the slot. Use a seasonal profile and volatility
   forecasts estimated on a window ending the prior day. Implied volatility enters with a
   one-day lag. yfinance is allowed in this replay only.
2. Rebuild the universe for each date from the git history of `config/tickers.json`,
   keeping names that were later removed. State with every result that the universe was
   hand-picked.
3. Run a counterfactual: the same days, slots, gates and budgets, baseline versus baseline
   plus this suggester. Credit unspent cash at the T-bill rate. Add a fixed $1-per-ticker
   control.
4. Model fills at the next bar's ask, or at mid plus the observed median slippage, never at
   the decision price.
5. Judge the metrics in this order:
   - concentration: max single-name share of cost basis, the Herfindahl index, and the
     largest correlated-cluster share;
   - position drawdown and 1-month CVaR, including a replay of the 2026-10-14 and
     2026-10-22 shock days;
   - deployment: unspent budget and $900 weekly-cap breaches;
   - cost versus VWAP and versus the TWAP of the seven slot prices;
   - terminal wealth.

   Do **not** use "average cost below average price" or IRR as success metrics (Hayley 2014).
6. Report the Deflated Sharpe Ratio and the probability of backtest overfitting (PBO) on the
   wealth-difference series. Walk forward month by month.
7. Run shadow mode for at least 4 weeks: publish live without consumption, next to the
   actual orders.
8. Promote only if all of these hold:
   - concentration and drawdown fall materially;
   - unspent budget rises by no more than an agreed tolerance;
   - terminal wealth is no worse, statistically.

## Sources and confidence

The design and the literature claims come from an operator-supplied research note
(2026-11-23). They were **not re-verified** against the papers for this change:
Avellaneda-Stoikov 2008; Gueant-Lehalle-Fernandez-Tapia 2013; Cartea-Jaimungal 2016;
Almgren-Chriss 2000; Almgren-Lorenz 2007; Vanguard (Finlay and Zorn 2023); Hayley 2014;
Harvey et al. 2018; Cederburg et al. 2020; Andersen-Bollerslev 1997. The dashboard figures
the note cites (the 2026-11-16 week, the October volatility refresh) came from Notion and
were not re-checked either.

The interface contract is verified against the code: `dca_shared_interfaces.forecasting`,
`publishing.py`, `comovement_signals/` and `sizing_adjustment_reader.py`. The note's
pseudo-code assumed a different contract, with a `ctx` object, a neutral return on failure
and a min-combine reader. This implementation follows the repo instead.
