# ADR: A suggest-only shadow log of cash-secured puts

- **Status:** Accepted (2026-11-03, operator "accept all"; open questions take their proposed defaults). Operator approved this plan on 2026-11-01 after the research
  report "Options overlay for DCA portfolio" (2026-11-02). Open questions carry labeled proposed
  defaults.
  **Amended 2026-11-02:** Amendment 1 below (18 to 46 day window, a cap-waived track, 4:20 PM ET
  capture).
- **Scope:** research only. No code places, previews or reviews an options order. The live
  prompts, `config/` and all three entry points are untouched. Shadow records live in the Phase 0
  DuckDB ledger (`docs/adr-outcome-ledger-pit-harness.md`).

## Context

The operator wants to explore options to improve returns and offset losses. The research found:

- Index evidence: put-writing lowered volatility by about a third but did not beat buy-and-hold
  (CBOE PUT 10.1% a year at 10.1% volatility vs S&P 500 9.8% at 15.3%, 1986 to 2015, before
  costs), with worse crash skew. High-income call writing lagged badly.
- Options cannot offset losses for free: premium pays for giving up upside or taking downside.
- Feasibility: one contract is 100 shares. The bot's account and the fractional DCA positions
  cannot support covered calls; the only realistic use is an occasional cash-secured put in the
  main margin account or an IRA.
- The Robinhood connector offers options data and `review_option_order` but no options order tool,
  so any live trade is manual.

The question is therefore narrow: on this account's real bid prices, does selling a cash-secured
put on a name the DCA system already wants to own beat leaving the same cash in the High-Yield
Cash sweep (3.60% APY, Sept 2026) or in plain DCA?

## Decision (proposed)

1. **Shadow log only, for at least 6 months.** A separate suggester writes one record per
   candidate per trading day: ticker, expiry, strike, bid, ask, mid, implied volatility, delta,
   days to expiry, quote timestamp, underlying price, and the collateral it would lock. No order
   is ever sent or reviewed.
2. **Candidates.** Active DCA tickers only, 25 to 45 days to expiry, delta between -0.30 and
   -0.15, bid-ask spread at most 10% of mid, and a strike at or below the price the graded ladder
   would already buy at.
3. **Settlement.** Each shadow trade is entered at the **bid** and settled at expiry or modeled
   assignment using later point-in-time prices; an early close is costed at the **ask**.
   Assignment risk is modeled around ex-dividend dates and deep in-the-money moves.
4. **Benchmarks.** The same collateral in the cash sweep, and the same dollars in plain DCA of
   that ticker over the same period.
5. **Pass bar after at least 6 months and 20 settled trades:** net return on locked collateral
   beats the cash sweep; worst single-trade loss is smaller than 12 months of premium collected;
   overlay drawdown is no worse than holding the stock outright; zero shadow assignments would
   have breached concentration or wash-sale rules; every data failure dropped the suggestion and
   never blocked DCA.
6. **Wash-sale interaction.** A ticker inside a tax-loss pause (`docs/adr-tax-loss-sale.md`) gets
   no put suggestions, and open shadow puts on it block that ticker's tax-loss eligibility, so the
   shadow log rehearses the real rule.
7. **Independence and fail closed.** The suggester is independent of the sizing suggesters and
   never changes DCA sizing. Missing or stale chain data produces no record for that ticker that
   day, never an estimate.
8. **After a pass**, a separate ADR would define a human pilot: one contract at a time, placed by
   the operator after `review_option_order`, with a written exit rule.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Accounts in scope | Main margin account first; IRAs later if level allows. Bot cash account out |
| 2 | Where the suggester lives | Research tree, beside the Phase 0 ledger in `dca-sizing-signals/` |
| 3 | Spread cap | 10% of mid |
| 4 | Collateral limits | At most 10% of the account per contract, 25% in total; none on a sector already above 30% of the account |
| 5 | Chain data source | Robinhood read-only option chains and quotes, captured daily; DoltHub for backfill (cast strings) |
| 6 | Benchmarks | Both the cash sweep and plain DCA |
| 7 | Who settles trades | The weekly review job, from point-in-time prices |
| 8 | Writing a put inside a wash-sale window | Treated as blocking until a tax professional confirms |
| 9 | Options level in each account | Operator confirms in the app (Level 2 cash-secured put permission is unverified) |

## Amendment 1 (2026-11-02): window, cap-waived track, capture time

Operator approved on 2026-11-02, after the first real chain capture (PR #2111, fixture
`dca-sizing-signals/tests/ledger/fixtures/rh_option_chains_2026-11-02.json`) showed that with
the original defaults no contract qualified for a $50,000 account:

- single-name put spreads were 22% to 181% of mid (GLW, KR), far above the 10% cap;
- the 25 to 45 day window missed both monthly expirations (Nov 20 at 18 days, Dec 18 at 46);
- the one liquid name, QQQ (spread 2.5%), needs $64,000 or more of collateral per contract.

Six months of "zero trades" would not answer the ADR's question, so:

1. **Window 18 to 46 days.** Decision 2's "25 to 45 days" becomes **18 to 46 days**, so the
   standard monthly expiration falls inside the window in most weeks. The chain capture
   follows the selector's window automatically.
2. **A separate cap-waived track.** Alongside the real track, the shadow log keeps a second
   set of shadow trades selected with **no collateral or sector caps** (every other filter,
   the tax-loss pause and fail-closed data rules unchanged). It answers "would the strategy
   beat the sweep if the account were large enough", and is never presented as tradable.
   It lives in its own tables (`csp_shadow_cap_waived_entries`,
   `csp_shadow_cap_waived_settlements`) and gets its own scorecard, so it can never mix into
   the real track's pass bar. Its open puts do **not** block tax-loss eligibility (decision 6),
   because the account could not have held them.
3. **Capture time 4:20 PM ET** on the Mac Studio, after the 4:15 PM option close (QQQ quotes
   updated until 4:14:59 PM). This is a research job, not part of the afternoon DCA chain; it
   is a second exception to the 3:40 PM rule, beside the Friday 5:00 PM Notion refresh.

Amendment open questions:

| # | Question | Proposed default |
|---|---|---|
| A1 | Does the cap-waived track still allow only one open put per ticker? | Yes |
| A2 | Should the real track's pass bar ever look at the cap-waived results? | No, report them side by side only |

## Consequences

- About six months of data collection before any decision. The likely outcome is "not better than
  the cash sweep", which is still a useful answer.
- No new execution path and no new risk to live trading.

## Verification

- A test proves no code path in the suggester can call an order, review or cancel tool.
- A test proves a ticker in a tax-loss pause never receives a suggestion.
- A test proves shadow entries use the bid and early exits the ask, never the mid.

## Rollback

Revert the PRs and drop the shadow tables. Nothing in the live path reads them.
