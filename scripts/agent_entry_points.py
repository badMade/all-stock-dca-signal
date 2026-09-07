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
            "python3 -m scripts.same_day_top_up_gate --account <acct> "
            "--trading-day <YYYY-MM-DD> --orders-pages <file> --candidates <file>"
        ),
        exists_path="scripts/same_day_top_up_gate.py",
        doc_ref="claude/dca-executor-prompts/live-mirror/1030ET-1030-dca-recurring-buy.txt",
        symbols=("main",),
        notes="The printed JSON's buy_usd per ticker is authoritative. Never recompute it by hand.",
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
            "MORNING_LIVE_MODE, not a subclass like the other three rows. Two things follow. "
            "First, the 9:45 PRE-FLIGHT GATE runs: evaluate() narrows the universe to the "
            "same-day pre-flight verdict for exactly this stream+mode, which makes this the "
            "ONLY Gate 5 command that reads the artifact tree and the only one whose output is "
            "not a pure function of its arguments. It is deliberately NON-BLOCKING (Geoff, "
            "2026-08-02) -- a missing/invalid/empty pre-flight degrades to the full universe "
            "with a warning -- and the verdict rides along in the document's preflight_gate "
            "field (null on every other slot). Second, load_morning_config takes `mode` "
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
