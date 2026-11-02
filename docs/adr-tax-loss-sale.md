# ADR: One allowed sell type, an operator-approved tax-loss sale with a buy pause

- **Status:** Proposed (2026-11-02). Operator asked for this on 2026-11-01: "allow just one kind:
  an operator-approved tax-loss sale, with a 31-day buy pause on that ticker." Open questions
  carry labeled proposed defaults.
- **Scope:** design only. No code in this PR places, previews or reviews a sell order. The live
  prompts, `config/`, and all three entry points are untouched. Later PRs add a research-only
  candidate report and wash-sale calculator on the Phase 0 tax-lot ledger
  (`docs/adr-outcome-ledger-pit-harness.md`). Enforcing the buy pause in the executor needs its
  own later ADR.

## Context

The system is buy-only today, and that is part of its safety model: a failure means "do not
act", and nothing it does is hard to undo. A sale cannot be undone. Several positions in the main
account carry large unrealized losses (for example VCX, CBRS, QBTS on the 2026-10-30 marks), and
a realized loss can offset gains and up to $3,000 a year of ordinary income.

The wash-sale rule (IRC section 1091, IRS Publication 550) disallows a loss when substantially
identical stock is bought within **30 days before or 30 days after** the sale, in any of the
taxpayer's accounts, including an IRA (where the loss is lost permanently). This system buys most
tickers almost every trading day, so:

- a buy pause that starts **at** the sale is not enough: lots bought in the 30 days before the
  sale also trigger the rule for the matching shares;
- buys in the operator's other accounts, dividend reinvestment, and manual orders count too.
- the operator's Robinhood login also holds managed accounts (Robinhood-managed individual and
  Roth IRA portfolios, read-only to this system) and self-directed IRAs. A managed portfolio can
  buy the same ticker on its own schedule, and an IRA purchase makes the loss permanently
  disallowed, so the lookback must read every account, not only the agentic one.

## Decision (proposed)

1. **Only one sell type exists:** a tax-loss sale of specific lots of one ticker, approved by the
   operator for that ticker, that date and that share count. No other sell is ever generated.
2. **Never automatic.** The system only *proposes*. Each sale needs an explicit operator approval
   recorded in the ledger (approval id, ticker, lots, max shares, expiry). An expired or missing
   approval means no sale.
3. **Buy pause on both sides of the sale.** For the ticker, in every account the ledger tracks:
   - **Pre-sale pause:** no buys for at least 31 calendar days before the sale date. The sale is
     not eligible until the ledger proves zero buys of that ticker in that window.
   - **Post-sale pause:** no buys for 31 calendar days after the sale date.
   - Dividend reinvestment for the ticker must be off for the whole period.
   This is the operator's "31-day buy pause", applied on both sides because daily DCA buys would
   otherwise wash the loss.
4. **Candidate report (research only, weekly).** Lists lots with an unrealized loss of at least
   15% and at least $25, in taxable accounts only, with: estimated realizable loss, holding period
   (short or long term), days of buy pause still needed, and every recent buy that would wash it.
5. **Who places the sale.** The operator places it by hand, or tells Claude in chat to place that
   one approved sale. Claude never places one from a scheduled run.
6. **Fail closed.** Any doubt about the lookback (missing order history in any account, unknown
   DRIP status, unreadable tax lots) means "not eligible". The pause, once started, is only lifted
   by the operator or by the 31-day post-sale window ending.
7. **Re-entry.** After the post-sale pause, the ticker returns to normal DCA if it is still on the
   active list. No re-buy of a "substantially similar" substitute is proposed in this ADR.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Loss threshold for the candidate report | At least 15% and at least $25 per lot |
| 2 | Which accounts count for the wash-sale lookback | All Robinhood accounts the ledger reads (alias-keyed), plus an operator attestation for accounts outside Robinhood |
| 3 | Lot selection | Highest-cost lots first; confirm Robinhood supports specific-lot sells before relying on it |
| 4 | How the executor enforces the pause | Interim: operator adds the ticker to the existing exclusion list for the pause period. Later: a dedicated pause file read by all three entry points, in its own ADR |
| 5 | Should the bench or universe rules buy a substitute during the pause? | No |
| 6 | Year-end timing | Proposals in November and December need the pre-sale pause started by early November to sell before December 31 |

## Consequences

- A paused ticker gets no DCA buys for about 62 days. Its dollars flow to other tickers through
  the normal ladder and caps.
- The first sale breaks the "buy-only" invariant documented across the repo. Docs, the dynamic
  universe ADR (question 9) and the safeguards page must be updated when this is accepted.
- Realized losses need correct reporting; this system does not give tax advice.

## Verification

- A test proves a sale is never eligible while any buy of that ticker exists in the prior 31
  days in any tracked account.
- A test proves no code path generates a sell without a matching, unexpired approval record.
- A dry-run walk-through on one real candidate lot, with the operator, before the first sale.

## Rollback

Revert the PRs. Remove any pause entries by hand. Sales already placed cannot be rolled back,
which is why every one needs an operator approval.
