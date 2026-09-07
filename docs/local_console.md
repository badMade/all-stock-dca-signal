# Local DCA console

Launch from the repository with Python 3.11 or later:

```bash
python3 from.py
```

This opens `http://127.0.0.1:8765`, starts an isolated DCA demo and an Octopus
baseline replay, and displays progress while they run. The DCA test uses the
existing `run_dca.py --demo --report` entry point. The Octopus test uses its
existing synthetic 2019–2020 archive, followed by the daily-update entry point.
Forecasts on this first screen are synthetic research examples, with their real
artifact dates shown. They are not current market predictions.

The core interface has no third-party runtime dependencies. If the repository
`.venv` exists, jobs use its Python automatically. Otherwise they use the
launcher's interpreter. Octopus needs its separate dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -r wondrous-octopus/requirements.txt
python3 from.py --check
```

Installation is manual; the console does not install packages. You can instead
point `--research-python` at an existing Python with NumPy, pandas and
scikit-learn. The neural Octopus smoke test also needs PyTorch. Missing research
dependencies leave the core interface usable, disable the individual research
buttons, and mark the combined demo as incomplete in its job log.

## Everyday interface

- Daily summaries: select a date, inspect observed runs, statuses, candidate
  counts and submitted-order counts, and download the summary as JSON.
- Confirmed spend: comes from the existing purchase-history reader. Requested
  amounts and simulated fills do not count as confirmed live spending. Missing
  price or fill evidence displays as unavailable, not a made-up zero.
- Schedule: Eastern Time clock, configured task times and countdowns, with
  market holidays, early closes, disabled tasks and registration gaps visible.
  Configuration describes intent; it does not establish that an external
  scheduler fired or that an order executed.
- Forecasts: adjusted-dollar open/high/low/close predictions and p10/p90 bands,
  ticker filtering and source dates. A pending target without a known date is
  labeled as the next trading row. The latest research run is independent of
  the selected historical DCA date.
- Historical periods: measured model errors, coverage and skill versus
  persistence where recorded. Only periods actually present in the report have
  results; the synthetic demo does not cover all historical crises.
- Jobs: real command output, start and finish times, failures and retained
  history. One job runs at a time, with bounded logs and execution deadlines.
- Program directory: the repository's canonical entry-point catalog plus the
  adjacent research projects. Commands requiring market-data exports, an archive,
  broker adapters, or optional services retain those setup requirements.

The browser refreshes about every 15 seconds. While the launcher is running,
its background reader also saves the current day's digest without requiring an
open browser tab. The daily JSON and Markdown summaries live in
`<console-output>/summaries/<demo-or-local>/<YYYY-MM-DD>.*`. Selecting an older
day regenerates that day's summary from the artifacts currently available.
These are evolving summaries, not immutable historical snapshots. Stopping the
launcher stops refreshes; it does not install an OS or Codex scheduled task.

## Connect existing data

```bash
python3 from.py --local
python3 from.py --local --root /path/to/cron_tracking --octopus-run /path/to/octopus/run
```

The default artifact root follows `dca_paths.cron_tracking_root()` and
`DCA_CRON_TRACKING_ROOT`. Console output normally uses
`dca_paths.stream_root('console')`; `--output-dir` overrides it. Demo output
always goes below `<console-output>/demo` and is excluded from the local daily
view. `--root` can select a mounted or synchronized copy of external run
artifacts. Streams relocated outside that root must be inspected through their
own root; this interface does not silently discover or combine arbitrary paths.

Local mode observes existing output and opens without launching a demo. The
**Update Octopus day** button invokes `daily_update.py` on the supplied run,
using its recorded archive. Refresh that archive through the existing ticker
maintenance workflow first. An initial model run must already exist:

```bash
.venv/bin/python wondrous-octopus/run_walkforward.py --preset quick --dry-run
.venv/bin/python wondrous-octopus/run_walkforward.py --preset quick --run-dir /path/to/octopus/run
```

The estimate is read-only. The second command starts real research training
and may take substantial time depending on the archive and hardware. It is a
separate deliberate command, not part of console startup.

The console does not schedule or submit live brokerage orders. The existing
seven-slot executor still needs broker/data callables wired through
`dca_runner` as documented in [live_wiring.md](live_wiring.md). The seven-slot
test action exercises the existing candidate-generator regression suites.
Notion, alerts, remote archive refreshes, and other external integrations keep
their existing connector and authorization requirements. Octopus predictions
remain research artifacts and never automatically change live sizing.

## Simple entry points for testing

```bash
# Browser demo; choose an available port automatically.
python3 from.py --port 0

# Full DCA + Octopus demo, summaries and JSON result, then exit.
python3 from.py --once --output-dir /tmp/dca-console-demo

# Just the DCA demo (works with Python's standard library).
python3 from.py --run demo-dca --output-dir /tmp/dca-console-demo

# Actual PyTorch Octopus smoke test: ten replay days, with a time budget.
python3 from.py --run demo-neural --output-dir /tmp/dca-console-demo

# Test all seven signal-generation entry points using existing tests.
python3 from.py --run test-signals

# Save a summary of real local artifacts without starting any execution job.
python3 from.py --local --once --date 2026-09-04 --root /path/to/cron_tracking

# Export consolidated confirmed purchases and save the daily digest.
python3 from.py --local --run reports --root /path/to/cron_tracking

# Inspect readiness, or start without opening a browser.
python3 from.py --check
python3 from.py --no-browser
```

An explicit demo `--date` must pass the existing strategy date/window gates.
The launcher defaults to a recent full market session and rejects a nominally
successful DCA demo if no run-state artifact was produced. Single-action and
`--once` commands exit nonzero when requested work fails or is incomplete.
Ctrl+C stops the server and requests termination of its active child process.
Each demo invocation gets a new directory; previous runs are preserved.

## Local API

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/health` | Service status and mode |
| GET | `/api/bootstrap` | Session token and default date |
| GET | `/api/snapshot?date=YYYY-MM-DD&ticker=AAPL&period=all` | Daily, schedule, forecast, job and module views |
| GET | `/api/export?date=YYYY-MM-DD` | Downloadable daily/forecast JSON |
| GET | `/api/docs?path=docs/live_wiring.md` | Allowlisted local documentation |
| POST | `/api/jobs` | Start a fixed action with `{"action":"demo-dca"}` and `X-Console-Token` |

The server binds only to loopback, checks Host and Origin, and requires a
session token for job submission. It serves fixed assets and documented APIs;
requests cannot provide shell commands, arbitrary file paths, or broker actions.
Do not expose it through a public reverse proxy. This launcher is intended for
the source checkout; adjacent research projects are not bundled into the core wheel.

Implementation: `from.py` → `dca_console.py`, with separate daily-data,
Octopus-artifact, and job modules plus browser assets under `console/`.

## Verification for this change

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest tests/test_dca_console.py tests/test_dca_console_jobs.py tests/test_dca_console_data.py tests/test_dca_console_octopus.py scripts/tests/test_agent_entry_points.py
.venv/bin/python -m pytest
.venv/bin/python run_dca.py --demo --report --now 2026-09-04T15:45 --artifacts-dir /tmp/dca-demo
```

Lint, the 62 focused tests, and the core smoke run pass. The full suite produced
7,917 passes, four skips, and six failures in
`test_inverse_martingale_wiring.py::test_direct_live_corporate_action_lookup_rejects_invalid_results`.
All six failures also reproduce in a pristine archive of pre-change HEAD
`40a35dbe96b7e9b2592ad4a4ceb3ec338a071b97`; the console change does not modify that
strategy code. Later focused tests include the final console refinements.

The launcher was also exercised with `--once`, `--run demo-neural`, and a
local-artifact snapshot. Desktop and narrow-mobile browser checks covered date
selection, ticker/component/period filtering, empty states, reports, downloads,
documentation, and job logs. No credentials or generated run artifacts were
added to the source changes.
