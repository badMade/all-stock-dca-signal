# Ticker advisory tool

`ticker-advisor/` scores candidate tickers against three signals and proposes additions to the
active universe for manual review. It is a self-contained, heavy-dependency subproject (own
`pyproject.toml`/`requirements.txt`/tests), excluded from the core repo's stdlib-only ruff and
coverage gates — see `CLAUDE.md`'s Conventions section and `pyproject.toml`'s `extend-exclude`/
`omit` entries.

## Why this exists

Before this tool, there was no tooling to help decide *which* tickers are worth adding to the
DCA universe — every addition documented in `docs/strategy.md`'s "TICKER UNIVERSE UPDATE" entries
was a purely ad hoc, owner-initiated decision, verified only for broker tradability. This tool
adds a scoring layer in front of that decision — it does not replace it.

## What it does

Given a candidate list you supply, it computes three independent signals per candidate
(strategy-health metrics, a backtested-strategy performance check, and news/SEC-filing
sentiment), combines them into one composite score, and classifies each candidate ADD / WATCH /
REJECT. See `ticker-advisor/README.md` for the full signal breakdown and usage.

It writes its output to `analysis/<date>-ticker-advisory/<time>/` — a ranked report and, for
ADD-classified candidates, a `proposed_tickers_diff.json` describing the literal edit needed to
add them to `config/tickers.json`'s `active` array, `near_close_dca.ACTIVE_TICKERS`, and
`config/scheduled_tasks.json`'s `canonical_first_buy_and_topup_rule.active_universe.active` list
(that field mirrors `config/tickers.json`'s `active` array per its own `source_of_truth` note, but
nothing keeps the two in sync automatically), all in matching order.

## How its output feeds the existing manual workflow

This tool **never writes to `config/tickers.json`, `near_close_dca.py`, or
`config/scheduled_tasks.json` itself.** Applying a proposed addition still follows the exact
process already documented in `docs/strategy.md`:

1. Run `advise_tickers.py` on your candidate list, review `scores.md` / `proposed_tickers_diff.json`.
2. Verify broker tradability via Robinhood MCP for anything you intend to apply — this repo has
   no broker/network access, so this tool cannot do that check itself. Confirm the symbol
   resolves to a real tradable equity/ETF, not a cash-settled index or derivative (see the `DJX`
   rejection precedent in `docs/strategy.md`).
3. Apply all three edits from `proposed_tickers_diff.json` by hand (or have an agent apply them):
   `config/tickers.json`, `near_close_dca.py`, and `config/scheduled_tasks.json`, preserving order
   across all three. Skipping the `config/scheduled_tasks.json` edit leaves the scheduled
   execution contract looking at a stale universe even after the other two files are correct --
   this exact omission already happened once in this repo (see `config/scheduled_tasks.json`'s own
   `ticker_universe_update_2026_08_04` note).
4. Run `test_config_invariants.py` to confirm `config/tickers.json` and `near_close_dca.py` stayed
   in lockstep (it does **not** check `config/scheduled_tasks.json` -- verify that one by hand).
5. Add a "TICKER UNIVERSE UPDATE" changelog entry to `docs/strategy.md` and
   `config/tickers.json`'s `notes` field, per the existing convention.
6. Once the new ticker has enough archived price history,
   `python -m scripts.generate_per_ticker_sizing` will calibrate its sizing tiers from real drop
   percentiles; until then it uses the global fallback ladder (`GLOBAL_FALLBACK_TIERS`).

## Phase 2 (not built yet): influencing sizing, not just universe membership

The tool's composite score is currently used for one decision only: whether to propose a ticker
for the active universe. It does not touch `near_close_dca.py`'s sizing math at all.

A natural next step — explicitly **not** implemented by this change — would be letting a
composite score (recomputed periodically for tickers *already* in the active universe, not just
new candidates) adjust the sizing ladder that `graded_buy_multiplier()` and
`select_first_allocation_base_usd()` (`near_close_dca.py`) compute from
`config/per_ticker_sizing.json`. The rough shape such a change would need to take, drawn from the
constraints already established for this tool:

- A bounded, explainable adjustment — e.g. a composite score clamped to something like ±20% on
  the resolved multiplier or base amount, never an unbounded scaling factor. The existing ladder
  math (`scripts/generate_per_ticker_sizing.py`'s percentile-based tiers) stays the primary
  driver; a score-based adjustment would be a secondary nudge on top of it, not a replacement.
- Still offline-computed, still human/agent-reviewed before publishing — the same
  `daily_per_ticker_calibration.py`-style guarded-publish pattern this tool's ticker-advisory
  flow already follows, not a live inline call to `sentiment_score()`/`backtest_score()` during
  a trading run.
- Its own schema and its own test coverage in the core suite, since (unlike ticker-universe
  membership) this would directly change a real per-trade dollar amount on every live entry
  point that calls `evaluate_source_signal()`.

Per `CLAUDE.md`'s "Consolidating is a decision, not a cleanup" principle, wiring this in deserves
its own dedicated review once the ticker-advisory scoring pipeline above has a track record —
this document exists to record the intended shape of that follow-up, not to implement it.

### Implemented: the ml-forecaster sibling tool

A related, but separate, mechanism following the same shape now exists: `ml-forecaster/` (a
sibling directory, isolated from the core stdlib-only gate exactly like `ticker-advisor/`) trains
a pooled, regularized (Ridge) regression over the active universe's OHLCV history, predicting a
forward 5-day return conditional on today being a dip day, walk-forward validated and
guard-published (refuses to overwrite a better-performing prior model). Its output is
`config/ml_sizing_adjustment.json` (schema: `schemas/ml_sizing_adjustment.schema.json`) — a
bounded (±20%, hard-capped in the schema itself), staleness-checked, per-ticker multiplier that
`near_close_dca.graded_buy_multiplier()` (`near_close_dca.py`) applies as a small nudge on top of
the primary ladder, never as a replacement for it. Consumption is entirely read-only, stdlib
`json`-only (`dca_config.load_ml_sizing_adjustment`), and fails closed to a pure no-op (every
ticker's factor is 1.0) on a missing, malformed, stale, or out-of-bounds publish — see that
function's docstring for the exact fail-closed contract.

Publish gate (2026-11-11, operator decision): a model must beat the naive MAE baselines **or** pass the
rank-IC path -- every walk-forward fold's Spearman rank IC > 0, at least 3 folds, mean >= 0.02
(`ml-forecaster/publish.py`, `MIN_RANK_IC_FOLDS` / `MIN_MEAN_RANK_IC`). Sizing only needs the model to order dip
days, and the 2026-11 retrain had rank IC +0.04 in all five folds while MAE trailed the baseline.

Incumbent comparison (2026-11-24): a candidate admitted only through the rank-IC path is compared with the
incumbent on mean rank IC (strictly higher than the recorded `eval_metric.model_rank_ic`, when one exists), not
on the incumbent's MAE skill ratio -- but only when the incumbent itself failed the MAE baseline. Before this,
the 2026-11-12 incumbent (skill ratio 1.0055) set an MAE floor no later rank-IC model could clear, and 12 of 13
retrains were `blocked_regression`. An incumbent that beat the MAE baseline keeps the full MAE comparison.
Publishing records the mean rank IC as `eval_metric.model_rank_ic` for the next retrain. The output is still
shadow-only: `config/ml_sizing_adjustment.json` stays `inactive`.

This is a distinct mechanism from the ticker-advisory composite score above (different model,
different config file, different consumption point), built to the same design principles this
section already laid out: bounded, offline, guard-published, fail-closed. Like the accumulator
in `docs/inverse_martingale.md`, it is wired into the canonical `near_close_dca.py` path only —
see `docs/live_wiring.md`'s "Three entry points" section.
