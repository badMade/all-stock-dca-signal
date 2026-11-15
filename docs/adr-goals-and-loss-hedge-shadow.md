# ADR: Operator goals, a console goal editor, and a shadow-only loss hedge

- **Status:** Accepted (2026-11-13). The operator approved the 12 tests as written (including
  test 12's synthetic returns) and accepted every proposed default under "Open questions".
  Phases 0 and 1, the console Goals panel and the hedge shadow pricing/scorecard functions are
  implemented; Phase 2 goal progress tables and snapshot capture are implemented (the
  `dca-sizing-signals/tests/ledger` and `tests/test_goal_snapshot_capture.py` suites pass, 379
  and 12 tests on 2026-11-15). The daily hedge capture task and its DuckDB tables, the stale-goal
  alert, the 07:31 ET task registration and Phase 4 stay open.
- **Scope:** goals as versioned data, a console page that proposes goal changes as pull
  requests, and a shadow-only loss hedge (options and crypto). **Nothing here changes a live
  dollar amount, places, previews or reviews an order, or edits a file under `config/` at
  runtime.** Live sizing effects of a goal are deferred to a separate ADR (Phase 4 below).

## Context

The operator wants to set goals that can change over time, starting with "20% year-over-year
growth minimum", and wants the learning components to adjust toward them. They also want to
hedge losses with options and, possibly, short-term crypto positions, but **neither hedge may
trade yet**. They want to edit goals through the GUI.

Facts that shape the design (research report "Goal driven DCA adaptation", 2026-11-13):

- **Nothing in the repo stores a goal or measures portfolio return over time.** The Notion
  dashboard tracks spend and execution; the outcome ledger stores per-fill 5 and 20 session
  returns, not portfolio TWR.
- **The ML factor is inert today.** `config/ml_sizing_adjustment.json` is `status: inactive`,
  so every ticker's factor is 1.0, and `sizing_adjustment_reader.py` is not wired into the live
  engine. Every learning component is scored on forecast error (MAE, rank-IC, Huber/quantile
  loss), never on a portfolio outcome.
- **A 20% floor every year is not attainable.** US stocks returned more than 20% in 36 of the
  100 years 1926 to 2025 and lost money in 26. 2026 forecasts for US large caps are about 5 to
  7% a year (JPMorgan 6.7%, Vanguard 4.8 to 6.8%) with CAPE near 41. An illustrative lognormal
  model gives roughly a 0.03% chance of five straight 20% years at forecast returns.
- **Raw account growth is the wrong measure.** Monthly DCA deposits alone show as large
  "growth". Time-weighted return (TWR) removes deposits; money-weighted return (MWR/IRR)
  measures the operator's dollar result.
- **Goal-seeking raises risk when behind.** The optimal policy in goals-based wealth management
  (Das et al. 2020) takes the most risk when furthest behind the goal; fund managers behind at
  mid-year raise risk (Brown, Harlow and Starks 1996).
- **The local console never writes state or config** (`CLAUDE.md`, Architecture table;
  `dca_console_config.py` module docstring). Its only POST routes are `/api/jobs` and
  `/api/jobs/cancel` (`dca_console.py`, `do_POST`).
- **No order tool exists for options or crypto** in the Robinhood connector the live sessions
  use (`docs/adr-csp-shadow-log.md`, Context). Live execution is Robinhood equity only.
- **Crypto is not a reliable equity hedge in 2026 data.** The 30-day BTC to S&P 500 correlation
  reached 0.81 in May 2026; even in windows where a long-window correlation turned negative,
  the two still fell on many of the same days. In 2022 BTC fell about 65% while the S&P 500
  fell 19%.

## Decision (proposed)

### D1. The goal is TWR CAGR over a rolling horizon, aspirational, never a floor

`"20% year-over-year minimum"` is recorded as `metric: "twr_cagr"`, `horizon_years: 5`,
`target: 0.20`, `kind: "aspirational"`. The system reports progress, the CAGR still required
over the remaining horizon, a probability of reaching the target, and an `infeasible` flag. It
never adds risk to close a gap. MWR/IRR is reported beside it; the MWR minus TWR spread is the
value added by the sizing and timing rules.

### D2. Goals are data: `config/goals.json` and `schemas/goals.schema.json`

```json
{
  "$schema": "../schemas/goals.schema.json",
  "version": 1,
  "status": "active",
  "goals": [
    {"goal_id": "growth-twr-5y", "goal_version": 1, "kind": "aspirational", "metric": "twr_cagr",
     "horizon_years": 5, "target": 0.20, "benchmark": "SP500_TR",
     "effective_from": "2026-12-01T00:00:00-05:00"},
    {"goal_id": "loss-hedge", "goal_version": 1, "kind": "loss_hedge", "mode": "shadow",
     "max_drawdown": 0.25, "hedge_budget_pct": 0.02,
     "instruments": [
       {"instrument": "protective_put", "mode": "shadow", "underlying": "QQQ", "horizon_days": 45},
       {"instrument": "crypto_allocation", "mode": "shadow", "assets": ["BTC"], "max_alloc_pct": 0.05,
        "hold_days": 10}
     ],
     "effective_from": "2026-12-01T00:00:00-05:00"}
  ]
}
```

- A new stdlib-only top-level module `dca_goals.py` validates and loads it (no pydantic:
  CLAUDE.md requires a stdlib runtime). It is added to `pyproject.toml` `py-modules`, and
  `config/goals.json` to `data-files`.
- **Fail closed.** A missing, malformed, inactive, stale or future-dated file, an unknown field,
  a NaN/inf or out-of-range number resolves to `NEUTRAL_GOALS`: no growth target, loss hedge
  `mode: "off"`. The loader never raises into a scheduled slot.
- **Bounds** (schema and loader agree): `target` in (0, 0.50]; `horizon_years` 1 to 30;
  `max_drawdown` in (0, 0.90]; `hedge_budget_pct` in [0, 0.05]; put `horizon_days` 7 to 365;
  crypto `max_alloc_pct` in (0, 0.05], `hold_days` 1 to 60, `assets` within
  `{"BTC", "ETH"}`.
- **Goals can never name a hard limit.** Spend caps, position caps, the kill switch and the
  ladder ceiling stay in code and their existing config files. The goal schema has no field that
  references them.

### D3. Hedge modes are `off` and `shadow` only; execution is locked in code

- The schema's `mode` enum, on the hedge goal and on each instrument, is exactly
  `["off", "shadow"]`. An instrument is shadowed only when both its own `mode` and the goal's
  `mode` are `shadow`. A broken instrument resolves to `off` without disabling the others.
- There is no `"active"` value to set, from the GUI or by hand.
- `dca_goals.HEDGE_EXECUTION_ENABLED = False` is a module literal. No config key, environment
  variable or console field reads or sets it. `dca_goals.hedge_execution_allowed(goals)`
  returns `HEDGE_EXECUTION_ENABLED and ...`, so it is False for every input.
- Turning any hedge on requires a separate ADR, an operator confirmation, and an order tool
  that does not exist today.

### D4. Shadow hedge log beside the CSP shadow log

A new module `dca-sizing-signals/market_data_ingest/ledger/hedge_shadow.py` follows
`csp_shadow.py`'s rules (`docs/adr-csp-shadow-log.md`): suggest-only, independent of the
sizing suggesters, no broker or network import, append-only ledger rows.

- **Puts and collars.** Each trading day it records the hedge it would buy: underlying, strike,
  expiry, bid/ask/mark, quote timestamp, and cost against `hedge_budget_pct`. Entry is costed at
  the **ask** (buying protection), exit at the **bid**.
- **Crypto.** It records the allocation it would hold for `hold_days`, priced from the last
  quote at or before the 4:00 PM ET equity decision time (crypto trades 24/7).
- **Point in time.** A quote is usable only if its own timestamp (`updated_at` for options and
  crypto) is at or before the decision time. Missing or later quotes produce no record for that
  day, never an estimate.
- **Scorecard.** `down_day_scorecard` needs at least 3 common finite days, else no card. It reports cost, protection delivered, and performance **on the days the equity
  portfolio fell**, beside the full-period correlation, so a hedge that looks uncorrelated but
  falls on the same down days is visible.
- **Data.** Shadow mode places no orders, so it may use non-Robinhood history under
  `docs/yfinance_replacement_plan.md`. Daily captures use Robinhood read-only quotes.

### D5. Console Goals page proposes changes as pull requests

- A Goals panel on the console Config page, backed by `dca_console_goals.py` and `GET /api/goals`. It shows the active goals, version history, hedge
  shadow results, and a form whose `mode` picker offers only `off` and `shadow`, under a fixed
  "Hedge execution disabled" banner.
- New route `POST /api/goals/propose` with the **same guard order** as `/api/jobs` (untrusted
  peer 403, then Basic 401, then console token 403), duplicate-key rejection, and the 1024-byte
  body limit. Body: exactly `{"goal": {...one goal entry...}}`.
- A valid proposal is written **only** to `<console-output>/goal-proposals/<goal_id>-v<N>.json`.
  Nothing under `config/` is written by any console module.
- A new allowlisted job action `propose-goal` (posted as `{"action": "propose-goal"}` to the
  existing `/api/jobs`) takes the newest proposal file and builds the new `config/goals.json` (only the edited
  entry changes, its `goal_version` and the file `version` bump), commits it on branch
  `goals/<goal_id>-v<N>` in a throwaway worktree of `origin/main` and opens a DRAFT PR titled
  `Goal <goal_id> v<N>` (`scripts/propose_goal_pr.py`). The operator merges. The next
  scheduled slot reads the merged file. No file watcher, no ad hoc run.
- This is the console's first write path. It is confined to the console output tree and is
  stated as a deliberate amendment of the "never writes state or config" rule: the console
  still never writes state or config.

### D6. Learning components adapt by scoring, not retraining

The goal is an input to evaluation and promotion, never a training loss. Phase 3 adds
goal-attainment metrics to `sizing_suggestion_compare.py` and the ledger `trials` table as an
**extra** publish gate beside MAE. Suggesters receive the goal at construction; the
`SizingSuggester` protocol does not change.

### D7. Phasing

| Phase | Ships | Live dollars |
|---|---|---|
| 0 | This ADR and the 12 tests | None |
| 1 | `config/goals.json`, schema, `dca_goals.py`, `goal_versions` ledger table | None |
| 2 | `portfolio_snapshots` and `goal_progress` ledger tables, Notion "Goal progress" section | None |
| 3 | Goal scoring of suggesters (shadow), hedge shadow log, console Goals page | None |
| 4 | Goal pressure bounded to [0.8, 1.0] through the existing `ml_factor` (separate ADR) | Can only hold or reduce |

## Tests (written first, failing by design)

| # | Test | File |
|---|---|---|
| 1 | Proposal validation rejects unknown fields, out-of-range targets, naive or past `effective_from`, bodies over 1024 bytes | `tests/test_dca_goals.py`, `tests/test_dca_console_goals.py` |
| 2 | `POST /api/goals/propose` has the `/api/jobs` guard order (403, 401, 403) and writes nothing when rejected | `tests/test_dca_console_goals.py` |
| 3 | No console module writes under `config/`; the only new write location is `<console-output>/goal-proposals/` | `tests/test_dca_console_goals.py` |
| 4 | `dca_goals.build_goals_document` output validates against the schema and differs from the current document only in the edited goal and the version bumps | `tests/test_dca_goals.py` |
| 5 | Hedge `mode: "active"` or any unknown mode is rejected; a broken hedge goal resolves to `off` | `tests/test_dca_goals.py` |
| 6 | No goal, console-goal or hedge module names a broker order tool or imports a broker, MCP or network client | `dca-sizing-signals/tests/ledger/test_hedge_shadow_no_orders.py` |
| 7 | `HEDGE_EXECUTION_ENABLED` is the literal False, nothing can set it, and `hedge_execution_allowed` is False for 2,000 seeded random goal documents | `tests/test_dca_goals.py` |
| 8 | A shadow put is priced only from option quotes stamped at or before the decision time (real fixture `rh_option_chains_2026-11-02.json`) | `dca-sizing-signals/tests/ledger/test_hedge_shadow_pit.py` |
| 9 | Crypto `mode: "active"`, `max_alloc_pct` above 0.05 and unknown assets are rejected; a broken crypto entry resolves to `off` | `tests/test_dca_goals.py` |
| 10 | No goal, console-goal or hedge module names a crypto order tool | `dca-sizing-signals/tests/ledger/test_hedge_shadow_no_orders.py` |
| 11 | A shadow crypto price is the last quote at or before 4:00 PM ET (real fixture `rh_crypto_quotes_2026-11-13.json`) | `dca-sizing-signals/tests/ledger/test_hedge_shadow_pit.py` |
| 12 | The scorecard reports hedge performance on equity down days, not only full-period correlation | `dca-sizing-signals/tests/ledger/test_hedge_shadow_scorecard.py` |

Not yet written (follow in Phase 1 and 2): the bitemporal `goal_versions` point-in-time test,
TWR/MWR golden values from real fills, and the stale-goal alert.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Source of truth: `config/goals.json` in git, or DuckDB `goal_versions` | Git file; the ledger keeps a point-in-time copy for backtests |
| 2 | May the console write a proposal file (its first write path)? | Yes, only under `<console-output>/goal-proposals/` |
| 3 | Does `propose-goal` open the PR itself (needs `gh` on the Mac Studio) or only stage the branch? | Opens a PR with `gh`; operator merges |
| 4 | May a goal change that only tightens a limit auto-merge? | No; every goal PR is merged by the operator |
| 5 | First shadow put underlying | QQQ index put (the one liquid chain in the 2026-11-02 capture) |
| 6 | Hedge budget: shared by options and crypto, or separate | One shared `hedge_budget_pct` |
| 7 | Does the hedge budget come out of the DCA budget? | No; it is tracked separately and never reduces DCA spend in shadow mode |
| 8 | Crypto assets allowed in shadow | BTC and ETH only |
| 9 | Minimum shadow period before an activation ADR may be considered | 6 months and at least 3 equity drawdowns of 5% or more |
| 10 | Snapshot source and cadence for `portfolio_snapshots` | Robinhood read-only positions at the daily close, fail-closed on a missed day |
| 11 | Goal pressure range for Phase 4 | [0.8, 1.0], calibrated by walk-forward backtest before that ADR |
| 12 | Drawdown limit breach: freeze extra goal-driven buying only, or baseline DCA too | Extra buying only; baseline DCA and the existing kill switch unchanged |

## Not in scope

- Any live options or crypto order, preview or review.
- Any change to `near_close_dca.py`, the three entry points, budgets, the ladder or the kill
  switch.
- Retraining any model on a goal-conditioned loss.
