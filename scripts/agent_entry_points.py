#!/usr/bin/env python3
"""Index of existing entry points, for agents deciding whether to reuse or reimplement.

WHAT THIS IS FOR
----------------
An agent session working on this repo -- especially a fresh local session with
none of it already in context -- routinely needs to do something the repo
already does: dry-run the pipeline, size a same-day top-up, classify a run,
check for drift. Re-deriving that logic by reading ``near_close_dca.py``
(~8.7k lines) or ``dca_runner.py``, or writing a throwaway script around it,
burns far more tokens than just running the existing command and costs
correctness too -- see AGENTS.md's "reuse existing entry points" note and the
Gate 5 line in ``claude/dca-executor-prompts/live-mirror/*.txt``: "Use the
repo entry point, never a reimplementation."

Run this before writing new code for an operational or exploratory task:

    python scripts/agent_entry_points.py
    python scripts/agent_entry_points.py --json
    python scripts/agent_entry_points.py --search budget

This module is a read-only index. It never calls Robinhood, never places
orders, and does not duplicate any gate, sizing, or signal logic itself --
it only points at where that logic already lives. ``scripts/tests/
test_agent_entry_points.py`` keeps every entry below honest: each referenced
path and symbol must actually exist, or the test fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence


@dataclass(frozen=True)
class EntryPoint:
    """One existing thing an agent can invoke instead of writing new code."""

    need: str
    command: str
    exists_path: str
    doc_ref: str
    symbols: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


ENTRY_POINTS: tuple[EntryPoint, ...] = (
    EntryPoint(
        need="Launch the local DCA interface with daily summaries, timing, and Octopus research",
        command="python3 from.py [--local --root <cron-root> --octopus-run <run-dir>] [--once] [--check]",
        exists_path="from.py",
        doc_ref="docs/local_console.md",
        notes=(
            "Default launches isolated DCA and Octopus demos plus a loopback browser UI. "
            "--local observes existing artifacts; --once emits a summary and exits. "
            "No live orders or automatic external scheduler registration. "
            "--run test-signals exercises all seven existing candidate entry points."
        ),
    ),
    EntryPoint(
        need="Dry-run the full DCA pipeline for one slot, no broker/network calls",
        command="python run_dca.py --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir <dir>",
        exists_path="run_dca.py",
        doc_ref="docs/live_wiring.md",
        symbols=("main",),
        notes=(
            "The standard pre-handoff smoke test (see AGENTS.md). "
            "Use an approved weekday timestamp (Mon–Fri, 15:45 ET recommended) — a weekend "
            "or out-of-window date hits the date gate, skips report generation, and exits 0 "
            "without exercising the full pipeline."
        ),
    ),
    EntryPoint(
        need="Size a same-day top-up buy from broker order history",
        command=(
            "python3 -m scripts.same_day_top_up_gate "
            "--trading-day <YYYY-MM-DD> --orders-pages <file> --candidates <file>"
        ),
        exists_path="scripts/same_day_top_up_gate.py",
        doc_ref="claude/dca-executor-prompts/live-mirror/1030ET-1030-dca-recurring-buy.txt",
        symbols=("main",),
        notes="The printed JSON's buy_usd per ticker is authoritative. Never recompute it by hand.",
    ),
    EntryPoint(
        need="Validate an operator's ad hoc buy list when the scheduled path is unavailable",
        command=(
            "python3 -m scripts.adhoc_orders --orders <csv|json> --trading-day <YYYY-MM-DD> "
            "--spent-today <usd> --spent-week <usd> --attempts-today <n> --buying-power <usd>"
        ),
        exists_path="scripts/adhoc_orders.py",
        doc_ref="docs/adhoc_orders.md",
        symbols=("main", "build_plan", "place_adhoc"),
        notes=(
            "Dry-run only: prints an order sheet and writes a manual-run plan, never places an order "
            "or updates a pointer. Exit 2 means place nothing. The four figures are required and must come "
            "from a fresh, fully paginated broker pull. Placement is place_adhoc() with an injected "
            "callable and confirm=True, then write_execution_state(), from an approved agent session."
        ),
    ),
    EntryPoint(
        need="Write the slot's order plan file before Gate 7 places anything (plan-first backup executor)",
        command=(
            "python3 -m scripts.emit_order_plan --trading-day <YYYY-MM-DD> --slot <slot_id> "
            "--candidates <resolved buy list file> [--out <plan file>]"
        ),
        exists_path="scripts/emit_order_plan.py",
        doc_ref="docs/adr-plan-first-backup-executor.md",
        symbols=("main",),
        notes=(
            "Emit only: places nothing and never resizes buy_usd. A FAILED result (exit 2) writes no "
            "file and must not block placement. A plan is input for the backup executor, never "
            "evidence of a purchase."
        ),
    ),
    EntryPoint(
        need="Read, compute, or finalize the backup executor's order plan (plan-first backup)",
        command=(
            "python3 -m scripts.backup_planner {plan|compute|finalize} --trading-day <YYYY-MM-DD> "
            "--slot <slot_id> ..."
        ),
        exists_path="scripts/backup_planner.py",
        doc_ref="docs/adr-plan-first-backup-executor.md",
        symbols=("main",),
        notes=(
            "Prints one JSON document; exit 2 means place nothing. Always the Normal budget tier. "
            "Never places an order and never prints the account number."
        ),
    ),
    EntryPoint(
        need="Assemble saved oversized get_equity_orders pages into an orders_pages envelope",
        command=(
            "python3 -m scripts.assemble_order_pages --trading-day <YYYY-MM-DD> "
            "--out <orders_pages file> [--snapshot-at <ISO>] [--expect-count <N>] "
            "[--expect-total-usd <X>] <page file | -> [<page file> ...]"
        ),
        exists_path="scripts/assemble_order_pages.py",
        doc_ref="docs/repo_map.md",
        symbols=("assemble", "main"),
        notes=(
            "The one validated way to get get_equity_orders output into the top-up gate: refuses "
            "placeholder order ids, condensed captures and non-finite amounts, and records an "
            "ingest block (provenance, hashes). Never re-key rows by hand; any non-zero exit is "
            "incomplete or unvalidated history."
        ),
    ),
    EntryPoint(
        need="Ingest raw Robinhood bars and quotes into a Gate 5 market-data payload",
        command=(
            "python3 -m scripts.ingest_market_data --trading-day <YYYY-MM-DD> "
            "--window-start-et <HH:MM> --window-end-et <HH:MM> --expected-tickers <A,B,C> "
            "--bars-5m <file | -> --bars-daily <file | -> --quotes <file | -> --out <market_data file>"
        ),
        exists_path="scripts/ingest_market_data.py",
        doc_ref="docs/repo_map.md",
        symbols=("ingest", "main"),
        notes=(
            "Takes the raw get_equity_historicals / get_equity_quotes output (repeat a flag per "
            "chunked response) instead of a hand-typed payload. A bad ticker is named in "
            "ticker_errors, never trimmed; a stdin input is recorded as the weaker stdin_paste."
        ),
    ),
    EntryPoint(
        need="Load validated logged-reference/order bindings from prior-allocation artifacts",
        command=(
            "python3 -m scripts.prior_reference_bindings --trading-day <YYYY-MM-DD> "
            "--root <resolved-prior-root> [--root <resolved-prior-root> ...]"
        ),
        exists_path="scripts/prior_reference_bindings.py",
        doc_ref="docs/market_open_signal_entry.md",
        symbols=("load_reference_bindings", "main"),
        notes=(
            "Read-only and fail-closed on missing roots or unreadable artifacts. The returned "
            "order_id must still be matched to the latest order in fully paginated broker history."
        ),
    ),
    EntryPoint(
        need="Decide whether a run is scheduled or manual, and where its artifacts belong",
        command="python -c 'import scripts.run_classification'  # library API, no standalone CLI",
        exists_path="scripts/run_classification.py",
        doc_ref="docs/run_classification.md",
        symbols=("build_scheduled_classification",),
        notes="Also see build_manual_classification and validate_recovery_classification.",
    ),
    EntryPoint(
        need="Check a completed run for drift against expected behavior",
        command="python scripts/drift_detection.py --run-dir <run_dir> [--slack]",
        exists_path="scripts/drift_detection.py",
        doc_ref="docs/drift_detection.md",
        symbols=("main",),
        notes="--run-dir is required.",
    ),
    EntryPoint(
        need="Find out which gates keep aborting the executor slots, and whether their alerts were delivered",
        command=(
            "python -m scripts.alert_digest --drafts <gmail_export.json> "
            "[--messages <sent_export.json>] "
            "[--alerts-dir \"$(python3 -c 'import dca_paths; print(dca_paths.cron_tracking_root())')\"] "
            "[--since YYYY-MM-DD] [--ledger] [--json]"
        ),
        exists_path="scripts/alert_digest.py",
        doc_ref="docs/alert_digest.md",
        symbols=("main", "build_digest", "load_gmail_drafts", "load_alert_files"),
        notes=(
            "Never calls Gmail itself -- the session holding the connector exports to JSON "
            "and pipes it in. Once the corrected Gate 9 prompts are deployed, alerts are "
            "SENT, so pass --messages (a sent-mail export) as well as or instead of "
            "--drafts; a drafts-only digest cannot see them. Point --alerts-dir at the "
            "cron-tracking root, not one stream: the scan recurses into every alerts/ "
            "directory and the prompts write to six roots. Resolve that root through "
            "dca_paths, never a literal path. --fail-on-warnings exits 1 when an alert "
            "claims a channel that is off or a path .gitignore discards."
        ),
    ),
    EntryPoint(
        need="Check whether every live slot left verifiable scheduled evidence today",
        command="python -m scripts.verify_schedule [--date YYYY-MM-DD] [--root <cron-tracking root>] [--json]",
        exists_path="scripts/verify_schedule.py",
        doc_ref="docs/schedule_verification.md",
        symbols=("main", "verify_day", "classify_slot_evidence", "SLOT_EVIDENCE_LAYOUT"),
        notes=(
            "Read-only. Reads each slot's OWN dated run_state.json, not the stream pointer "
            "(intraday's one pointer covers three slots). Exits 1 when any due slot is "
            "evidence_incomplete or no_evidence; the console Schedule page shows the same verdict."
        ),
    ),
    EntryPoint(
        need="Summarize a trading day's slot outcomes (aborts, caps, errors, gate9 notes) for the triage routine, redacted",
        command="python -m scripts.triage_log_summary [--date YYYY-MM-DD] [--root <cron-tracking root>] [--json]",
        exists_path="scripts/triage_log_summary.py",
        doc_ref="docs/triage_routine.md",
        symbols=("main", "summarize_day", "summarize_slot", "redact", "latest_trading_day"),
        notes=(
            "Read-only. Joins verify_schedule with each slot's own run_state status, error and "
            "gate9 notes, which verify_schedule's ok does not judge (2026-10-30: ok true while "
            "9:35 and 3:30 aborted). Defaults to the latest trading day on/before today ET. "
            "Strips the account number and account-shaped digits; emits relative paths only. "
            "Exits 1 when a slot needs attention, 2 with LOGS UNAVAILABLE when the root is unreadable."
        ),
    ),
    EntryPoint(
        need="Check after a slot that no order was platform-refused and every eligible buy was submitted or accounted for",
        command="python -m scripts.check_slot_fills --slot <HHMM> --date YYYY-MM-DD [--root <cron-tracking root>] [--json]",
        exists_path="scripts/check_slot_fills.py",
        doc_ref="docs/schedule_verification.md",
        symbols=("main", "evaluate"),
        notes=(
            "Read-only; never touches a broker. Exits 1 when any report in the slot's run directory "
            "contains PLATFORM REFUSAL, or counts.submitted != gate9_report.eligible_count minus "
            "gate9_report.dropped (price_recovered, spread_over_1pct, daily_cap), or run_state.json "
            "is missing/malformed (fails closed). Closes the gap verify_schedule leaves open: it proves "
            "a slot fired, not that orders were sent (2026-10-07 09:35: 4 of 20 submitted). Counts submissions, including pending/unconfirmed rows, not confirmed fills."
        ),
    ),
    EntryPoint(
        need="Check whether the backup_planner.pyz pasted into Perplexity is stale against origin/main",
        command=(
            "python3 -m scripts.planner_stamp_check [--planner backup_planner.pyz] [--ref origin/main] [--notify]"
        ),
        exists_path="scripts/planner_stamp_check.py",
        doc_ref="docs/schedule_verification.md",
        symbols=("main", "pasted_stamp", "fresh_stamp", "export_ref"),
        notes=(
            "Compares the content_sha256 stored inside the operator's own planner with a planner built from "
            "origin/main (git fetch, then git archive into a temp dir; the working tree is never touched). "
            "Exit 0 unchanged, 1 stale, 2 could not check; --notify raises a macOS notification on 1 and 2. "
            "Runs on the Mac because a cloud routine has no repo credentials; a launchd template ships beside "
            "the schedule watchdog's."
        ),
    ),
    EntryPoint(
        need="Check Claude Desktop is running before a live slot",
        command="python -m scripts.claude_liveness [--notify] [--now ISO] [--wait SECONDS]",
        exists_path="scripts/claude_liveness.py",
        doc_ref="docs/schedule_verification.md",
        symbols=("check", "main"),
        notes=(
            "Pre-slot launchd check; reopens Claude if absent, records an append-only check row, "
            "and writes one alert per check minute. It never places orders. Install the rendered "
            "deploy/launchd/com.all-stock-dca.claude-liveness.plist.template on the Mac."
        ),
    ),
    EntryPoint(
        need="Alert independently of the executor session when a live slot left no scheduled evidence",
        command=(
            "python -m scripts.schedule_watchdog [--date YYYY-MM-DD] [--root <cron-tracking root>] "
            "[--json] [--slack [--slack-webhook-env DCA_WATCHDOG_SLACK_WEBHOOK]]"
        ),
        exists_path="scripts/schedule_watchdog.py",
        doc_ref="docs/schedule_verification.md",
        symbols=("main", "failing_rows", "write_watchdog_alert_files", "send_slack_alert"),
        notes=(
            "Thin wrapper over scripts.verify_schedule -- does not re-derive evidence, only "
            "alerts on what that module already finds. Exists because every other alert "
            "channel (in-app message center, the Gate 9 repo alert file, email) is written "
            "BY the executor session, so a session that ends on a question instead of "
            "following the abort-and-alert rule (docs/safeguards.md's live execution roster "
            "note) leaves no trace on any of them. This module depends on none of that: it "
            "writes its own durable, idempotent alert file per failing slot (honoring --root, "
            "overwriting rather than numbering on a repeat fire for the same day/slot) and "
            "optionally posts to Slack. Meant to run on its own external schedule, not "
            "inside any of the seven executor sessions -- see the module docstring's "
            "'Deploying this' section."
        ),
    ),
    EntryPoint(
        need="Write a live slot's Gate 9 run_state and refresh its latest pointer",
        command=(
            "python -m scripts.finalize_run_evidence --slot HHMM --started-at <ISO-8601> "
            "--outcome <label> --order-submit-status <status> [--orders-json <file>] "
            "[--signals-json <file>] [--report-json <file>]"
        ),
        exists_path="scripts/finalize_run_evidence.py",
        doc_ref="docs/schedule_verification.md",
        symbols=("main", "finalize", "build_run_state", "acceptance_window"),
        notes=(
            "The one Gate 9 command every live-mirror prompt runs, on every outcome. Stamps the "
            "executor's own run_classification, never overwrites an executor-written run_state, "
            "and refuses an off-window --started-at. Exit 0 only when the slot verifies."
        ),
    ),
    EntryPoint(
        need="Find out whether a scheduled cloud run keeps its cron_tracking files between fires",
        command="python -m scripts.workspace_probe [--root DIR] [--now YYYY-MM-DDTHH:MM:SSZ]",
        exists_path="scripts/workspace_probe.py",
        doc_ref="docs/workspace_probe.md",
        symbols=("main", "run"),
        notes=(
            "Writes one timestamped marker under <root>/workspace_probe/ and lists earlier ones (age from the "
            "time inside each marker). Run it twice as a scheduled task; if the second run lists the first "
            "marker the workspace persists. No broker, order, network or account access."
        ),
    ),
    EntryPoint(
        need="Prove a cloud run's checkout is the pinned release and pull the evidence store (cloud Gate 2)",
        command=(
            "python -m scripts.cloud_preflight --repo-root . --evidence-remote <url> "
            "--evidence-root \"$DCA_CRON_TRACKING_ROOT\" --trading-day YYYY-MM-DD "
            "[--expect-prior-slots 0935,1030] [--init]"
        ),
        exists_path="scripts/cloud_preflight.py",
        doc_ref="docs/cloud_clone_runs.md",
        symbols=("main",),
        notes=(
            "Refuses (exit 2, 'GATE2_ABORT: <reason>') unless HEAD equals $DCA_PINNED_SHA, is merged to "
            "origin/main and unmodified, and the evidence store's MANIFEST.json is usable. Never installs anything."
        ),
    ),
    EntryPoint(
        need="Publish a cloud run's evidence to the private store, or claim a slot before trading",
        command=(
            "python -m scripts.evidence_publish publish|claim --evidence-root <root> --remote <url> "
            "--trading-day YYYY-MM-DD --slot HHMM (--code-sha <40 hex> | --claimant <run id>)"
        ),
        exists_path="scripts/evidence_publish.py",
        doc_ref="docs/cloud_clone_runs.md",
        symbols=("main", "scan_for_secrets"),
        notes=(
            "publish never moves local artifacts, refuses any evidence holding the account number, and on a "
            "failed push returns an alert text with the run numbers. claim is a compare-and-swap: first run "
            "per slot and day wins, an unreachable store refuses."
        ),
    ),
    EntryPoint(
        need="Back up the live strategy config from a pasted thread/notes dump",
        command="python scripts/backup_strategy.py --transcript <file> --write",
        exists_path="scripts/backup_strategy.py",
        doc_ref="docs/README.md",
        symbols=("main",),
        notes="--transcript is required; --write persists the extracted config files (omit for a dry-run preview only).",
    ),
    EntryPoint(
        need="Build a consolidated ledger of every confirmed buy across all streams/days",
        command=(
            "python -m scripts.purchase_history "
            "[--cron-tracking-root <dir>] [--output-dir <dir>] [--additional-root <dir> ...]"
        ),
        exists_path="scripts/purchase_history.py",
        doc_ref="docs/purchase_history.md",
        symbols=("collect_purchase_records", "build_ticker_rollup", "write_purchase_history", "main"),
        notes=(
            "Read-only reporting tool, never on the live buy path -- same category as "
            "backup_strategy.py / drift_detection.py. Writes purchase_history.csv (one row per "
            "confirmed fill) and purchase_history_by_ticker.json (per-ticker rollup). "
            "--additional-root (repeatable) is needed when a stream's live artifact root has "
            "been relocated outside the cron-tracking tree; see docs/purchase_history.md's "
            "Limitations section, which also documents known gaps (morning/midday currently "
            "contribute no confirmed fills; same-ticker multiple orders within one CSV collapse "
            "to one row)."
        ),
    ),
    EntryPoint(
        need="Regenerate per-ticker sizing overrides",
        command="python -m scripts.generate_per_ticker_sizing",
        exists_path="scripts/generate_per_ticker_sizing.py",
        doc_ref="docs/README.md",
        symbols=("main",),
        notes=(
            "Module form required: running the file directly (python scripts/x.py) puts "
            "scripts/ rather than the repo root on sys.path and fails on `import dca_fileio`."
        ),
    ),
    EntryPoint(
        need="Record Wondrous Octopus Tier C sizing suggestions to the append-only suggestion history",
        command="python3 scripts/publish_octopus_sizing.py [--dry-run]",
        exists_path="scripts/publish_octopus_sizing.py",
        doc_ref="docs/README.md",
        symbols=("main", "publish_octopus_sizing"),
        notes=(
            "Translates next-day Octopus multi-quantile forecasts into bounded Tier C recovery "
            "suggestions appended to <cron root>/sizing_suggestions/octopus/; never writes live "
            "config/ml_sizing_adjustment.json (docs/sizing_suggestion_history.md)."
        ),
    ),
    EntryPoint(
        need="Run 90-trading-day walk-forward prediction and sequestered evaluation experiment",
        command="python3 scripts/run_90day_prediction_experiment.py [--days 90] [--tickers SPY AAPL ...] [--model persistence|gbm|octopus]",
        exists_path="scripts/run_90day_prediction_experiment.py",
        doc_ref="README.md",
        symbols=("main", "generate_walkforward_baseline_predictions"),
        notes=(
            "Runs a strictly zero-lookahead walk-forward replay from 90 trading days ago to today, "
            "quarantines predictions, and executes the independent sequestered truth comparator."
        ),
    ),
    EntryPoint(
        need="Evaluate predictions against market actuals with independent sequestered source of truth",
        command="python3 scripts/sequestered_truth_comparator.py --predictions <csv> [--history-dir <dir>] [--report-md <out>]",
        exists_path="scripts/sequestered_truth_comparator.py",
        doc_ref="README.md",
        symbols=("main", "SequesteredComparator", "DailyRecord", "EvaluationSummary"),
        notes=(
            "Standalone, sequestered comparator computing MAE, RMSE, MAPE, directional hit rates, "
            "and quantile band coverage without lookahead bias or model entanglement."
        ),
    ),
    EntryPoint(
        need="Export Octopus ledger predictions and scores for the 61 scheduled stocks",
        command="python3 scripts/export_octopus_ledger.py --run-dir <run_dir> [--all-scheduled] [--csv-out <out.csv>]",
        exists_path="scripts/export_octopus_ledger.py",
        doc_ref="README.md",
        symbols=("main", "export_ledger_records", "load_scheduled_tickers"),
        notes=(
            "Streams records from a Wondrous Octopus run ledger, filters to the requested "
            "tickers (e.g. 61 scheduled stocks), and exports clean tabular CSVs and metrics."
        ),
    ),
    EntryPoint(
        need="Supervise and auto-restart Wondrous Octopus walk-forward runs upon transient crashes or shard shifts",
        command="python3 scripts/auto_resume_walkforward.py [--run-dir <run_dir>] [--delay <sec>] [--max-retries <n>]",
        exists_path="scripts/auto_resume_walkforward.py",
        doc_ref="README.md",
        symbols=("main", "run_supervisor", "build_walkforward_cmd", "build_export_cmd"),
        notes=(
            "Supervises a walk-forward run and automatically resumes with `--resume --retrain` "
            "if interrupted by transient errors (such as macOS inode shifts or file sync locks). "
            "Automatically exports predictions to CSV upon successful completion."
        ),
    ),
    EntryPoint(
        need="Trigger interactive web browser OAuth login for github-mcp-server",
        command="python3 scripts/github_mcp_login.py",
        exists_path="scripts/github_mcp_login.py",
        doc_ref="README.md",
        symbols=("main",),
        notes=(
            "Initiates an interactive OAuth login session with the local GitHub MCP server, "
            "launching the default browser for authorization without requiring on-disk tokens."
        ),
    ),
    EntryPoint(
        need="Recalibrate per-ticker signal parameters for the day",
        command="python -m scripts.run_ticker_maintenance calibrate",
        exists_path="scripts/run_ticker_maintenance.py",
        doc_ref="docs/README.md",
        symbols=("main", "run_calibration"),
        notes=(
            "The `calibrate` subcommand is the independent, force=True path that "
            "config/scheduled_tasks.json names as the canonical scheduled command -- it "
            "recalculates from the local archive unconditionally. Its sibling "
            "`event-calibrate` (force=False) instead requires a same-day, status=completed "
            "ticker_update_event.json naming a changed active ticker, and otherwise leaves "
            "config/per_ticker_sizing.json untouched and exits nonzero; do not substitute it "
            "when the need is an unconditional daily recalibration."
        ),
    ),
    EntryPoint(
        need="Generate Gate 5 buy candidates for the 1530ET final-buy slot",
        command=(
            "python3 -m scripts.generate_buy_candidates --slot 1530ET "
            "--trading-day <YYYY-MM-DD> --market-data <file> --deployed-last-7d-usd <usd>"
        ),
        exists_path="scripts/generate_buy_candidates.py",
        doc_ref="docs/strategy.md",
        symbols=("generate_candidates", "main"),
        notes=(
            "1530ET ONLY -- see the module's own SCOPE section. The other six roster "
            "slots each have their own entry point now (the next four rows) and must use "
            "it: they run through morning_dca.MorningDCAExecutor's "
            "separately-implemented evaluate_signal(), not the near_close_dca functions "
            "this module calls, so pointing this command at one of them would silently "
            "score against the wrong logic. Add an eighth via "
            "scripts/stream_candidates.py (see its row below), never by copying a slot "
            "module."
        ),
    ),
    EntryPoint(
        need="Generate Gate 5 buy candidates for the 0935ET market-open slot",
        command=(
            "python3 -m scripts.generate_market_open_candidates "
            "--trading-day <YYYY-MM-DD> --market-data <file>"
        ),
        exists_path="scripts/generate_market_open_candidates.py",
        doc_ref="docs/market_open_signal_entry.md",
        symbols=("generate_market_open_candidates", "main"),
        notes=(
            "0935ET ONLY. Constructs the real market_open_dca.MarketOpenDCAExecutor and "
            "calls its own evaluate() -- it does NOT go through near_close_dca."
            "evaluate_source_signal the way the 1530ET row above does, because that is "
            "not this slot's signal engine. Takes ONE market-data source (robinhood): "
            "the executor evaluates a single combined provider, so a finance payload is "
            "rejected rather than silently discarded. No --deployed-last-7d-usd: the "
            "7-day kill-switch is funded by afternoon-scheduled evidence only and is "
            "inert on this stream in the live executor too, so accepting one here would "
            "size differently from the executor this mirrors. Gate 5 only -- it never "
            "places an order, never touches a broker, and writes no run artifacts."
        ),
    ),
    EntryPoint(
        need="Generate Gate 5 buy candidates for the 1230ET midday slot",
        command=(
            "python3 -m scripts.generate_midday_candidates "
            "--trading-day <YYYY-MM-DD> --market-data <file> --deployed-last-7d-usd <usd>"
        ),
        exists_path="scripts/generate_midday_candidates.py",
        doc_ref="docs/midday_signal_entry.md",
        symbols=("generate_midday_candidates", "main"),
        notes=(
            "1230ET ONLY. Calls MiddayDCAExecutor.evaluate(), NOT run(): run() owns budget "
            "arithmetic, the top-up guard, placement and the artifact tree, which this slot's "
            "own prompt already owns as Gates 6-9. Two differences from the 0935ET row above: "
            "trend validation APPLIES here, so the payload must carry 5-minute bars from 11:30 "
            "ET onward or qualifying buys silently become manual_review_candidates; and "
            "--deployed-last-7d-usd is REQUIRED, because this slot's producer contract makes "
            "the 7-day kill switch the producer's responsibility (nothing downstream enforces "
            "it). Its `cands` output is the prompt's cands.json payload, calibration fields "
            "included. Get that deployment figure from the risk subsystem rather than "
            "re-deriving it: run `python3 -m dca_risk --state-root "
            "\"$(python3 -c 'import dca_paths; print(dca_paths.cron_tracking_root())')\" "
            "--as-of <YYYY-MM-DD>` (console script run-dca-risk) and read "
            ".scheduled_live.deployed_last_7d_usd -- but first check that report's .evidence "
            "block (state_root_exists true, run_states_loaded > 0, run_states_unreadable == 0, "
            "run_states_unclassifiable == 0, afternoon_states_seen > 0, "
            "afternoon_states_undated == 0, afternoon_states_invalid_notional == 0) "
            "and abort if it fails. "
            "Every figure in that report is a sum over the states it found, so a zero means "
            "'nothing deployed' and 'no history loaded' equally well, and passing the second "
            "as the first leaves the kill switch disengaged and deep-drop candidates uncapped."
        ),
    ),
    EntryPoint(
        need="Generate Gate 5 buy candidates for the 1030ET morning slot",
        command=(
            "python3 -m scripts.generate_morning_candidates "
            "--trading-day <YYYY-MM-DD> --market-data <file>"
        ),
        exists_path="scripts/generate_morning_candidates.py",
        doc_ref="docs/morning_signal_entry.md",
        symbols=("generate_morning_candidates", "load_morning_live_config", "main"),
        notes=(
            "1030ET ONLY. Calls MorningDCAExecutor.evaluate() -- the BASE class, in "
            "MORNING_LIVE_MODE, not a subclass like the other three rows. One thing follows: "
            "load_morning_config takes `mode` "
            "POSITIONALLY-SECOND, so the spec uses the load_morning_live_config wrapper; the "
            "bare loader would have silently loaded 9:45 WATCHLIST policy for a 10:30 LIVE run. "
            "Trend validation applies (payload needs 5-minute bars from 09:30 ET onward). No "
            "--deployed-last-7d-usd, same reason as the 0935ET row."
        ),
    ),
    EntryPoint(
        need="Generate Gate 5 buy candidates for the 1130/1330/1430ET intraday slots",
        command=(
            "python3 -m scripts.generate_intraday_candidates "
            "--slot {1130|1330|1430} --trading-day <YYYY-MM-DD> --market-data <file>"
        ),
        exists_path="scripts/generate_intraday_candidates.py",
        doc_ref="docs/intraday_signal_entry.md",
        symbols=("generate_intraday_candidates", "spec_for_slot", "INTRADAY_SPECS", "main"),
        notes=(
            "ALL THREE intraday fires, one command. They are one cron (7be82b97), one executor "
            "class (IntradayDCAExecutor, which takes slot= as a constructor argument), one "
            "config and one artifact root, differing only in decision anchor and folder suffix "
            "-- so this is one module with a --slot selector, not three. --slot is REQUIRED and "
            "has no default: the executor's own DEFAULT_INTRADAY_SLOT ('1130') is right for a "
            "library caller and wrong here, since a 2:30 run that omitted it would score its "
            "payload against an 11:30 anchor and publish under the 1130ET label. Trend "
            "validation applies to all three, so each payload needs 5-minute bars from 60 "
            "minutes before ITS OWN anchor (10:30 / 12:30 / 13:30). No --deployed-last-7d-usd, "
            "same reason as the 0935ET row. The three fires share ONE pooled budget and nothing "
            "here knows that: pooling is the executing agent's Gate 6/6.5, as the prompts have it."
        ),
    ),
    EntryPoint(
        need="Refresh and re-size 0935ET candidates from post-anchor quotes",
        command=(
            "python3 -m scripts.refresh_market_open_candidates "
            "--candidates <file> --quotes <file>"
        ),
        exists_path="scripts/refresh_market_open_candidates.py",
        doc_ref="docs/market_open_signal_entry.md",
        symbols=("refresh_market_open_candidates", "main"),
        notes=(
            "Gate 6.5 placement-boundary path. Intentionally consumes explicit quotes "
            "after 09:35 and reuses the executor ladder plus shared dynamic-base selector; "
            "it is not a re-run of the fixed-anchor Gate 5 evaluator."
        ),
    ),
    EntryPoint(
        need="Add a Gate 5 entry point for another executor-class slot",
        command="scripts/stream_candidates.py -- define a StreamSpec, then a six-line CLI module",
        exists_path="scripts/stream_candidates.py",
        doc_ref="docs/midday_signal_entry.md",
        symbols=("StreamSpec", "build_document", "run_cli", "run_multi_slot_cli"),
        notes=(
            "The shared Gate 5 core behind all six executor-class slot entry points (0935, 1030, "
            "1130, 1230, 1330, 1430ET): payload parsing, fail-closed input validation, the "
            "executor call, the kill-switch injection, and the output document. A new slot is a "
            "StreamSpec literal plus a thin CLI wrapper -- do NOT copy an existing slot's module "
            "and edit the constants, which is how the three entry points in CLAUDE.md's own "
            "warning drifted apart. Use run_cli for a slot with its own executor class, or "
            "run_multi_slot_cli when one class serves several fires (the intraday case). Every "
            "roster slot now has an entry point; this row is for adding an eighth."
        ),
    ),
    EntryPoint(
        need="Run the live gate sequence in-process: build a plan, then place it",
        command=(
            "snapshot = dca_runner.budget_snapshot(orders, day); "
            "tier = near_close_dca.select_budget_tier(planned_spend_usd, high_multiplier_signal_count); "
            "plan = dca_runner.plan_orders(candidates, snapshot, tier); "
            "dca_runner.execute_plan(plan, broker, snapshot=snapshot, tier=tier)  # import and call, not a CLI"
        ),
        exists_path="dca_runner.py",
        doc_ref="docs/live_wiring.md",
        symbols=("plan_orders", "execute_plan", "budget_snapshot"),
        notes=(
            "Requires injected callables mapped to Robinhood MCP tools. plan_orders() takes an "
            "already-built BudgetSnapshot and BudgetTier as input -- it does not construct them -- "
            "and returns only a Plan, so obtain or construct the snapshot (dca_runner.budget_snapshot) "
            "and select the tier (near_close_dca.select_budget_tier) first, then pass both into "
            "plan_orders(). Reuse those SAME snapshot/tier objects, plus the returned plan, in "
            "execute_plan() -- do not round-trip through assess()'s JSON dict, which cannot be "
            "reconstructed back into a live Plan (see docs/live_wiring.md). assess() is a separate, "
            "JSON-serializable gate-check summary for prompt-driven sessions that have no way to hold a "
            "live Python object between tool calls; a real Python caller should call "
            "plan_orders()/execute_plan() directly instead."
        ),
    ),
    EntryPoint(
        need="Score candidate tickers for the active universe (health/backtest/sentiment)",
        command="python ticker-advisor/advise_tickers.py --candidates TICK1 TICK2 [--skip-sentiment]",
        exists_path="ticker-advisor/advise_tickers.py",
        doc_ref="docs/ticker_advisory.md",
        symbols=("main",),
        notes=(
            "Self-contained subproject, own requirements.txt (cd ticker-advisor && pip install -r "
            "requirements.txt first) -- not importable from the core repo's stdlib-only environment. "
            "Writes a ranked report and a proposed config/tickers.json diff to "
            "analysis/<date>-ticker-advisory/; never edits config/tickers.json or "
            "near_close_dca.ACTIVE_TICKERS itself. Broker tradability still needs manual verification "
            "via Robinhood MCP before applying -- see docs/ticker_advisory.md."
        ),
    ),
)


def _matches(entry: EntryPoint, keyword: str) -> bool:
    haystack = " ".join(
        (entry.need, entry.command, entry.notes, entry.doc_ref, entry.exists_path, *entry.symbols)
    ).lower()
    return keyword.lower() in haystack


def _format_table(entries: Sequence[EntryPoint]) -> str:
    if not entries:
        return "No entry points match that search."
    lines = []
    for entry in entries:
        lines.append(f"- {entry.need}")
        lines.append(f"    run:  {entry.command}")
        lines.append(f"    docs: {entry.doc_ref}")
        if entry.notes:
            lines.append(f"    note: {entry.notes}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print entries as a JSON array")
    parser.add_argument("--search", metavar="KEYWORD", help="filter entries by a case-insensitive keyword")
    args = parser.parse_args(argv)

    entries = ENTRY_POINTS
    if args.search:
        entries = tuple(e for e in entries if _matches(e, args.search))

    if args.json:
        # Always exit 0: stdout is valid JSON either way, including `[]` for a
        # search with no matches. A caller piping this into a JSON parser (or
        # a subprocess.run(..., check=True)) should not have to special-case a
        # clean, successfully-produced empty result as a failure.
        print(json.dumps([asdict(e) for e in entries], indent=2))
        return 0
    print(_format_table(entries))
    return 0 if entries else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
