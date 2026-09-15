# PLAN — Automated self-healing with a diagnostic memory

Build plan for the `rocket-fuel/self-heal` cycle. Vision and non-negotiables live in
`rocket-fuel/self-heal/VTO.md`; deferred items in `rocket-fuel/self-heal/DEFERRED.md`.
This plan is the subject of the Same Page Meeting and is revised on the record until
`VERDICT: SAME PAGE`. **Revision 1** (as submitted).

## Starting point (facts, verified 2026-09-14 on this checkout)

- `scripts/self_heal.py` (4,067 bytes): six idempotent repair steps (`pip install -e .`,
  `ruff check --fix --extend-exclude claude .` + `ruff format --exclude claude .`,
  `pytest --snapshot-update`, pinned type-stub install, `pip-compile` when
  `requirements.in` exists, optional doc/badge regeneration). After each step it compares
  `git status --porcelain` before/after and, only when *that* step changed something,
  runs `check_health()`; `sys.exit(0)` on the first step that both changed the tree and
  passed healthcheck, else `sys.exit(1)`. It takes no arguments, reads no config, has no
  concept of a failure identity, and keeps no record of having run.
- `scripts/healthcheck.py` (2,325 bytes): `ruff check .`, `pyright`, `pytest --cov=.
  --cov-fail-under=90`, the `run_dca.py --demo --report --now 2026-08-03T15:45` smoke test
  into a `tempfile.TemporaryDirectory` outside the worktree, and `pip-audit --requirement
  requirements-dev.txt --strict`. Prints the failing command's stdout/stderr; exit 0/1.
- `.github/workflows/self-heal.yml`: triggers are `schedule` (`0 3 * * 1`),
  `workflow_dispatch`, and `workflow_run` on CI completion. The job `if:` admits only
  `schedule` on `refs/heads/main` and `workflow_run` with `conclusion == 'failure'` and
  `head_branch == 'main'`, and excludes `selfheal-` ref names. It holds `contents: write`,
  `pull-requests: write`, `actions: read`, `issues: write`; `timeout-minutes: 15`;
  `concurrency` group `selfheal-${{ github.ref }}` with `cancel-in-progress` false on main.
  A `scope_check` step narrows reactive runs to the `Lint & test (core DCA)` and
  `Dependency & secret audit` jobs and picks the matching Python version. A stale-PR
  cleanup step closes only `github-actions[bot]`-authored `selfheal-repair-*` PRs older
  than 7 days by `updatedAt`. **Note the header comment's trust-boundary argument and the
  contradicting `workflow_dispatch:` line three lines below it — see Finding A.**
- `.gitignore` excludes `*.log` (so every `stdout.log`), `cron_tracking/`, `artifacts/`,
  `deploy/*.env`, `deploy/launchd/*.plist`, `.claude/`, and `ticker_data`. `analysis/` is
  **not** ignored; `git status` currently shows `analysis/real-octopus-run/shard_{1..4}/
  checkpoint/` and `shard_1/ledger/` as untracked.
- Live crash sample, `analysis/real-octopus-run/shard_1/stdout.log` (642 bytes, 2026-09-14
  07:27): one line beginning `RUNTIME FAILURE (NotImplementedError): The operator
  'aten::_nested_tensor_from_mask_left_aligned' is not currently implemented for the MPS
  device.` followed by `The last checkpoint (if any) is retained; --resume can continue the
  run.` This is the canonical test fixture for Rock 1.
- `cron_tracking/` holds eleven stream roots (`console`, `cumulative_dca_credits`,
  `intraday_dca`, `market_open_dca`, `midday_dca`, `morning_dca`, `per_ticker_calibration`,
  `scheduled_runs`, `ticker_history_update`, `whole_share_accumulation`,
  `wondrous_octopus`), most partitioned by `YYYY-MM-DD`.
- Reusable engines already present: `scripts/file_lock.py` (`cross_process_lock`, flock
  with a portable fallback), `scripts/reason_codes.py` (module-level string constants plus
  `is_valid_reason` / `is_fail_closed`), `scripts/model_usage_log.py` (`build_entry`,
  `resolve_log_path`, `append_model_usage_entry`, `log_model_usage` — best-effort,
  never raises into its caller; schema `schemas/model_usage_log.schema.json`),
  `scripts/idempotency.py`, `dca_paths.cron_tracking_root()` / `stream_root()`,
  `scripts/workflow_policy.py` (timeout / concurrency / `persist-credentials` policy over
  `.github/workflows/*.yml`).
- Redaction precedent: `dca_console_jobs.redact()` masks env values whose **names** match
  `SECRET|TOKEN|PASSWORD|API_KEY|ACCOUNT_NUMBER`, labelled secrets, and any 6–12 digit run.
- `pyproject.toml` `[tool.setuptools] py-modules` is an explicit flat list with a comment
  trail recording each time a module was forgotten; `packages = ["scripts"]`, so new
  `scripts/*.py` ship automatically, but new top-level config needs a `data-files` entry.
- Test layout: `tests/test_<module>.py` for core modules, `scripts/tests/test_<script>.py`
  for scripts (`test_healthcheck.py` and `test_model_usage_log.py` already exist there).
  `scripts/tests` is **outside** the root pytest sweep and is run separately.

## Architecture

Three cooperating engines, one config file, one committed hand-off tree. The split follows
interview Q2: **local collector, CI healer.**

```
  LOCAL MAC (launchd, once daily)                 GITHUB ACTIONS (repair sandbox)
  ────────────────────────────────                ──────────────────────────────
  scripts/self_heal_daily.py
    1. diagnostic_collector.py  ── sweeps ──▶ raw incidents (gitignored)
    2. incident_fingerprint.py  ── reduces ─▶ stable fingerprint + redacted excerpt
    3. incident_ledger.py       ── records ─▶ novelty verdict, attempt history
    4. self_heal_governor.py    ── gates ──▶ DISPATCH or a reason code
    5. on DISPATCH: commit sanitized
       digest + ledger mirror to the
       `self-heal-diagnostics` branch,  ────push───▶  workflow_run/push trigger
       push                                          │
                                                     ▼
                                          .github/workflows/self-heal.yml
                                            a. re-run self_heal_governor.py  ← same caps,
                                               against the committed ledger     enforced twice
                                            b. scripts/self_heal.py repair steps
                                            c. scripts/healthcheck.py
                                            d. open labeled PR (never merge)
                                            e. write the outcome back to the
                                               ledger on the diagnostics branch
```

The governor runs **on both sides**. Local enforcement stops a wasted push; CI enforcement
is the one that actually binds, because a hosted run triggered by any path (including the
weekly cron) must obey the same caps.

### Why a dedicated `self-heal-diagnostics` branch, not `main`

A daily digest commit to `main` would add a commit per day to the audit history of a live
trading repository and would race the operator's own concurrent commits (a known hazard in
this checkout). A long-lived orphan branch carrying only `diagnostics/` keeps the hand-off
auditable, keeps `main`'s history clean, and makes "self-heal reacting to its own output"
structurally impossible to confuse with a real change to `main`.

## Component 1 — `scripts/incident_fingerprint.py`

Pure functions, no I/O. Two public entry points.

`normalize(text: str) -> str` strips, in order:

| Pattern | Replacement | Why |
|---|---|---|
| ISO-8601 and `HH:MM:SS` timestamps | `<TS>` | same crash, different second |
| `0x[0-9a-f]{6,}` | `<ADDR>` | object addresses |
| `/Users/<name>/…`, `$HOME` | `~` | machine-specific, and a PII leak |
| absolute paths inside the checkout | repo-relative | CI path ≠ local path |
| `pid=\d+`, `PID \d+` | `<PID>` | |
| `\b\d{6,12}\b` | `<NUM>` | account-shaped literals; see redaction |
| `shard_\d+` | `shard_<N>` | one MPS bug, four shards, one fingerprint |
| trailing whitespace, ANSI SGR escapes | removed | |

`fingerprint(kind, frames, message) -> str` returns the first 16 hex chars of
`sha256(kind + "\x00" + "\x00".join(frames[:5]) + "\x00" + normalize(message))`, where
`frames` are only the **repo-owned** traceback frames (path under the checkout, third-party
and stdlib frames dropped) each reduced to `relpath:function` — deliberately *without* the
line number, so a fix that shifts lines elsewhere in the file does not mint a new identity
for an unchanged bug.

Rationale for dropping line numbers: the alternative — including them — makes every
unrelated edit above the crash site look like a brand-new failure, which is exactly the
runaway-work condition the governor exists to prevent.

## Component 2 — `scripts/diagnostic_collector.py`

One `Source` protocol, five registered implementations, each a pure `scan(root) ->
list[Incident]`:

1. `octopus_shard` — `analysis/real-octopus-run/shard_*/stdout.log`: lines matching
   `^RUNTIME FAILURE \((?P<kind>\w+)\):` and any Python traceback block.
2. `cron_artifacts` — `$DCA_CRON_TRACKING_ROOT/**/run_state.json` in a terminal failure
   state, plus `$DCA_ATTEMPT_LOG_ROOT` failed-start markers.
3. `console_jobs` — `cron_tracking/console/**` job output with a nonzero exit.
4. `ci_runs` — `gh run list --branch main --status failure --limit 20 --json …`. Skipped
   with a recorded reason when `gh` is absent or unauthenticated; never fatal.
5. `healthcheck` — runs `scripts/healthcheck.py`, captures stdout/stderr on nonzero exit,
   attributes each failing command as its own incident.

Every `Incident` is a frozen dataclass serialized to JSON:

```json
{"fingerprint":"a1b2…","source":"octopus_shard","origin":"analysis/real-octopus-run/shard_1/stdout.log",
 "kind":"NotImplementedError","signature":"…normalized one-liner…","excerpt":"…redacted, ≤4 KiB…",
 "severity":"high","first_seen_utc":"…","last_seen_utc":"…","occurrences":3,"collector_version":1}
```

**Redaction runs on `excerpt` and `signature` before either is written anywhere**, using a
`redact()` lifted from `dca_console_jobs.py`'s rules and extended with: the literal value of
`$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` when set, every env value whose *name* matches the
existing regex, `~` substitution, and the `\b\d{6,12}\b` rule. A unit test asserts a
synthetic account number planted in a fixture never appears in any emitted artifact.

Raw, unredacted incidents are written under `$DCA_CRON_TRACKING_ROOT/self_heal/incidents/`
— already gitignored — so the operator can still read the full crash locally.

## Component 3 — `scripts/incident_ledger.py`

Append-only JSONL under `$DCA_CRON_TRACKING_ROOT/self_heal/ledger/incidents.jsonl`, with a
sanitized mirror at `diagnostics/ledger.jsonl` on the diagnostics branch. Every append takes
`file_lock.cross_process_lock`. A compacted `diagnostics/index.json` holds current state per
fingerprint so a reader need not replay the whole log.

Per-fingerprint state machine:

```
   new ──dispatch──▶ dispatched ──PR merged & healthcheck green──▶ repaired
    │                    │
    │                    ├──repair produced no diff, or CI still red──▶ refused ──▶ (back to new, attempts+1)
    │                    │
    └────────────────────┴──attempts > max_attempts_per_fingerprint──▶ quarantined (terminal)
```

`quarantined` is terminal and is **never** dispatched again. It is surfaced in the digest
and in the daily report so a human sees it. Clearing it is an explicit operator action
(`--unquarantine <fingerprint>`).

Novelty verdict: a fingerprint is dispatch-eligible iff state ∈ {`new`, `refused`} **and**
`attempts < max_attempts_per_fingerprint`.

## Component 4 — `scripts/self_heal_governor.py` (the limits)

`evaluate(now, ledger, config, spend) -> Decision`. `Decision` is
`(dispatch: bool, fingerprint: str | None, reason: str)`, where `reason` is a constant from
a new `scripts/self_heal_reasons.py` following the `scripts/reason_codes.py` house pattern.
Checks run in this fixed order and **short-circuit on the first refusal**:

| # | Gate | Config key | Default | Refusal reason |
|---|---|---|---|---|
| 0 | Kill switch | `enabled` / `$DCA_SELF_HEAL_DISABLED` | `true` / unset | `DISABLED` |
| 1 | Self-origin drop | — | — | `SELF_ORIGIN_DROPPED` |
| 2 | Daily rate limit | `max_runs_per_day` | **1** | `RATE_LIMITED_DAILY` |
| 3 | Circuit breaker | `consecutive_failure_threshold`, `breaker_cooldown_hours` | **3**, **72** | `BREAKER_OPEN` |
| 4 | Per-fingerprint attempts | `max_attempts_per_fingerprint` | **3** | `FINGERPRINT_QUARANTINED` |
| 5 | Token/USD budget | `daily_token_budget`, `daily_usd_budget`, `monthly_usd_budget` | **200000**, **2.00**, **20.00** | `BUDGET_EXHAUSTED_TOKENS` / `_USD` |
| 6 | Actions minutes | `monthly_actions_minutes` | **300** | `BUDGET_EXHAUSTED_MINUTES` |
| 7 | Wall clock | `max_wall_clock_seconds` | **1800** | `WALL_CLOCK_EXCEEDED` |
| 8 | Novelty | — | — | `NO_NOVEL_INCIDENTS` |

Every evaluation — dispatch or refusal — is appended to the ledger with its reason, so the
answer to "why did nothing happen yesterday?" is always in the record.

**Loop prevention, stated explicitly.** Four independent mechanisms, any one of which alone
halts a runaway:

- Gate 2 caps *dispatches per day* at 1. Even a pathological collector that mints a new
  fingerprint every minute gets one dispatch.
- Gate 4 caps *attempts per failure identity* at 3 for that identity's entire lifetime, then
  quarantines it permanently.
- Gate 3 trips a breaker after 3 consecutive unsuccessful dispatches regardless of
  fingerprint, and holds it open for 72 hours or until an explicit human reset.
- Gate 1 drops any incident whose origin path is under `diagnostics/`, whose branch matches
  `selfheal-*`, or whose signature names a self-heal module — so the loop cannot observe
  and react to its own exhaust. (The workflow's existing `!startsWith(github.ref_name,
  'selfheal-')` guard covers the ref; this covers the *content*.)

Fail-closed: an unparseable `config/self_heal.json`, an unreadable ledger, a clock that has
moved backwards relative to the last ledger entry, or a governor exception all produce
`dispatch=False` with an explicit reason. There is no code path where an error yields
permission.

## Component 5 — `scripts/self_heal_spend.py`

Append-only `$DCA_CRON_TRACKING_ROOT/self_heal/spend/YYYY-MM.jsonl`, mirrored sanitized to
`diagnostics/spend/YYYY-MM.jsonl`, schema `schemas/self_heal_spend.schema.json`. One entry
per governor evaluation that reached dispatch:

`{run_id, fingerprint, started_utc, ended_utc, wall_clock_seconds, tokens_in, tokens_out,
usd_estimate, actions_minutes, outcome, reason}`

`usd_estimate` is computed from a `config/self_heal.json` price table so it stays honest
when rates change; when a dispatch consumed no model call (the deterministic repair path,
which is the default) the token fields are `0` and the entry still records wall clock and
Actions minutes. Budget gates read this file; they never trust an in-memory counter.

## Component 6 — `config/self_heal.json` + `schemas/self_heal.schema.json`

Every number in the governor table above lives here; raising the daily cap later is a
one-line config edit, exactly as the operator asked. Schema validation is added to
`tests/test_config_invariants.py`, the existing gate. `pyproject.toml` gains the config
under `[tool.setuptools.data-files]` (hard invariant — a missed entry silently falls back to
defaults in an installed wheel).

## Component 7 — wiring

- `scripts/self_heal_daily.py`: the local orchestrator. Flags `--dry-run` (print the
  decision, touch nothing), `--now` (deterministic testing), `--report`.
- `deploy/launchd/render_self_heal_agent.py`: renders `com.all-stock-dca.self-heal.plist`
  (label, once-daily `StartCalendarInterval`, `.venv/bin/python`, no secrets embedded),
  modeled directly on the existing `render_console_agent.py` including its `sys.path.insert(1, …)`
  precedence comment. The rendered plist is gitignored by the existing `deploy/launchd/*.plist`
  rule.
- `.github/workflows/self-heal.yml`: add a `push` trigger on the `self-heal-diagnostics`
  branch; insert a governor step as the **first** step after checkout, whose refusal exits
  the job cleanly (not as a failure — a refused run is a correct outcome, and a red X here
  would itself trigger the reactive path); add an outcome-writeback step. `timeout-minutes`
  stays and is cross-checked against `max_wall_clock_seconds`.
  `python scripts/workflow_policy.py` must stay green.

## Rocks

Seven, at the cap. Each carries a proof command a human runs; per Audit rules every proof
also re-runs the existing suite so nothing regresses.

**Rock 1 — Fingerprint + redaction.** `scripts/incident_fingerprint.py`,
`scripts/self_heal_redact.py`.
*Non-goals:* no collection, no I/O, no ledger.
*Proof:* `.venv/bin/python -m pytest scripts/tests/test_incident_fingerprint.py scripts/tests/test_self_heal_redact.py -q` — including a golden test that the real shard-1 MPS line fingerprints identically when its timestamp, home path and `shard_N` differ, and a test that a planted 9-digit account number never survives redaction — plus `ruff check .`.

**Rock 2 — Collector.** `scripts/diagnostic_collector.py` with all five sources.
*Non-goals:* no ledger, no governor, no git operations.
*Proof:* `.venv/bin/python -m pytest scripts/tests/test_diagnostic_collector.py -q` and `.venv/bin/python -m scripts.diagnostic_collector --dry-run --json` over a fixture tree, plus `ruff check .`.

**Rock 3 — Ledger + novelty.** `scripts/incident_ledger.py`.
*Non-goals:* no governor limits, no dispatch.
*Proof:* `.venv/bin/python -m pytest scripts/tests/test_incident_ledger.py -q` — asserting a re-collected identical incident is not novel, a changed one is, the state machine's terminal `quarantined` is never re-dispatched, and concurrent appends under `cross_process_lock` do not interleave — plus `ruff check .`.

**Rock 4 — Governor + config + schema.** `scripts/self_heal_governor.py`,
`scripts/self_heal_reasons.py`, `config/self_heal.json`, `schemas/self_heal.schema.json`.
*Non-goals:* no spend ledger yet (gates 5–6 read an injected spend summary), no wiring.
*Proof:* `.venv/bin/python -m pytest scripts/tests/test_self_heal_governor.py -q` with one test per gate 0–8 in isolation and one fail-closed test per corruption mode, **and** `.venv/bin/python -m pytest tests/test_config_invariants.py -q`, plus `ruff check .`.

**Rock 5 — Spend ledger.** `scripts/self_heal_spend.py`, `schemas/self_heal_spend.schema.json`.
*Non-goals:* no live cost API; the price table is config.
*Proof:* `.venv/bin/python -m pytest scripts/tests/test_self_heal_spend.py -q` including month-rollover reset and a budget-exhausted refusal writing no dispatch, plus `ruff check .`.

**Rock 6 — Local orchestrator + launchd agent.** `scripts/self_heal_daily.py`,
`deploy/launchd/render_self_heal_agent.py`.
*Non-goals:* does not modify the workflow.
*Proof:* `.venv/bin/python scripts/self_heal_daily.py --dry-run --now 2026-09-14T04:00` prints a deterministic decision and exits 0 having written nothing; `.venv/bin/python deploy/launchd/render_self_heal_agent.py --print` emits a valid plist containing no secret; `.venv/bin/python -m pytest scripts/tests/test_self_heal_daily.py -q`.

**Rock 7 — CI wiring, docs, packaging.** `.github/workflows/self-heal.yml`,
`docs/self_healing.md`, `pyproject.toml`, a CLAUDE.md documentation-index row.
*Non-goals:* no change to `scripts/self_heal.py`'s repair steps themselves.
*Proof:* `.venv/bin/python scripts/workflow_policy.py`; `.venv/bin/python -m pytest scripts/tests/test_self_heal_workflow.py tests/test_config_invariants.py -q`; `ruff check .`; and the full suite `.venv/bin/python -m pytest` plus `python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo` green.

## Known open questions the Integrator should attack

Listed here rather than hidden, so the Same Page Meeting starts from an honest position:

- **Finding A (self-identified).** `self-heal.yml`'s header comment argues at length that
  `workflow_dispatch` is deliberately absent as a trust boundary, and the trigger block
  three lines below contains `workflow_dispatch:`. One of the two is wrong. This plan does
  not currently resolve it.
- Whether a `push`-triggered job on a non-default branch can hold `contents: write` without
  reopening the boundary the header comment defends.
- Whether the collector's `healthcheck` source double-counts what the `ci_runs` source
  already reports.
- Whether committing a ledger mirror to a branch creates a merge/conflict path when two
  daily runs race, and whether the local push should be `--force-with-lease`.
- Whether `max_attempts_per_fingerprint = 3` combined with `max_runs_per_day = 1` means a
  single stubborn failure blocks all other incidents for three days (head-of-line blocking).
