# Production Readiness Review

Reviewed: 2026-07-26, on `claude/production-readiness-review-qk2093`.
Scope: the core DCA library and its operational model (scheduler tasks, artifacts, alerting, state).
Baseline at review time: 693 tests passing, `ruff check .` clean, CI green on Python 3.11/3.12, demo smoke test producing full artifacts.

## Verdict

The repository itself is in good shape *as a library*: extensive fast tests, schema'd configs, fail-closed
MCP response parsing, dependency-injected adapters, no secrets in git, and live order placement gated
behind two independent switches (`StrategyConfig.dry_run` and `RobinhoodBroker.enable_live_orders`).

The production gaps are mostly **around** the code rather than in it:

1. Live execution authority lives in scheduler task text outside version control.
2. Durable state is an unlocked, non-atomically-written JSON file tree on one machine.
3. Failure visibility is convention-driven (task text + env-gated Slack), not guaranteed by code.
4. The trading calendar is a hardcoded pilot window (extended to 2026-10-30 in
   `config/trading_window.json` on 2026-07-26; the in-code fallback caught up on 2026-09-07).

Every incident in the July 2026 log (13th, 16th, 17th, 20th, 21st — see
`config/scheduled_tasks.json`) originated in those four areas, not in the strategy math.

**Status, 2026-09-07 — diagnostic remediation pass.** A read-only sweep across correctness,
security, performance and test coverage produced 66 findings (4 High, 27 Medium, 34 Low), and
the same-day remediation branch closed the following, each with a regression test that failed
before its fix:

- Fail-closed broker adapter: a live `RobinhoodBroker` now refuses an unwired buying-power,
  tradability or order-history lookup instead of degrading to the dry-run defaults; placement
  responses must describe the requested symbol, side, ref id and size; booleans and non-finite
  strings never parse as numbers; the live gate requires the literal `True` on every path.
- Runtime classification accepts the class-based morning/midday executors' own
  `state_schema_version`, so their scheduled runs count as `STREAM_SCHEDULED`.
- `dca_runner`: the bought-today refusal follows `one_order_per_ticker_per_day` like `assess()`,
  unknown order provenance fails the Gate 4 grace path closed, the strategy-state lock is bounded
  (30 s, `TimeoutError`), history freshness is bounded before every placement, a dry run rehearses the live
  validation, and four uncalled gate helpers are gone.
- Redaction everywhere text leaves the process: broker payloads in adapter logs and exception
  text, Slack alert bodies, attempt-log markers, `raw/orders_after_place.json`.
- A durable Gate 9 alert file writer (`dca_alerts.write_alert_file`), used automatically by
  `guarded_run` for structured failures and named by the live-mirror prompts.
- One canonical definition each for the deployed-status set, the terminal and live broker state
  sets, the executed-quantity columns, and the same-slot ref-id prefix families across all three
  entry points; `config/near_close_dca.json` ships in the wheel.

Still open from that sweep, each needing its own design and acceptance run: broker-history
cap seeding for the executor and afternoon paths (the three entry points still read caps from
three sources), the per-run artifact corpus index, the `run_daily_compact` extraction, wiring
`scripts/scheduler_lease.py`, SHA-pinning third-party actions, and the git-history account
number (a repository-owner decision).

## What is already strong

- **Test/CI hygiene**: 693 tests in ~4s; ruff (`E,F,I,B`) on the core tree; CI runs lint + tests + a
  dry-run smoke (`.github/workflows/ci.yml`) on 3.11 and 3.12.
- **Safety gates in code**: two-gate live placement (`dca_live_adapters.py` module docstring);
  phantom-order and terminal-state guards in `place_market_buy`; fail-closed same-day order lookup
  (`RobinhoodBroker._same_day_orders`); exact-match idempotency `ref_id`s; one-order-per-ticker-per-day;
  spread circuit breaker that refuses to fail open (`RobinhoodDataProvider.get_bid_ask`); NaN/inf
  collapsing in `_to_float` so a poisoned number can never bypass a numeric gate.
- **Risk layer**: budget tiers, weekly rollover reserve, `max_per_order_usd`, and a 7-day deploy-fraction
  kill switch (`dca_risk.py`).
- **Config discipline**: JSON schemas for configs and artifacts; `dry_run`/`broker` deliberately not
  settable from file config (`dca_config.load_near_close_overrides`), so config alone can never enable
  live trading.
- **Secrets policy**: account number via `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` env with a sentinel default
  (`near_close_dca.py`); webhook URLs env-only; no credentials in git.

## Gap analysis

### 1. Live execution is prompt-driven, not code-driven (highest priority)

The library is driven by scheduled agent sessions (tasks `42173154` morning, `a6239dae` midday,
`10ce362c` afternoon, plus watchdog/reporting tasks) that wire Robinhood MCP callables into
`run_dca.run_pipeline` at runtime. Consequences documented in the repo itself:

- Reliability rules from the 2026-07-17 incident are "manifest metadata only, NOT YET consumed by any
  runtime code … whether that procedure is followed today depends on the scheduler/agent layer that
  invokes this codebase, which is outside this repo"
  (`config/scheduled_tasks.json` → `live_reliability_fix_2026_07_17.implementation_status`).
- The schedule service can silently truncate long task text, dropping guardrails
  (`config/scheduled_tasks.json` → `live_task_text_compactness_policy`).
- The manifest and the live scheduler are known to drift
  (`analysis/2026-07-17-live-reliability/live_task_reliability_update_2026-07-17.md`).
- Documented-but-unimplemented contracts: morning automatic bounded-retry recovery
  (`config/morning_dca.json`, flagged NOT YET IMPLEMENTED), same-`ref_id` retry, and post-run
  `get_equity_orders` reconciliation.

**What it takes**: one committed, tested runner entrypoint per stream (wiring + auth + `run_pipeline` +
alerting) that the scheduled task invokes verbatim, demoting the agent to supervision/recovery. Then
implement the manifest's reliability rules as code (bounded recovery rerun, same-`ref_id` retry, order
reconciliation).

**Status, 2026-08-26 — the Gate 5 half of this gap is closed, per slot.** The seven-slot Claude
roster's Gate 5 ("run the repo's own signal generation for this slot") is the part of this gap other
files cite as "gap #1". All seven slots now have a real, tested entry point:

- `0935ET` — `scripts/generate_market_open_candidates.py` (constructs `MarketOpenDCAExecutor` and calls
  its own `evaluate()`; trend-exempt; see `docs/market_open_signal_entry.md`).
- `1030ET` — `scripts/generate_morning_candidates.py` (drives `MorningDCAExecutor` itself in live mode,
  scoring the full configured, non-excluded universe; see `docs/morning_signal_entry.md`).
- `1130` / `1330` / `1430ET` — `scripts/generate_intraday_candidates.py --slot {1130,1330,1430}` (one
  module for all three fires: one cron, one executor class taking `slot=`, one config; see
  `docs/intraday_signal_entry.md`).
- `1230ET` — `scripts/generate_midday_candidates.py` (same, on `MiddayDCAExecutor`; trend validation
  applies and the 7-day kill switch is required of the producer; see `docs/midday_signal_entry.md`).
- `1530ET` — `scripts/generate_buy_candidates.py` (calls `near_close_dca.evaluate_source_signal`).

The first four share one core (`scripts/stream_candidates.py`), so an eighth slot would be a
`StreamSpec` plus a thin CLI module rather than a fresh build.

None of them places an order or touches a broker: all are Gate 5 only, and Gates 6/6.5/6.7/7
remain prompt-driven, which is the rest of this gap and is unchanged. A slot also does not start
trading merely because its entry point exists — the live task's prompt must name the command, and that
text lives outside this repo (see `docs/safeguards.md#live-execution-roster`).

**Two related gaps in the inputs those producers depend on:**

`MorningConfig.as_strategy_config()` builds a narrow `StrategyConfig` view that omits all three fields
`near_close_dca.kill_switch_engaged` reads, so morning/market-open/midday/intraday have always
evaluated with `deployed_last_7d_usd` pinned at 0.0 — the 7-day deployment kill switch was present in
`graded_buy_multiplier` but could never trip on those streams, unlike the 3:30 PM path.
`MorningDCAExecutor.apply_deployment_risk_guard()` now arms it explicitly, with fail-closed validation
of all three fields (a NaN compares False against every threshold and would *disable* the guard rather
than trip it). It is opt-in, so no existing caller's sizing changed. The Gate 5 producers reach the same
cap by their own route (`scripts/stream_candidates._guarded_sizing_config`); wiring the guard into the
executor streams' own live `run()` path is still open.

`dca_risk`'s report is where that deployment figure comes from, and every figure in it is a sum over the
run states it found — so a zero meant "nothing deployed" and "no history loaded" indistinguishably, and
a consumer taking the second for the first leaves the kill switch disengaged. The report now carries an
`evidence` block (`state_root_exists`, `run_state_files_found`, `run_states_loaded`,
`run_states_unreadable`, `run_states_unclassifiable`, `afternoon_states_seen`,
`afternoon_states_undated`, `unusable_states_outside_window`) so a caller can tell them
apart and fail closed. Purely additive: no existing field changed. Callers are expected to check it —
`scripts/agent_entry_points.py`'s 1230ET row states the specific conditions.

### 2. State durability and write integrity

- `StrategyState` (daily/weekly caps, `weekly_rollover_reserve_usd`) is never persisted by
  `run_pipeline`; a caller passing a fresh `StrategyState()` silently resets budget accounting
  (`docs/live_wiring.md` documents the trap). The runner must own save/load.
- Artifact writes were non-atomic across the tree (`run_state.json`, CSVs, and the cross-run
  coordination pointers `latest_morning_run.json` / `latest_scheduled_run.json`); a crash mid-write
  leaves torn JSON that readers silently degrade past. For the same-day allocation guard, "degrade"
  means a prior fill vanishes and a re-buy becomes possible.
- No file locking anywhere; `_backfill_orders_csv_usage` does an unlocked read-modify-write of
  `run_state.json`; the `raw.tmp`/`raw.bak` swap in `run_dca.py` races destructively if two runs share
  a root.
- The file tree is the database: prior-allocation and cost scans `rglob` the entire history each run;
  no retention policy; state root lives on one machine (`/home/user/workspace/cron_tracking/...`) with
  no documented backup (the restore runbook covers code/config only).

**What it takes**: atomic temp+`os.replace` writes everywhere (Phase 1);
single-writer locking or enforced serialization per state root; explicit `StrategyState` persistence;
retention/pruning; backup of the live state root; longer term, move canonical run/budget state to a
real store (SQLite is enough).

### 3. Failure visibility

- Two `except Exception` blocks with **no logging** in the same-day allocation guard
  (`near_close_dca.py`, orders-CSV parsing in `_orders_csv_fills` and
  `_orders_csv_submitted_notionals`): a corrupt/partial CSV silently reads as "no prior allocation",
  which can double-deploy capital — the same drift class as the 2026-07-16 incident — with zero log
  line. (Phase 1.)
- No top-level failure wrapper: the only in-code alert (Slack via `DCA_DRIFT_SLACK_WEBHOOK`) fires from
  inside `run_pipeline`'s raw-persistence handler; exceptions anywhere else propagate with no alert.
  All alert branches were `# pragma: no cover` — the operator-notification path was untested.
  (Phase 1.)
- Library code installs no log handler; unless the caller runs `run_dca.configure_logging()`, INFO
  lines (including "Placed buy for …") are dropped.
- Compact mode suppresses no-op notifications, so a run that never fires is indistinguishable from a
  quiet day (the 2026-07-16 and 2026-07-20 missed-fire class). A dead-man's switch is required:
  per-stream heartbeat pings whose *absence* alerts. (Phase 1.)

### 4. Calendar and time correctness

- `APPROVED_TEST_DATES` ends 2026-08-28 (`near_close_dca.py`); live trading silently hard-stops after
  that date. `MARKET_HOLIDAYS` contains a single 2026 date; `nyse_holidays()` (morning stream) is
  2026-only; half-days are not modeled (a 16:00 close is assumed even on early-close days).
- Cron schedules are UTC with manual DST re-verification notes (`config/scheduled_tasks.json`).

**What it takes**: a maintained trading calendar (`exchange_calendars` / `pandas-market-calendars`, or
a vendored table with an expiry alarm), config-driven windows with loud expiry warnings (Phase 1), and half-day close handling.

### 5. Portability and configuration

- Machine-specific defaults baked into code: `/home/user/workspace/cron_tracking/...` roots
  (`morning_dca.py`, `midday_dca.py`, `scripts/model_usage_log.py`) and opaque scheduler task IDs
  (`ce16ba6b`, `42173154`, `a6239dae`) including one embedded in a default path. `AGENTS.md` itself
  forbids hardcoded local absolute paths.
- Inconsistent config failure posture: budget caps fail closed on corrupt JSON, but
  `load_morning_config` logs and falls back to code defaults — a corrupt sizing ladder silently
  reverting to defaults on a live run should halt instead.
- Env/config resolved once at import (`ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`, `DEFAULT_CONFIG_DIR`) —
  fine for one-shot runs, wrong for long-lived processes.

### 6. Broker robustness

- No retry/backoff anywhere in the repo; Robinhood 429 throttling is an observed live failure mode
  (2026-07-17: 7 of 27 manual override orders failed). Retries must honor do-not-retry semantics and
  reuse idempotency `ref_id`s.
- Timeouts for broker/market-data calls live in whatever wires the MCP callables — invisible to this
  codebase until execution moves into a committed runner (gap 1).

### 7. Whole-share claim entitlement is spread across six hand-maintained fields

- Six overlapping fields (`restored_credit_usd`, `restored_credit_as_of_withdrawn_usd`,
  `claim_credit_remainder_usd`, `claim_credit_remainder_as_of_withdrawn_usd`,
  `claim_sizing_basis_usd`) plus a ledger-level withdrawal counter stand in for one claim's
  ownership of the shared `pending_usd` pool. Each is written at a different subset of sites and
  read by a different subset; six consecutive review rounds each found one path that failed to age
  or update one of them.
- **No over-crediting hazard is currently open.** The three findings carried out of PR #818 are
  fixed on PR #790, each with a regression test reproducing its exact sequence. What remains is a
  maintainability gap, not a live-money one: two independent aging rules over six fields is more
  machinery than one invariant needs, and the review history is evidence it is hard to hold correct
  by hand.
- The remaining work is the refactor in issue #868 — collapse the pairs into a single aged
  entitlement read through one accessor. See `docs/whole_share_accumulation.md` -> "Claim
  entitlement in a shared pool: the seam, and how each hazard is closed" for each finding, its fix,
  and the proposed shape. The regression tests pin behaviour rather than fields, so they should
  survive the refactor unchanged — which is the check that it preserved the invariant.

### 8. Engineering hygiene

- No type-checking gate (annotations are pervasive; add mypy or pyright to CI) and no coverage
  measurement.
- `strategies` extra is unpinned (torch/transformers/vectorbt); pin or split it out of the core package.
- Stale docs to reconcile: "only `10ce362c` trades" / "11 scheduled tasks" (`docs/README.md`,
  `docs/restore_runbook.md`) predate the three live streams; `$15` vs `$10` max-per-order references
  are mixed across `analysis/` and `docs/safeguards.md`.
- A real-looking account number is committed as the test default (`conftest.py`,
  `tests/test_morning_dca.py`); replace with an obviously-fake value.
- The Polygon fetch subsystem (`FETCH_*` reports; 5 of 5,502 tickers fetched; blocked on missing
  credentials since 2026-04-26) is dead weight — archive or delete.

## Roadmap

### Phase 1 — small diffs, big risk reduction *(implemented on this branch)*

- [x] Loud logging for the two silent `except Exception` swallows in the same-day allocation guard
      (`_orders_csv_fills` / `_orders_csv_submitted_notionals`): handlers narrowed to real I/O/parse
      errors, corrupt CSVs logged at exception level with a reconcile instruction, rows parsed before
      the failure kept (the conservative direction). Programming errors now propagate.
- [x] Atomic writes (temp + `os.replace`) for every JSON/CSV/markdown artifact and coordination
      pointer, via the shared `dca_fileio.py` helper.
- [x] Top-level failure wrapper (`dca_alerts.guarded_run`) on the scheduler-facing entrypoints
      (`run_pipeline`, `MorningDCAExecutor.run`, and midday via inheritance): guarantees a log
      handler, Slack-alerts any escaping exception (`DCA_ALERT_SLACK_WEBHOOK`, falling back to
      `DCA_DRIFT_SLACK_WEBHOOK`), then re-raises. Alert path covered by tests.
- [x] Dead-man's-switch heartbeats: env-gated per-stream success/fail pings
      (`DCA_HEARTBEAT_URL_<STREAM>` / `DCA_HEARTBEAT_URL`, healthchecks.io-compatible) wired into the
      same wrapper. Point each stream at a check whose *absence* of pings alerts, and the
      missed-fire failure class becomes detectable.
- [x] Approved-window expiry alarm: every scheduled run within 10 approved trading days of the window
      end logs a countdown warning, and runs after the end log an explicit hard-stopped error naming
      the end date and the config to extend.

### Phase 2 — the structural work (next)

- [ ] Committed runner entrypoint per stream (morning/midday/afternoon); scheduler task text shrinks to
      "invoke this entrypoint".
- [ ] Persist and load `StrategyState` across runs from the runner.
- [ ] Real market calendar (holidays + half-days), replacing `APPROVED_TEST_DATES`/`MARKET_HOLIDAYS`
      with config + calendar.
- [ ] Externalize `/home/user/workspace/...` roots and scheduler task IDs into config/env.
- [ ] Broker retry with backoff/jitter honoring do-not-retry, reusing idempotency `ref_id`s; post-run
      `get_equity_orders` reconciliation as code.
- [ ] Implement the documented morning bounded-retry recovery.

### Phase 3 — durability and hygiene

- [ ] Durable state store (SQLite) + retention/pruning for artifact trees + state-root backups.
- [ ] mypy/pyright and coverage gates in CI.
- [ ] Fail-closed config loading for live runs.
- [ ] Docs reconciliation; remove committed account-number-like test values; archive the Polygon fetch
      subsystem.

## A note on scale

At a $2.00 base buy and $10 per-order cap, today's blast radius is tens of dollars, and the existing
reactive-hardening loop has been a reasonable trade. The roadmap above is what changes if the system
must run unattended past 2026-08-28 — or before order sizes are ever raised, at which point Phases 1–2
stop being optional.
