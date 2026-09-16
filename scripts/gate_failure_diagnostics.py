#!/usr/bin/env python3
"""Diagnostics for two documented, fail-closed live-run aborts.

This module explains -- it never changes -- the verdict of two gates that
correctly abort a run when the evidence they need is missing or untrustworthy:

  1. ``dca_risk.is_afternoon_scheduled_evidence``: the 1230ET/1030ET producer
     contract requires the trailing-7-day AFTERNOON-scheduled deployment
     figure (docs/midday_signal_entry.md). A qualifying count of 0 means
     abort, not a deployment of $0.00 -- but the bare count does not say
     WHY it is 0 (root missing, tree empty, evidence stale, or every state
     found belongs to a different stream). :func:`diagnose_afternoon_evidence_gap`
     answers that, reusing the same public predicates the gate itself calls
     so the two can never disagree.
  2. ``scripts.same_day_top_up_gate._bind_saved_pages``: rejects an
     ``--orders-pages`` snapshot that cannot be proven to be the raw,
     account/day-pinned broker response (docs/reliability_spec.md section 3).
     :func:`diagnose_orders_pages_rejection` wraps that same function -- the
     single source of truth for the reason codes -- and adds a human
     explanation, a recommendation, and a payload-shape breakdown, without
     ever persisting the actual account number or trading day (AGENTS.md
     secrets policy applies to a local diagnostics log exactly as it does to
     tracked source).

Diagnostic-only, by design: this module never retries a placement, never
fabricates evidence a gate rejected, and is never called from
``scripts/self_heal.py`` (which is CI-only, has no broker/MCP access, and
cannot safely act on live trading state). It is a read-only tool an operator
or a live executor session runs by hand when a gate aborts, and a JSONL
logger so repeated aborts accumulate a queryable history instead of each one
being a one-off derivation snippet.

Standard library only. No network calls, no secrets.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import dca_paths
import dca_risk
from scripts.same_day_top_up_gate import _bind_saved_pages

# --- diagnose_afternoon_evidence_gap: likely_cause vocabulary -----------------

#: No root in the given list exists on disk at all.
AFTERNOON_ROOT_MISSING = "afternoon_roots_missing"
#: Every given root exists, but none holds a single run_state*.json file.
AFTERNOON_NO_FILES = "afternoon_no_run_state_files"
#: Qualifying afternoon evidence exists, but none of it falls inside the
#: trailing window -- the newest qualifying run is older than window_days.
AFTERNOON_STALE = "afternoon_evidence_stale"
#: Run states were found and read, but none of them classify as afternoon
#: scheduled evidence (wrong stream, before the 2:30 PM floor, or missing a
#: timestamp) -- the tree is populated with the WRONG evidence, not empty.
AFTERNOON_STREAM_EXCLUDED = "afternoon_evidence_excluded_by_stream"
#: At least one qualifying afternoon run state falls inside the window --
#: the gate's own 0-count, if seen, came from a different root list or a
#: different DCA_CRON_TRACKING_ROOT than the one diagnosed here.
AFTERNOON_HEALTHY = "afternoon_evidence_healthy"


def _classify_afternoon_state(state: Dict[str, Any]) -> str:
    """Bucket a single run_state by why it does/doesn't count as afternoon evidence.

    Mirrors ``dca_risk.is_afternoon_scheduled_evidence``'s own branching
    exactly (same order, same predicates) so this diagnostic cannot report a
    reason that disagrees with the gate it explains. Returns ``"qualifying"``
    for a state the gate would accept; the caller resolves that further into
    an in-window/outside-window/undated bucket, which needs ``as_of`` and is
    not this function's concern.
    """
    if dca_risk.classify_stream(state) != dca_risk.STREAM_SCHEDULED:
        return "not_scheduled_stream"
    if dca_risk.is_morning_scheduled_evidence(state) or dca_risk.is_midday_scheduled_evidence(state):
        return "morning_or_midday_stream_excluded"
    if state.get("recovered_deployment_evidence") is True:
        return "qualifying"
    dt = dca_risk.run_timestamp_et(state)
    if dt is None:
        return "missing_run_timestamp"
    if dt.timetz().replace(tzinfo=None) < dca_risk.AFTERNOON_EVIDENCE_FLOOR_ET:
        return "before_afternoon_floor"
    return "qualifying"


def _window_bucket(state: Dict[str, Any], *, as_of: date, window_days: int) -> Tuple[str, Optional[date]]:
    """For a state ``_classify_afternoon_state`` already called qualifying,
    resolve which window bucket it falls into and its attributed date.

    Mirrors the window arithmetic in docs/midday_signal_entry.md's own
    documented derivation snippet (``0 <= (d - ts.date()).days < window_days``)
    exactly, including that snippet's behavior for an undated qualifying state
    (only reachable via ``recovered_deployment_evidence``): with no timestamp
    to compare, the snippet's own ``and`` short-circuits it out of the
    in-window count, so it is bucketed separately here rather than silently
    counted as either in- or outside-window.
    """
    dt = dca_risk.run_timestamp_et(state)
    if dt is None:
        return "qualifying_undated", None
    run_date = dt.date()
    days_ago = (as_of - run_date).days
    if 0 <= days_ago < window_days:
        return "qualifying_in_window", run_date
    return "qualifying_outside_window", run_date


def _scan_root(root: str, *, as_of: date, window_days: int) -> Dict[str, Any]:
    """Diagnostic report for one root: what was found, and why each state
    read from it does or doesn't count as in-window afternoon evidence."""
    root_path = Path(root)
    exists = root_path.exists()
    files = dca_risk.find_run_state_files(root) if exists else []
    reason_counts: Counter = Counter()
    files_unreadable = 0
    newest_qualifying_date: Optional[date] = None
    for path in files:
        try:
            data = json.loads(path.read_bytes())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            files_unreadable += 1
            continue
        if not isinstance(data, dict):
            files_unreadable += 1
            continue
        reason = _classify_afternoon_state(data)
        if reason == "qualifying":
            bucket, run_date = _window_bucket(data, as_of=as_of, window_days=window_days)
            reason_counts[bucket] += 1
            if run_date is not None and (newest_qualifying_date is None or run_date > newest_qualifying_date):
                newest_qualifying_date = run_date
        else:
            reason_counts[reason] += 1
    return {
        "root": str(root),
        "exists": exists,
        "run_state_files_found": len(files),
        "files_unreadable": files_unreadable,
        "reason_counts": dict(reason_counts),
        "newest_qualifying_date": newest_qualifying_date.isoformat() if newest_qualifying_date else None,
    }


def diagnose_afternoon_evidence_gap(
    roots: Sequence[str],
    *,
    as_of: date,
    window_days: int = dca_risk.KILL_SWITCH_WINDOW_DAYS,
) -> Dict[str, Any]:
    """Explain why the trailing-window afternoon-evidence count is (or isn't) 0.

    ``roots`` should be every root ``is_afternoon_scheduled_evidence`` could
    draw evidence from for the slot being diagnosed -- per
    docs/midday_signal_entry.md, that means BOTH the final-buy root
    (``dca_paths.stream_root('ce16ba6b', 'scheduled_runs')``) and the 2:30 PM
    intraday root (``dca_paths.stream_root('intraday_dca')``); omitting either
    silently undercounts, exactly as it would for the gate itself. Reports
    ``cron_tracking_root``/``cron_tracking_root_env_override`` so an operator
    can immediately see whether this diagnostic (and by extension, the run
    that aborted) is even reading the tree they expect.
    """
    root_reports = [_scan_root(root, as_of=as_of, window_days=window_days) for root in roots]
    qualifying_in_window_total = sum(r["reason_counts"].get("qualifying_in_window", 0) for r in root_reports)
    qualifying_outside_window_total = sum(r["reason_counts"].get("qualifying_outside_window", 0) for r in root_reports)
    total_files_found = sum(r["run_state_files_found"] for r in root_reports)
    any_root_exists = any(r["exists"] for r in root_reports)
    excluded_total = sum(
        r["reason_counts"].get("not_scheduled_stream", 0)
        + r["reason_counts"].get("morning_or_midday_stream_excluded", 0)
        + r["reason_counts"].get("missing_run_timestamp", 0)
        + r["reason_counts"].get("before_afternoon_floor", 0)
        for r in root_reports
    )

    if not any_root_exists:
        likely_cause = AFTERNOON_ROOT_MISSING
    elif total_files_found == 0:
        likely_cause = AFTERNOON_NO_FILES
    elif qualifying_in_window_total > 0:
        likely_cause = AFTERNOON_HEALTHY
    elif qualifying_outside_window_total > 0:
        likely_cause = AFTERNOON_STALE
    elif excluded_total > 0:
        likely_cause = AFTERNOON_STREAM_EXCLUDED
    else:
        # Every file present was unreadable/not-a-dict; nothing classifiable
        # was found, which reads the same as an empty tree to an operator.
        likely_cause = AFTERNOON_NO_FILES

    return {
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "cron_tracking_root": dca_paths.cron_tracking_root(),
        "cron_tracking_root_env_override": bool(os.environ.get(dca_paths.CRON_TRACKING_ROOT_ENV)),
        "roots": root_reports,
        "qualifying_in_window_total": qualifying_in_window_total,
        "likely_cause": likely_cause,
    }


# --- diagnose_orders_pages_rejection -------------------------------------------

# (explanation, recommendation) per reason code _bind_saved_pages returns.
# Kept here rather than in same_day_top_up_gate: that module's reason strings
# are its own contract, unchanged by this addition -- this is prose ABOUT
# them, not a new source of truth for them.
_ORDERS_PAGES_REASON_DETAIL: Dict[str, Tuple[str, str]] = {
    "orders_pages_missing_query_metadata": (
        "The --orders-pages payload is not the raw broker_reconciliation "
        "raw_sink snapshot: it is missing the pages/account_number/trading_day "
        "envelope scripts.same_day_top_up_gate requires to prove which account "
        "and day it covers. This is the shape produced when a large "
        "get_equity_orders result is reconstructed via manual field-extraction "
        "from a truncated MCP tool result, rather than saved verbatim.",
        "Re-fetch the orders with a narrower query (single symbol or a "
        "shorter date range) that stays under the inline tool-result size "
        "limit, and persist the full raw JSON envelope -- including "
        "account_number and trading_day -- rather than an extracted order "
        "list.",
    ),
    "orders_pages_account_mismatch": (
        "The saved pages carry an account_number that does not match the "
        "account this run is trading, so they cannot be trusted as this "
        "account's same-day order history.",
        "Re-fetch get_equity_orders for the correct account and re-save the raw response before retrying the gate.",
    ),
    "orders_pages_trading_day_mismatch": (
        "The saved pages carry a trading_day that does not match today's "
        "trading day, so they cannot be trusted as today's same-day order "
        "history.",
        "Re-fetch get_equity_orders for today's trading day and re-save the raw response before retrying the gate.",
    ),
}


def _describe_payload_shape(payload: Any) -> Dict[str, Any]:
    """Low-level shape facts about a rejected payload -- never the payload's
    actual account_number/trading_day values (see module docstring)."""
    is_dict = isinstance(payload, dict)
    return {
        "type": type(payload).__name__,
        "has_pages_key": is_dict and "pages" in payload,
        "pages_is_list": is_dict and isinstance(payload.get("pages"), list),
        "has_account_number_field": is_dict and bool(str(payload.get("account_number") or "").strip()),
        "has_trading_day_field": is_dict and bool(str(payload.get("trading_day") or "").strip()),
    }


def diagnose_orders_pages_rejection(payload: Any, *, account_number: str, trading_day: str) -> Dict[str, Any]:
    """Explain why ``same_day_top_up_gate._bind_saved_pages`` did or didn't
    accept ``payload`` as this account/day's same-day order history.

    Wraps ``_bind_saved_pages`` directly rather than re-deriving its checks,
    so this diagnostic cannot silently drift from the gate it explains.
    """
    pages, reason = _bind_saved_pages(payload, account_number=account_number, trading_day=trading_day)
    if reason is None:
        return {"rejected": False, "reason_code": None, "pages_found": len(pages or [])}
    explanation, recommendation = _ORDERS_PAGES_REASON_DETAIL[reason]
    return {
        "rejected": True,
        "reason_code": reason,
        "explanation": explanation,
        "recommendation": recommendation,
        "payload_shape": _describe_payload_shape(payload),
    }


# --- JSONL logger ---------------------------------------------------------------


def log_gate_failure_diagnostic(record: Dict[str, Any], *, log_path: Optional[str] = None) -> Path:
    """Append ``record`` as one JSON line to a durable diagnostics log.

    Defaults to ``dca_paths.stream_root('gate_diagnostics', 'gate_failures.jsonl')``
    -- under the gitignored cron_tracking tree, matching every other local
    run-output convention in this repo (CLAUDE.md's "Do not modify" list).
    """
    path = (
        Path(log_path)
        if log_path is not None
        else Path(dca_paths.stream_root("gate_diagnostics", "gate_failures.jsonl"))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(record)
    entry.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


# --- CLI --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    afternoon = sub.add_parser(
        "afternoon-evidence",
        help="Diagnose a Gate 5 zero-qualifying-afternoon-evidence abort",
    )
    afternoon.add_argument(
        "--root",
        action="append",
        required=True,
        dest="roots",
        help="A run_state*.json root to scan; repeat for multiple roots (both the "
        "final-buy and 2:30 intraday roots are required -- see docs/midday_signal_entry.md)",
    )
    afternoon.add_argument(
        "--as-of",
        default=datetime.now(dca_risk.EASTERN).date().isoformat(),
        help="Report date (YYYY-MM-DD); defaults to today",
    )
    afternoon.add_argument("--window-days", type=int, default=dca_risk.KILL_SWITCH_WINDOW_DAYS)
    afternoon.add_argument("--log-path", default=None, help="Override the default JSONL log location")

    orders = sub.add_parser(
        "orders-pages",
        help="Diagnose a same-day top-up gate --orders-pages rejection",
    )
    orders.add_argument("--orders-pages-file", required=True, help="Path to the rejected --orders-pages JSON file")
    orders.add_argument("--account", required=True, help="The account this run is trading")
    orders.add_argument("--trading-day", required=True, help="Today's trading day (YYYY-MM-DD)")
    orders.add_argument("--log-path", default=None, help="Override the default JSONL log location")

    args = parser.parse_args(argv)

    if args.command == "afternoon-evidence":
        as_of = date.fromisoformat(args.as_of)
        result = diagnose_afternoon_evidence_gap(args.roots, as_of=as_of, window_days=args.window_days)
        log_gate_failure_diagnostic({"gate": "afternoon_evidence", **result}, log_path=args.log_path)
    else:
        payload = json.loads(Path(args.orders_pages_file).read_text())
        result = diagnose_orders_pages_rejection(payload, account_number=args.account, trading_day=args.trading_day)
        log_gate_failure_diagnostic({"gate": "same_day_top_up", **result}, log_path=args.log_path)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
