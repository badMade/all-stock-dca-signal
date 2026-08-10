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
        need="Back up the live strategy config from a pasted thread/notes dump",
        command="python scripts/backup_strategy.py --transcript <file> --write",
        exists_path="scripts/backup_strategy.py",
        doc_ref="docs/README.md",
        symbols=("main",),
        notes="--transcript is required; --write persists the extracted config files (omit for a dry-run preview only).",
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
        doc_ref="claude/GATE5-PROPOSAL.md",
        symbols=("generate_candidates", "main"),
        notes=(
            "1530ET ONLY -- see the module's own SCOPE section. The other six roster "
            "slots (0935/1030/1130/1230/1330/1430ET) are NOT covered: they run through "
            "morning_dca.MorningDCAExecutor's separately-implemented evaluate_signal(), "
            "not the near_close_dca functions this module calls, and using this command "
            "for them would silently score against the wrong logic."
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
