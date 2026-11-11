# analysis/

Dated, one-off analysis folders — audits, backtests, and reliability writeups produced at a
point in time. Nothing here is read by the strategy at runtime; each folder is a snapshot, not
a maintained report, and later folders sometimes supersede earlier ones (noted below where that
happened). New analysis gets its own `YYYY-MM-DD-short-name/` folder rather than editing an old
one, so the history stays intact.

| Folder | What it covers |
| --- | --- |
| `2026-06-29-execution-audit/` | 10-weekday execution audit of the legacy single-task 3:45 PM ET live strategy (`ce16ba6b`): scheduler state, order history, and a file inventory. |
| `2026-06-29-revised-ladder/` | Backtest of a revised graded-buy ladder against 30 trading days of daily bars, plus a same-day intraday tier preview. |
| `2026-07-17-live-reliability/` | Reliability fixes applied 2026-07-17 to align the version-controlled schedule manifest with live scheduler state for the three pre-authorized live tasks. |
| `2026-07-23-live-reliability-fixes/` | Supersedes the 2026-06-29 audit's schedule/stream assumptions: records the switch from the single `ce16ba6b` task to four Robinhood-only scheduled streams plus a read-only monitor, and a same-day recovery changelog. |

| `composite-rank/` | Not a dated snapshot: a maintained, read-only daily ranking tool (weighted multi-period ROI, low-volatility tilt, rank-movement history) run by the scheduled task `daily-composite-rank`. See its `README.md` and `RUNBOOK.md`. |

None of these folders are on the live buy path — see `docs/live_wiring.md` and
`docs/run_classification.md` for the code that actually gates and classifies runs today.
`docs/reliability_spec.md` documents a related set of reliability engines, but its own
implementation-status matrix (§10) marks them "implemented, tested — engine only," not yet
wired into the live decision path; treat it as a pending-integration design, not a description
of what currently governs a run. Per the AGENTS.md secrets policy, no analysis file names the
live account number; redact it the same way if you add a new one.
