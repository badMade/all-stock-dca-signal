# Handoff: local DCA console and project memory

Saved after the 2026-09-06 implementation and memory sessions; refreshed in the
current worktree at the user's request.

## User intent and completed work

The user requested a functional local interface that brings together existing
runners, daily summaries, timing updates, Wondrous Octopus predictions and
historical periods, with a simple `from.py` test launcher. The interface is
implemented and verified. The user then asked to preserve the program map as
memory and save this handoff.

Read [MEMORY.md](MEMORY.md) for the reusable program map and
[docs/local_console.md](docs/local_console.md) for setup, commands, architecture
and limitations. `AGENTS.md` points future project sessions to these resources.

## Working-tree state

Current checkout: `/Users/geoffreyplymale/.codex/worktrees/012d/all-stock-dca`.
Branch at this handoff: `codex/fix-cost-chart`.
Current HEAD: `9eee9fe39561ebb25baa7ab6b3ce92f350ebc001`.

The console implementation, memory, and previous handoff are already tracked in
this checkout. The worktree was clean before this handoff refresh; this refresh
is an uncommitted documentation edit. No commit, push or PR was performed for
this request. Resume work here, not in the original Documents/Git checkout.

Original pre-console baseline, referenced by the historical verification below:
`40a35dbe96b7e9b2592ad4a4ceb3ec338a071b97`.

| Files | Changes |
| --- | --- |
| `from.py`, `dca_console.py` | Launcher, loopback server, fixed workflow actions, snapshots and daily summary persistence |
| `dca_console_data.py` | Daily run/fill evidence, workload proxies and configured ET timetable |
| `dca_console_octopus.py` | Stdlib artifact reader for forecasts, uncertainty bands, regime metrics and freshness |
| `dca_console_jobs.py` | Serialized jobs, streamed/redacted logs, deadlines, restart history and child cleanup |
| `console/index.html`, `console/app.js`, `console/styles.css` | Responsive dashboard, filters, charts, downloads, documentation and job controls |
| `tests/test_dca_console*.py` | HTTP, CLI, job lifecycle, daily evidence and forecast regressions |
| `README.md`, `docs/README.md`, `docs/local_console.md`, `scripts/agent_entry_points.py` | Launcher discoverability and operating guide |
| `MEMORY.md`, `AGENTS.md`, `HANDOFF.md` | Durable program map and continuation context |

No existing strategy or trading-configuration files were changed. No credentials
or generated run artifacts were added to the source changes.

## Resume and test

Run from the repository:

```bash
python3 from.py --check
python3 from.py
python3 from.py --once
python3 from.py --run demo-dca
python3 from.py --run demo-neural
python3 from.py --run test-signals
```

For existing data:

```bash
python3 from.py --local --root <cron-root> --octopus-run <research-run>
```

Core runtime is standard-library-only, Python 3.11+. The launcher detects the
repository `.venv` for jobs. Research needs NumPy/pandas/scikit-learn; the neural
Octopus action also needs PyTorch. All were detected in the local environment
during implementation. `--research-python` can select another environment.

During the original checkout's session, `http://127.0.0.1:8765/api/health` returned
`{"status":"ok","mode":"demo"}`. The process was launched with:

```bash
python3 from.py --no-browser --port 8765 --output-dir /tmp/dca-console-browser
```

Its demo artifacts and summaries are below `/tmp/dca-console-browser`. This is
a disposable test-output location, not a production path. Verify that the
process and files still exist before relying on them. That process was launched
from the original checkout; it is not evidence that this worktree is being served.
No server was started or verified during this handoff refresh. If the port is occupied,
inspect the existing console or use `--port 0`; do not blindly kill another
process. Default output otherwise follows `dca_paths.stream_root('console')`.

## Verification already completed

| Check | Result |
| --- | --- |
| `.venv/bin/python -m ruff check .` | Passed |
| Focused console and entry-point tests | 62 passed after final interface refinements |
| `.venv/bin/python -m pytest --tb=short` | Most recent full run: 7,927 passed, 6 failed, 4 skipped, 2 subtests passed |
| Core `run_dca.py --demo --report` smoke run | Passed and produced run state/reports |
| `from.py --once` | DCA + baseline Octopus demo completed |
| `from.py --run demo-neural` | Actual neural training, ten replay days and daily artifacts completed |
| `from.py --run test-signals` | Existing seven-slot candidate tests passed |
| Local-mode snapshot | Read artifacts and saved summaries without launching trading jobs |
| Browser QA | Desktop/mobile, dates, ticker/component/period filters, empty states, charts, reports, downloads, documentation and job logs passed |

Exact focused test command:

```bash
.venv/bin/python -m pytest tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py scripts/tests/test_agent_entry_points.py
```

Exact core smoke command:

```bash
.venv/bin/python run_dca.py --demo --report --now 2026-09-04T15:45 --artifacts-dir /tmp/dca-demo
```

The six failures are all parameters of
`tests/test_inverse_martingale_wiring.py::test_direct_live_corporate_action_lookup_rejects_invalid_results`
at line 1114. Invalid corporate-action results unexpectedly advance the tracker
cursor and append an event. All six were reproduced in a pristine `git archive`
of the base HEAD, with imports confirmed to come from that archive. They predate
the console; do not attribute them to this change or silently suppress them.
No fix to that strategy behavior was attempted in this task.

The memory/handoff changes are documentation only. The full-suite result above
comes from the original checkout's memory turn; this handoff records that
evidence rather than claiming another full-suite run in the new worktree.
This location-only refresh was checked with `git diff --check`; runtime tests
were not repeated because it changes no executable code.

## Boundaries and remaining setup

- The default interface is a synthetic demo, not a current market prediction
  feed. The baseline Octopus archive covers 2019–2020; dates and models are shown.
- Local mode needs the user's real artifact root and existing Octopus run.
  Those production connections were not selected or configured in this session.
- The console does not place live orders, wire broker credentials, send external
  notifications, or register external schedules. Existing broker/data/connector
  integrations retain their setup requirements. The program directory exposes
  their canonical entry points, not universal automatic execution.
- Daily summaries refresh while the launcher runs. The timetable reflects
  configuration, not proof that an external scheduler fired.
- Octopus predictions remain research data and never automatically change DCA
  sizing. Unresolved target dates and missing fill evidence remain explicit.
- Workload figures are proxy units, not measured API charges or subscription use.

There is no unfinished implementation step for the requested local interface or
memory. Resume by reading the current user request, inspecting `git status`, and
using the existing launcher/catalog. Connecting production data, enabling live
execution, changing strategy behavior, and committing/publishing are separate
follow-up actions, not implicit instructions from this handoff.
