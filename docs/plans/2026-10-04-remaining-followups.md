# Plan + prompt: the three remaining code/doc follow-ups (2026-10-04)

Source: the "discussed but not implemented" list from the 2026-10-04 Antigravity session.
Items 1-3 are repo changes. Items 4-5 are operator decisions with no code change (bottom).

Order: **A (docs) -> C (Octopus) -> B (GFUZ)**. A is risk-free, C is research/console-only,
B touches the live 10:30 path and should be reviewed on its own.

---

## A. Reconcile `schedule_manifest.md` / `run_classification.md` with the seven-slot roster

**Why it is not just a find-and-replace.** `docs/safeguards.md` (~L175) says the reconciliation
is separate from "which roster is actually placing orders today". Since then, `market_open` and
`intraday` were switched to `live_execution_enabled: true` (commit `9fa8bdc2`), and morning was
made explicit (`be634e88`), so descriptions of those in-repo executors as disabled are stale.
Those flags only say what a class executor does when invoked; they do not prove that an external
scheduled task invokes it or places orders. Reconcile configured executor capability from config,
and reconcile the actually deployed roster separately from verified scheduler/prompt evidence.

**Files**
- `docs/schedule_manifest.md` -- rewrite the "Stale as of 2026-07-28" banner and the three-stream
  prose (morning `42173154`, midday `a6239dae`, afternoon `10ce362c`).
- `docs/run_classification.md` -- same banner; add the per-stream pointer/root table that
  CLAUDE.md "Run classification" already states (market_open / morning / midday / intraday /
  final 15:30 under legacy `ce16ba6b`).
- `docs/safeguards.md` L175-179 -- delete the "have not yet been reconciled" paragraph once done.
- `config/scheduled_tasks.json` -- the manifest says it is the machine-readable source; check
  whether a test or schema validates it before editing, and edit only if the docs claim
  parity with it.

**Source of truth for the roster:** `config/{market_open,morning,midday,intraday}_dca.json`
(`live_execution_enabled`), `docs/schedule_manifest.md` cron IDs, `docs/safeguards.md#live-execution-roster`,
and CLAUDE.md's architecture table (09:35 `2ba19526`, 10:30 morning, 11:30/13:30/14:30 `7be82b97`,
12:30 `a6239dae`, 15:30 final).

**Constraints:** docs-only. Do not retire, rename, or re-ID any cron task. Keep dated
"added/rewritten" notes in the house style; mark superseded text as historical rather than
deleting incident evidence.

**Verify:** `ruff check .`; `pytest tests/test_config_invariants.py -q`; grep that no remaining
doc claims "five tasks are the only live execution".

---

## C. Octopus: separate expected calendar dates from forecast targets

**Current state.**
- `wondrous-octopus/daily_update.py::_write_next_day_forecasts` writes
  `daily/<date>_next_day_forecasts.csv` with `origin_date` but no target date.
- `dca_console_octopus.py::_forecast` already reads an optional `target_date` and falls back to
  "Next trading row (date unresolved)"; the warning at L336-337 fires when any row lacks one.
- Trap documented at `dca_console_octopus.py` ~L113: a checkpoint `outstanding` bucket date can be
  a deferred *scoring* date, so it is **not** a valid target date. Compute the target from the
  origin date; never read it from the bucket key.

**Design**
1. Add a pure helper `next_trading_date(origin: date) -> date` in `wondrous-octopus` (stdlib only;
   the subproject must not import repo-root modules). Skip weekends plus an NYSE holiday set. Prefer
   reading the same calendar data `dca_runner.market_day_status` uses, passed in as an optional
   path/arg; if none is supplied, fall back to next weekday and label the column value as an
   estimate (see 3). Do not guess holidays silently (`dca_runner.py` ~L211 documents why).
2. `_write_next_day_forecasts`: keep `target_date` unresolved until the ticker's next available
   data row is known. Store the calendar result separately in `expected_target_date` with
   `expected_target_date_basis` (`calendar` | `weekday_estimate`). Append these columns **after**
   the existing columns so positional readers do not break.
3. `dca_console_octopus.py`: read `target_date`, `expected_target_date`, and
   `expected_target_date_basis` in `_csv_forecasts` and `_checkpoint_forecasts` (checkpoint path
   computes only the expected date from `origin_date` with the same helper or leaves it `None`);
   show "(est.)" for an expected date with basis `weekday_estimate`, without treating it as the
   forecast target. Keep the L337 warning only for rows still lacking a known target date. Update
   `target_label`.
4. `wondrous-octopus/inspect_run.py` and the `daily_update.py` summary validator (~L109-126) must
   tolerate old CSVs without the columns (existing run directories on disk are not rewritten).
5. Update the "Forecast targets are the next available trading row" wording in
   `docs/wondrous_octopus.md` and `wondrous-octopus/README.md`.

**Tests:** `wondrous-octopus/tests/test_daily_update.py` (new columns, holiday/weekend rollover,
Friday -> Monday, old-CSV tolerance), `tests/test_dca_console_octopus.py` (label, estimate flag,
warning only when missing).

**Constraints:** research artifacts only; never wired into live sizing. `ml-forecaster` and the
console both read these files, so grep for other readers of `next_day_forecasts` before changing
the header.

**Verify:** `(cd wondrous-octopus && python -m pytest tests/)`, `ruff check --isolated --select=E,F,B --line-length=120 wondrous-octopus`,
`.venv/bin/python -m pytest tests/test_dca_console_octopus.py -q`, then load the console Octopus page
against a fresh daily CSV.

---

## B. GFUZ supplemental preflight (live 10:30 path -- review separately)

**Current behavior (`morning_dca.py::morning_preflight_eligible_tickers`, ~L3267-3365).** The gate is
NON-BLOCKING in three cases (forbidden root, missing artifact, valid-but-empty eligible list): all
degrade to the full universe, so GFUZ is evaluated there. The only case that gates GFUZ out is a
**valid, non-empty** 9:45 artifact with no GFUZ row (`admitted = [t for t in tickers if t in eligible]`).
That is the one case the spec in `config/morning_dca.json` `gfuz_special_handling.supplemental_preflight`
wants handled.

**Design (minimal, fail-closed by default)**
1. Add an optional keyword `supplemental_validator: Callable[[str], Mapping] | None = None` to the
   public `MorningDCAExecutor.run(...)` and `run_dca.run_slot(...)` entry points, plus an output
   directory derived from each entry point's existing run directory. Thread the callable through
   `_run_impl` / `evaluate` / `_apply_preflight_gate` into `morning_preflight_eligible_tickers`.
   The repo makes no network calls, so the validator is an **injected callable** like the other
   adapters in `dca_live_adapters.py` (Robinhood quote/prior-close, read-only).
2. When GFUZ is in `tickers`, the preflight artifact is valid and non-empty, and GFUZ is not in
   `eligible`: call the validator, write `gfuz_supplemental_preflight.json` into the
   `morning_dca/YYYYMMDD_1030ET/` run dir **before** any signal/budget/order decision, then admit GFUZ
   only if the persisted result says valid. No validator, validator raises, or persisted file
   unreadable -> GFUZ stays gated out (today's behavior). Never buy GFUZ on unpersisted data.
3. Record the outcome in the returned `gate` dict (`gfuz_supplemental: {...}`) so artifacts show it.
4. Use `dca_fileio` atomic writes and the existing filename already listed at `morning_dca.py` ~L3188.
5. Flip `implementation_status` text in all five places: `config/tickers.json` L40-44,
   `config/morning_dca.json` L103-108 and L212, `config/scheduled_tasks.json` ~L3506/L3536,
   `docs/morning_dca.md` ~L190-201/L541, `docs/schedule_manifest.md` ~L535-539.

**Tests (`tests/test_morning_dca.py`, plus `tests/test_run_dca.py` for the 10:30 slot):**
validator absent -> gated out (regression); validator valid -> GFUZ admitted and file persisted
before decisioning; validator invalid/raises -> gated out; GFUZ present in preflight -> validator
never called; missing/empty preflight -> full universe unchanged.

**Constraints:** CLAUDE.md says changing the live path needs its own review and acceptance run. Keep
the diff small, do not touch the other entry points, and run the dry-run smoke test:
`python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo`.

**Open question for the owner:** is GFUZ supplemental validation still wanted? The fail-closed skip
only costs a missed GFUZ buy on days its 9:45 row is absent. If not, the cheaper fix is to delete the
"must run" spec from the five places above and document the skip as intended.

---

## D / E. No code change (operator decisions)

- **D. Alert channels.** `DCA_ALERT_SLACK_WEBHOOK`, `DCA_DRIFT_SLACK_WEBHOOK`, and the shared or
  per-stream `DCA_HEARTBEAT_URL[_<STREAM>]` variables belong in the gitignored `.env` (mode 0600,
  see `docs/local_env_file.md`) or in each scheduler environment. Never commit URLs; the owner
  supplies them. After an `.env` change, restart the console and inspect `/api/alerts` (supplying
  Basic auth when configured). For scheduler-only variables, verify them in that scheduler's own
  environment; the console endpoint cannot observe them.
- **E. `self_heal.config.enabled: false`.** Disabled by design. Leave it unless the owner
  explicitly wants automatic remediation; if so it is its own decision, not part of this change.

---

# PROMPT (copy everything below into the implementing agent)

You are working in `/Users/geoffreyplymale/Documents/Git/all-stock-dca`. Read `CLAUDE.md` and
`docs/plans/2026-10-04-remaining-followups.md` first; the plan is the spec. Implement sections
**A, C, then B**, as three separate commits on a new branch (not `main`). Skip D and E.

Before editing: run `git status --short` and `git log -5 --oneline`. The owner commits to this
checkout concurrently, so re-check branch and status before each commit and stage only your files.
Leave the untracked `analysis/2026-10-04-ticker-advisory/` alone.

Rules that bind this work:
- Python stdlib only in core modules; `wondrous-octopus/` stays self-contained.
- Fail closed. Never place orders or call the network from the repo; GFUZ validation is an injected
  callable.
- Match the house comment style (why-comments, dated notes). Do not rewrite incident evidence in docs;
  mark superseded text historical.
- No account-shaped literals anywhere; no absolute paths (use `dca_paths`).
- If you add a top-level module or newly read config, update `pyproject.toml`.
- Add a regression test for every behavior change.

For each section: make the change, then run its verify commands from the plan. Before finishing, run
`ruff check .`, `.venv/bin/python -m pytest tests/test_config_invariants.py tests/test_morning_dca.py tests/test_run_dca.py tests/test_dca_console_octopus.py -q`,
`.venv/bin/python -m pytest scripts/tests -q`, the wondrous-octopus suite and ruff command, and the demo
smoke test. Report any failing test with its output; do not skip or weaken tests.

For section A, derive the live roster from `config/*_dca.json` `live_execution_enabled` and
`docs/safeguards.md`, not from the old prose. For section B, stop and ask me before implementing if
you find the supplemental validation cannot be wired without changing `run_dca.run_slot` behavior for
any other slot. Finish with a short summary: what changed, test results, and anything left undone.
