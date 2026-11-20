# ADR: Dated no-buy window for tickers sold at a loss

- **Status:** Accepted 2026-11-20 (operator chose this design in chat after the exclusion-list
  alternative was costed). Slice 1 and 2 are implemented; later slices are listed below.
- **Builds on:** `docs/adr-tax-loss-sale.md` (item 3, the buy pause, and open question 4, "how the
  executor enforces the pause"). This is the dedicated mechanism that ADR deferred.
- **Not tax advice.** Confirm every consequence with a tax professional before filing.

## Context

On 2026-11-20 the operator sold DFTX, RGTI, LITE, CBRS, VCX, INTC and SNDK at a realized loss for
tax-loss harvesting. The DCA program buys most names almost daily, and a purchase of the same stock
within 30 days before or after a loss sale can wash the loss (IRC section 1091). Buys in any of the
taxpayer's accounts count, and a buy in an IRA or Roth can disallow the loss permanently. A read-only
lookback the same day found the DCA account (the MCP account) holding many small fractional buys of six
of the seven names from the prior 30 days, so every further scheduled buy adds replacement shares.

The interim named in the tax-loss ADR is the permanent `excluded` list. Trying that in a scratch copy
showed it touches about ten files (the `EXCLUDED_TICKERS` constant, the `scheduled_tasks.json` mirror,
`ticker_history.csv`, docs and at least six tests that pin the universe size and the excluded set), stops
tracking, and has to be reverted by hand on the day buying resumes.

## Decision

1. `config/tickers.json` gets an optional `no_buy` object keyed by ticker. Each entry has `blocked_on`,
   `until`, `reason` and optionally `released_on` and `release_reason`. A ticker is blocked when
   `blocked_on <= today <= until` (both inclusive, US Eastern) unless `released_on` is set and reached.
2. The window is 31 calendar days after the sale date: 30 days of the statutory window plus a one-day
   buffer. For the 2026-11-20 sales, the last blocked day is 2026-12-21 and buying resumes 2026-12-22. The
   statutory minimum would resume on 2026-12-21.
3. `dca_config.no_buy_blocked_tickers(cfg, on)` is the one rule. It fails closed: a malformed block
   raises `ValueError` naming `no_buy`, and one bad entry fails the whole load. A symbol must be in
   `active` and not in the permanent `excluded` list.
4. `dca_config.load_tickers` overlays the blocked set. Blocked symbols are added to `excluded_tickers`
   and removed from the runtime `tickers` tuple (the resolved `StrategyConfig` requires those two to be
   disjoint). `tickers.json`'s own `active` list is untouched, so the ticker stays in the history fetch,
   sizing calibration and universe records, and returns to the runtime tuple on its own the day after
   `until`. `load_strategy_config(now=...)` uses the run's own clock, so a replayed day sees that day's
   windows.
5. That one overlay reaches `near_close_dca` (the 3:30 PM path), `morning_dca` and its three subclasses,
   and `scripts/generate_buy_candidates.py`. `dca_runner.py` reads `tickers.json` itself, so it checks the
   block at call time in tier selection, `plan_orders` (refusal `no_buy_window`) and `execute_plan`.
6. Early release is `python -m scripts.no_buy_window release SYMBOL --reason "..." --accept-wash`. A
   release inside the window refuses without `--accept-wash`, always needs a reason, sets `released_on`
   (never deletes the entry), and changes only that entry's line. `status` lists every entry.

## Not covered (known gaps)

- **Signals are not evaluated during the window.** A blocked ticker is in `excluded_tickers`, so the run
  does not score it or write a signals row for it. "Tracking" today means the ticker stays in `active` and
  in the data and sizing pipelines. A would-have-bought record in the run artifacts is a later slice.
- **Bare `StrategyConfig()` constructions** never read `tickers.json` and so never see the block. Every
  live path found uses `load_strategy_config` or the stream loaders; a new code path that builds a bare
  config would bypass it.
- **The live prompts** in `claude/dca-executor-prompts/live-mirror` hold no ticker lists and obey the
  candidate files and `dca_runner`, which enforce the block. Nothing in this repo can stop an external
  agent that ignores both.
- **Manual orders and other accounts.** The block only stops this program. It does not watch the main
  account, the IRAs or the managed accounts, dividend reinvestment, or a spouse's accounts.
- **BYDDY** (a whole-share ticker sold at a loss on 2026-11-17) is not blocked: the config forbids a
  ticker in both `excluded` and `whole_share`. Its exposure is small, about one share per accumulator fire.
- **No lookback.** The 30 days before a sale are not checked. That needs the cross-account lot ledger.

## Later slices (each needs its own test first)

1. A single guard at order submission plus a pre-sale lookback report.
2. A cross-account lot ledger and a daily loss-sale detector that proposes (never writes) new entries.
3. A blocked-buy record in run artifacts.
4. A year-end reconciliation against the 1099-B for Form 8949 code W lines.

## Rollback

Delete the `no_buy` block from `config/tickers.json`. Nothing else holds state. Remove the overlay in
`dca_config.load_tickers` and the three checks in `dca_runner.py` to remove the mechanism itself.

## Verification

`tests/test_no_buy_window.py` (the rule), `tests/test_no_buy_enforcement.py` (the loader, each stream
config and `dca_runner`) and `scripts/tests/test_no_buy_window_cli.py` (release safety).
