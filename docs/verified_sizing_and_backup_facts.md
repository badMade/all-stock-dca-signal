# Verified sizing and backup-executor facts (2026-10-07)

Facts found while building the plan-first backup executor (`docs/adr-plan-first-backup-executor.md`) that are
true of the repo regardless of that design. Each line names the file or key it was checked against, so it can be
re-checked. Nothing here changes behavior; it records where older prose is stale or where two sources disagree.

## Sizing

- **Live sizing is per-ticker calibration, not the global ladder.** `config/per_ticker_sizing.json` is `active`
  (version `2026-10-05`) and has an entry for every active ticker: `$1.00` base and `$1.00` below-close base,
  linearly interpolated tiers, multipliers up to `10.0`. `config/sizing_ladder.json` (15 tiers, `$2`/`$1` base, 8x
  at 16% and above) is only the fallback for a ticker without a calibration.
- **The smallest buying drop is per ticker.** A calibrated ticker buys from a drop of about 0.01%; the global
  ladder buys from 0.1%.
- **The effective per-order cap is `$16`.** `dca_runner.MAX_PER_ORDER_USD` is derived as `base_amount_usd x
  max(ladder multiplier)` and `config/budgets.json` `circuit_breakers.max_per_order_usd` is `16.0`. The `$10` and
  "max 5x" wording is stale in `config/sizing_ladder.json` (`historical_note`, `rescaled_2026_08_02`),
  `config/budgets.json` (`hard_limits`) and `CLAUDE.md`.
- **A non-finite price drop (NaN or infinity) returns 0.0 on both ladders.** The interpolated and the stepped
  ladder both fail closed (`dca_ladder_core`, ported from main's `55e0340`). Before that fix an interpolated curve
  fell through to its top multiplier (10.0) on NaN; that edge no longer exists.

## Universe

- `config/tickers.json` has 90 active symbols (APH and TEL added 2026-10-12, GFS added 2026-10-14, 22 free-cash-flow-yield names added 2026-10-19). RVI, VCX and TRLV are active (re-added 2026-08-16 by the owner).
  QBUT is the only excluded symbol; BYDDY and RVII are `whole_share`.

## Same-day repeats

- `config/budgets.json` `order_rules.one_order_per_ticker_per_day` is `false`: a later slot may buy a ticker
  again, resized by `scripts/same_day_top_up_gate.py` and only below the prior reference price.
  `one_order_per_ticker_per_stream` is `true`.

## How the live slots place orders

- Placement in the seven live prompts is prose, not code. No live prompt calls `assess()` or `execute_plan()`
  (`docs/live_wiring.md`); the prompts call scripts for candidates and the top-up gate, then place through the
  broker tool directly.
- A reused `ref_id` is not known to give broker-side deduplication. Nothing in the repo or in order history
  proves it either way.

## Where two sources disagree

- `config/scheduled_tasks.json` `standing_perplexity_backup_policy.mechanism` says Perplexity "stands down and
  places no orders" when Claude evidence exists for the slot. `claude/PERPLEXITY-CLAUDE-COORDINATION.md` section 1
  (fixed 2026-08-02) designs ticker-level exclusion so a partly finished Claude batch is completed. #1618 rewrote
  the Perplexity prompt mirrors in `claude/perplexity-prompts/` to the ticker-level behavior; the JSON text was
  not updated to match.
- Two sets of Perplexity backup prompts exist: the hand-maintained mirrors in `claude/perplexity-prompts/` (#1618)
  and the generated set in `claude/dca-executor-prompts/perplexity/` (#1619), which differ on the 10:50 task's role,
  the same-day rule and the `ref_id` format.

## Accepted limitation

- The 15:45 ET backup may run out of time before the 16:00 ET close if a Mac-first check is added in front of it.
  Accepted by the owner on 2026-10-06; no slot-specific timeout or shortcut is to be added for it.
