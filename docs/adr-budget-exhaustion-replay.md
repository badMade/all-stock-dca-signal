# ADR: Budget-exhaustion replay (gate for the Markov category-1 ideas)

Status: proposed. Implementation blocked until the open questions in section 7 are answered.
Decided 2026-11-23 (operator): report location is a CLI only, no console page for now.
Date: 2026-11-23
Related: `claude/markov-extensions.md`, `HANDOFF_budget_exhaustion_opportunity.md`, draft PR #2403.

## 1) Context

Before building the MDP cash-pacing suggester or the absorbing tier chain, we need one number:
how often a later scheduled slot saw a buyable dip after the day's budget could no longer fund
the buy the live system would have made.

Revision 2 of the spec priced that buy with the global ladder only. The operator rejected that
shortcut (2026-11-23): the count must match live sizing, per-ticker ladders included. Reading
the live code shows what a faithful replay needs:

| Live input | Where it lives | In the DuckDB ledger? |
|---|---|---|
| Per-ticker ladder: own tiers, $1 bases, optional linear interpolation up to 10x | `config/per_ticker_sizing.json` via `dca_config.load_per_ticker_sizing` (recalibrated over time) | No. Versions only in git history |
| Global ladder and bases | `config/sizing_ladder.json` | No. Versions in git history |
| ML nudge (applied before the 10x ceiling) | `config/ml_sizing_adjustment.json` (currently `inactive`) | No |
| Inverse-Martingale suppression (ladder = 0) | `config/inverse_martingale.json` | No |
| Kill switch (cap to 1x when 7-day deployment > 40% of $3,600) | `dca_risk`: counts scheduled evidence at/after 2:30 PM ET only | Partly: fills have times, but the evidence rule reads run artifacts |
| Same-day top-up: preserved first-allocation base, reference price, incremental delta, $0.50 price-step rule, sub-dollar pending credits | `near_close_dca.evaluate_top_up_guard`, `PriorSameDayAllocation`, `CumulativeCreditLedger` | No. Base and reference live in run artifacts; credits in the credit ledger |
| Day's budget tier ($180 to $360, chosen by the count of 3x+ signals) | `near_close_dca.select_budget_tier` | No |

Copying that logic into `dca-sizing-signals` would fork about 400 lines of top-up rules and drift
silently. `dca-sizing-signals` also may not import `near_close_dca`
(`tests/test_architecture_boundaries.py`).

## 2) Decision (proposed)

Split the work in two. Live sizing code is called, never copied.

**A. Extract (dca-sizing-signals, ledger package).** A read-only, point-in-time exporter that
returns plain rows: visible intraday quotes, scheduled fills (manual ids excluded), and the prior
session's close and low per quote. No sizing logic. Revision 2's point-in-time, fail-closed,
manual-exclusion and read-only tests move here unchanged; its ladder tests move to B.

**B. Replay (repo root, `scripts/budget_exhaustion_replay.py`, stdlib only).** For each quote:

1. Build the `StrategyConfig` in force at the quote's timestamp from the config versions in force
   then, loaded with the real `dca_config` loaders.
2. Compute the planned buy with the live functions: `select_first_allocation_base_usd`,
   `graded_buy_multiplier` (per-ticker tiers, interpolation, ML factor, 10x ceiling, kill switch,
   inverse-Martingale suppression), then `evaluate_top_up_guard` with the same-day prior
   allocation rebuilt from earlier scheduled fills and their run artifacts.
3. Spend before the quote comes from scheduled fills. Event when planned > remaining budget.
4. Report per daily cap, per dip kind, and (if question 4 says so) at the replayed day tier.

Parity is by construction: B imports the code that sizes live orders. A test pins that B calls
those functions rather than reimplementing any of them.

## 3) Non-negotiable constraints

- Read-only. Never writes state, config, artifacts or the ledger. Never imports a broker adapter.
- Point in time: every input (quote, fill, bar, config version, artifact) must be known at the
  quote's timestamp. A config version committed after a session is never applied to it.
- Fail closed: an input that cannot be resolved point in time (missing config version, missing
  prior-allocation base) marks that quote `unresolved` with a reason. It is counted and reported,
  never guessed and never silently dropped. The report states the unresolved share.
- Manual runs and manual order ids never count (docs/run_classification.md).

## 4) Evaluation

The report shows, per cap and per dip kind: sessions observed, sessions with a missed or
underfunded buy, dollars short, and the unresolved share. If missed sessions are rare, the MDP and
the absorbing chain drop down the roadmap (claude/markov-extensions.md).

## 5) Consequences

- No new dependency: B is stdlib plus repo modules; A already depends on duckdb.
- A becomes the first exporter other replays (CSP shadow, goals) can reuse.
- The replay inherits the top-up guard's own KNOWN GAP (artifact-only prior allocation, no broker
  order history merge). The report must say so.

## 6) Alternatives considered

- Global ladder only (revision 2): rejected by the operator; misprices calibrated tickers.
- Copy the ladder and top-up rules into dca-sizing-signals: rejected; forks live logic and drifts.
- Import `dca_ladder_core` only: covers base and multiplier, not the top-up guard or the kill switch.
- Read planned dollars from `SAFETY_CAP` blocks in run artifacts: exact for slots that ran and
  recorded a block, but silent for slots that never evaluated the ticker. Kept as a cross-check (Q6).

## 7) Open questions (must be answered before implementation)

1. Approve the split: extract in `dca-sizing-signals`, replay in `scripts/` importing `near_close_dca`?
2. Config effective time. Options: (a) git commit time on `main` (a lower bound; the Mac may have
   pulled later), (b) a config version or hash recorded in run artifacts, if one exists, (c) both,
   preferring (b). Which one?
3. The same-day prior allocation needs each first buy's selected base and reference price, which
   live only in run artifacts under the cron tracking tree on the Mac. Is requiring that tree as an
   input acceptable (replay runs on the Mac only)?
4. Day budget tier: keep the sweep over all four caps, also replay `select_budget_tier` from the
   day's 3x+ signal count, or both? (Recommendation: both.)
5. Sub-dollar pending credits: replay from the `CumulativeCreditLedger` file, or treat as $0 and flag?
6. Use recorded `SAFETY_CAP` blocks in run artifacts as a cross-check on the replay?
