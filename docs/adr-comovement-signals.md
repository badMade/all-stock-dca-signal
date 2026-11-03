# ADR: Co-movement signals for the DCA system (pairs as signals, not a strategy)

- **Status:** Accepted (2026-11-03, operator "accept all"; open questions take their proposed defaults). Operator decision 2026-11-01: use pairs and co-movement as
  **signals** inside the existing DCA system, and do **not** implement pairs trading as a
  strategy. Operator asked to build signals 1 and 2 first, then continue on 3 to 5.
- **Scope:** research-only suggesters in `dca-sizing-signals/`. They publish bounded suggestions
  through the existing `SizingSuggester` / `SuggestionPublisher` boundary and never control live
  sizing. Wiring any of them into a live slot needs its own later ADR. `config/`, the live
  prompts and all three entry points are untouched.
- **Source research:** "Pairs trading for DCA portfolio" report and notes (2026-11-02), including
  a read-only co-movement analysis of the 91 active tickers (2024-10-28 to 2026-10-30).

## Context

- Classic pairs trading (long one leg, short the other) has decayed: about 0.24% a month in
  2003 to 2009 before costs (Do and Faff 2010), roughly zero after realistic costs, and a simple
  retail model turned negative net of costs in a 2025 study. It also needs shorting, which this
  buy-only system does not do.
- Co-movement information is still useful for **where** DCA dollars go and **how much** a dip
  signal is worth:
  - About 27 active names (AI, semis, networking, broad tech ETFs) form one bloc with an average
    daily-return correlation of 0.58, so they are far fewer independent bets than 27.
  - Shared selloff days (2026-10-22: 21 of 44 names) currently produce many "independent" dip
    escalations that are really one market event.
  - Real single-stock pairs exist (STX/WDC 0.87, COHR/LITE 0.81, AMAT/ASML 0.81, QBTS/RGTI 0.78)
    but none survived a multiple-testing correction for cointegration, and rolling correlations
    swing widely (STX/WDC 0.59 to 0.95).

## Decision (proposed)

| # | Signal | Output (suggest-only) | Status |
|---|---|---|---|
| 1 | **Cluster concentration cap** | Per-cluster daily dollar ceiling as a share of the day's budget; suggested per-ticker multiplier scale-down when a cluster would exceed it | Build now |
| 2 | **Correlated selloff detector** | When at least a set share of a cluster (or the whole universe) is down together, mark the day as one correlated event and suggest capping escalation for that cluster at a single shared budget instead of N independent escalations | Build now |
| 3 | **Pair breakdown warning** | When a normally tight pair's rolling correlation falls far below its own history, flag both names and suggest holding them at 1x | Shadow experiment |
| 4 | **Cheaper-leg tilt** | Within a close-substitute pair, suggest shifting the pair's DCA dollars toward the leg that is cheap on the spread z-score, bounded (for example 0.2 to 0.8 of the pair's dollars) | Shadow experiment, speculative |
| 5 | **Staples vs semis rotation** | A weak regime indicator logged for research | Log only |

Rules shared by all five:

1. **Clusters are point-in-time.** Built from a trailing window of daily returns (proposed: 252
   sessions, refreshed weekly) using only data dated on or before the as-of date. A ticker with
   too little history is its own singleton cluster.
2. **Bounded and independent.** Each signal publishes its own suggestion file. None writes into
   another suggester's file. Combination happens only in `SizingAdjustmentReader`, and the kill
   switch keeps the last word.
3. **Fail closed.** Missing or stale returns, or a clustering run that fails its own checks,
   publishes nothing, and the existing tier logic runs unchanged.
4. **Scored before use.** Every signal is scored in the Phase 0 ledger against the baseline buys
   before anyone proposes live wiring.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Correlation window and refresh | 252 sessions, refreshed weekly |
| 2 | Clustering method | Average-linkage hierarchical clustering on 1 minus correlation, cut at distance 0.5 (correlation about 0.5) |
| 3 | Cluster cap | No cluster above 35% of the day's spend |
| 4 | Correlated-selloff trigger | At least 60% of a cluster with 3 or more members down at least 2% on the day |
| 5 | Escalation under a correlated selloff | The cluster shares one escalation budget equal to its single largest member's escalation |
| 6 | Breakdown warning trigger (signal 3) | 60-day correlation below its 2-year 10th percentile for 5 sessions |
| 7 | Tilt bounds (signal 4) | 0.2 to 0.8 of the pair's dollars; pairs with daily correlation of at least 0.75 only |

## Consequences

- Fewer dollars stacked into one theme on broad selloff days, and a truer picture of
  diversification.
- No new return claim. The value is risk control, measured by the ledger.

## Verification

- A test proves clustering never uses a return dated after the as-of date.
- A test proves a missing or stale input publishes nothing.
- Shadow-score signals 1 and 2 against the Phase 0 baseline for at least one quarter before any
  live-wiring ADR.

## Rollback

Revert the PRs and delete the suggestion files. Nothing in the live path reads them.
