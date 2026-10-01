# Dead and obsolete code report (2026-09-30)

Only the seven Tier A items marked **removed** below have been deleted (2026-09-30, behavior
preserving; the full suite, ruff and the demo smoke test were run afterwards). Everything else is a
lead with evidence; deleting it is a separate, reviewed change (see the "Consolidating is a
decision" note in `CLAUDE.md`).

**Method.** (1) AST import graph of core modules, `scripts/`, `all_stock_dca/`, `guardrails/`,
`selfheal/`, `viz/`. (2) `vulture --min-confidence 60` over the same set, run from a throwaway
venv (not added to `requirements-dev.txt`). (3) For each vulture hit, `git grep -w` across code,
tests, docs and prompts. (4) Full-suite coverage run, see the last section. (5) `git log` for
staleness.

**Limits.** Static analysis misses string dispatch and `importlib`. The live prompts in
`claude/dca-executor-prompts/live-mirror/` and the workflows count as callers. Tiers below reflect that.

## Tier A: defined, never referenced anywhere (strongest candidates)

Only the definition line matches `git grep -w`, no test, no doc.

| File | Symbol | Status |
|---|---|---|
| `dca_live_adapters.py` | `_placement_response_has_order_id` | removed |
| `dca_live_adapters.py:736` | `_placement_response_proves_zero_execution` | kept, needs a decision |
| `dca_market_graph.py` | `_within` | removed |
| `dca_runner.py:6599` | `_redact_malformed_symbol` | kept, needs a decision |
| `dca_runner.py:6606` | `_validate_weekly_rollover_reserve` | kept, needs a decision |
| `morning_dca.py` | `_first_not_none` | removed |
| `near_close_dca.py` | `build_alpaca_list_positions_payload` (and its now-unused `ALPACA_LIST_POSITIONS_TOOL` constant) | removed |
| `near_close_dca.py` | `_attempt_side_count` | removed |
| `scripts/export_octopus_ledger.py` | `_inspect_run_helpers` | removed |
| `scripts/scheduled_artifacts.py` | `_has_signal_csv` | removed |
| `dca_console.py:914` | parameter `explain` (100% confidence, but it mirrors the stdlib `send_error` signature) | keep |

The two `dca_runner.py` helpers are the ones to read first: names suggest a validation and a
redaction that were meant to run and do not. If so, that is a missing safeguard, not dead code.

## Tier B: whole files with no reference

| File | Lines | Evidence |
|---|---:|---|
| `all_stock_dca/merge_predictions.py` | 90 | Zero references repo-wide. Docstring points at `analysis/real-octopus-run/shard_{1..4}` |
| `all_stock_dca/train_forecaster.py` | 359 | Zero references. "Phase 5" octopus fine-tune |
| `scripts/evaluate_deferral_params.py` | 502 | Zero references, no test |
| `scripts/shard_heartbeat.py` | 133 | Zero references, no test. Babysits `run_walkforward` shards |

All four look like one-off research or long-run tooling. Move to `analysis/` or delete after you
confirm the experiments are finished. Note `all_stock_dca/backtest.py` is referenced (by
`scripts/run_phase4_backtest.py`, a 10 line wrapper with no other caller).

## Tier C: duplicated or stranded files

| Item | Finding |
|---|---|
| Root `snap.py`, `market_open_raw_data.json` | Byte-identical to `claude/_agent_scratch/` copies |
| Root `build_raw_data.py`, `run_market_open_signals.py`, `market_open_signal_results.json` | Differ from `_agent_scratch/` copies, and the root copies are the newer ones (last touched 2026-09-28 and 2026-09-24, scratch 2026-09-24), so the move was never finished and edits kept landing at the root. `repo_map.md` section 3.2 says all three "moved out of the repo root 2026-09-07" |
| `snap.py` | A hard-coded table of closing prices for one day; it is data, not code |
| `all_stock_dca/` | Not listed in `pyproject.toml` `packages` (`["scripts", "selfheal"]`) |

Suggested fix, in one commit: diff the two sets, keep the newer content in `_agent_scratch/`, delete the root copies, and either add
`all_stock_dca` to `packages` or document that it is checkout-only. The root JSON files are
not in the "Do not modify" list in `CLAUDE.md`.

## Tier D: engines with tests but no production caller

| Module | Why it is not simply dead |
|---|---|
| `scripts/run_ledger.py` (305 lines) | `docs/reliability_spec.md` marks it "Implemented, tested, engine only" |
| `scripts/scheduler_lease.py` (444) | Same. Its stated role (sole-executor guarantee) is done by Gate 4 from broker history instead |
| `scripts/run_state_machine.py` (333) | Same. Only `dca_console_alerts` imports it |
| `sizing_adjustment_reader.py` (102) | Documented as intentionally caller-less until a second suggester exists. It is in `pyproject.toml` |

Decision needed: either wire these in or mark them explicitly as reserved. The docs already say
"engine only", so this is a product decision, not cleanup.

## Tier E: public functions with only test callers (vulture 60%)

Keep unless you want to prune test-only API: `dca_config.load_backtest_sizing_tiers`,
`dca_cost_tracker.generate_cost_tracker`, `dca_market_graph.validate_payload_document`,
`dca_notion_sync.load_latest_run_summary`, `dca_reporting.generate_scheduled_reports`,
`dca_weekly_dashboard.write_weekly_dashboard`, `sizing_adjustment_reader.load_combined_adjustment`,
`near_close_dca.run_and_report`, `run_dca.run_slot` and `prior_slots` (driven by live sessions),
`scripts/alert_digest.detect_slot` / `detect_gate`. Most are the externally-driven library API
described in `CLAUDE.md`, so they are expected to look unused.

## Scripts with no test

`scripts/close_window_signal.py`, `daily_per_ticker_calibration.py`, `evaluate_deferral_params.py`,
`refresh_active_ticker_histories.py`, `refresh_market_open_candidates.py`, `run_phase4_backtest.py`,
`run_ticker_maintenance.py`, `run_tickers_daily_update.py`, `shard_heartbeat.py`, plus root
`from.py`, `snap.py`, `run_market_open_signals.py`. The two `daily_per_ticker_calibration` and
`run_ticker_maintenance` scripts are documented as live-sizing inputs, so the missing tests matter
more than for research scripts.

## Independent signal: full-suite coverage

Run: `pytest --cov=.` on Python 3.11, 2026-09-30. Total 90.8% (CI gate is 90%). The run had 3 failures, none related to
this page: `test_dca_fileio.py::test_the_destination_owner_is_preserved` and
`::test_the_write_aborts_when_ownership_cannot_be_reproduced` (they also fail with this change
stashed; they assert file-mode bits that this sandbox's root user does not reproduce), and a docs
catalog test that passed once the two new pages were indexed.

| Coverage | Files |
|---|---|
| 0% | `all_stock_dca/backtest.py`, `merge_predictions.py`, `train_forecaster.py`, `run_market_open_signals.py`, `scripts/evaluate_deferral_params.py`, `scripts/shard_heartbeat.py` |
| 36% | `scripts/export_octopus_ledger.py` |
| 54% | `guardrails/engine.py` |
| 60% | `scripts/refresh_market_open_candidates.py` |

The 0% set matches Tier B, plus `all_stock_dca/backtest.py`, which only its 10 line wrapper
`scripts/run_phase4_backtest.py` reaches. Functions with 3+ statements that the suite never
executes: 52 in total, 30 of them in those same 0% files. Of the Tier A names, coverage confirms
four were never run: `_placement_response_proves_zero_execution` (32 statements, worth a human
look since the name suggests a safety proof), `_within`, `_redact_malformed_symbol`,
`_validate_weekly_rollover_reserve`. The engines in Tier D are well covered (93 to 98%), so they
are tested, only unwired.
