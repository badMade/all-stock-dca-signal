#!/usr/bin/env python3
"""Daily DCA artifact drift detector.

Compares the *intended* buys recorded in a run's ``signals.csv`` against the
*actual* order records in ``submitted_orders.csv`` for a single run directory and
flags **drift**: any symbol that carried an auto-buy signal with a positive
intended dollar amount but for which no submitted-order record exists.

This exists because of the **Jul 16 2026 failure mode**: ``signals.csv`` held 8
rows with ``auto_buy_signal=true`` and positive incremental amounts, while
``submitted_orders.csv`` was empty and the ``raw/`` directory was missing -- the
run wrote signals but placed and persisted no orders, and nothing alerted. This
detector is meant to be called by every live run *after* it writes artifacts, so
that condition pages immediately instead of being noticed the next day.

Design constraints (see AGENTS.md):
  * **Standard library only.** No third-party deps, no broker/data connectors.
  * **Never places or reviews trades.** Read-only over artifact files.
  * **No secrets.** The optional Slack path reads a webhook from the environment
    (or an explicitly-passed argument); it never embeds one.
  * **Schema-tolerant.** The morning/midday/near-close streams emit slightly
    different signal columns, so column lookups are alias-based and forgiving.

CLI::

    python scripts/drift_detection.py --run-dir <dir> \
        [--json-out out.json] [--md-out out.md] \
        [--slack] [--slack-webhook-env DCA_DRIFT_SLACK_WEBHOOK] \
        [--fail-on-drift]

Exit codes: ``0`` no drift, ``2`` drift detected (only when ``--fail-on-drift``),
``1`` usage/IO error.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

# This module is a documented standalone CLI (``python scripts/drift_detection.py``),
# and direct invocation puts only ``scripts/`` on sys.path -- so the repo root is
# added here before the shared helpers below are imported. It previously carried a
# private copy of dca_fileio.atomic_write_text to sidestep that, and the copy had
# drifted: it omitted the parent-directory fsync that dca_fileio documents as
# required ("without the directory fsync the rename itself could be lost on power
# failure"), leaving this detector's own output strictly less durable than every
# other artifact this repo writes. Importing the one implementation is what keeps
# them from diverging again. Mirrors what scripts/tests/* already do.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dca_fileio import atomic_write_text  # noqa: E402 - needs the sys.path setup above

# --- Artifact file names -----------------------------------------------------

# The per-stream signal CSVs differ in name; check the generic name first, then
# the stream-specific ones. All share the columns this detector reads.
SIGNAL_CSV_NAMES = (
    "signals.csv",
    "morning_signals.csv",
    "midday_signals.csv",
    "market_open_signals.csv",
    "intraday_signals.csv",
)
# Order-record CSVs, most-authoritative first. submitted_orders.csv is the
# primary "an order was actually placed" record; the others are fallbacks.
ORDER_CSV_NAMES = ("submitted_orders.csv", "orders.csv", "filled_orders.csv")
# Files that, if present and non-empty, name explicit broker/order failures.
ERROR_CSV_NAMES = ("order_errors.csv", "rejections.csv", "order_rejections.csv")
# Afternoon stream writes planned amounts here; signals.csv carries action/multiplier only.
BUY_CANDIDATES_CSV_NAME = "buy_candidates.csv"
RAW_DIR_NAME = "raw"
RUN_STATE_NAME = "run_state.json"

# Streams that never persist a raw/ directory (morning and midday executors
# write only run_state.json + CSVs). A missing raw/ for these is expected and
# must NOT be diagnosed as raw_data_persistence_failure.
_STREAMS_WITHOUT_RAW = {"morning", "midday"}

# scripts/scheduled_artifacts.py's submitted_orders.csv writer emits a row for
# every order ATTEMPT (submitted/filled/pending/unconfirmed) so a live run
# always produces a machine-readable record, but "not_submitted_confirmation_required"
# (near_close_dca.OrderConfirmationRequiredError / run_classification.py's
# ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED) means NO order reached the broker --
# it was blocked pending a separate pre-authorization confirmation step. A row
# with that status must never count as a genuine submission here, or a
# confirmation-blocked buy would be silently reconciled as reported and the
# drift/Slack alert this detector exists for would never fire.
_NOT_ACTUALLY_SUBMITTED_STATUSES = {"not_submitted_confirmation_required"}

# --- Column aliases (schema-tolerant lookups) --------------------------------

SYMBOL_COLUMNS = ("ticker", "symbol")
AUTO_BUY_COLUMNS = ("auto_buy_signal", "auto_buy", "autobuy")
# Morning/midday executor rows use execution_status="auto_buy_eligible" instead
# of a boolean auto_buy column; detect it here so those rows aren't silently
# excluded from the expected-buys set.
EXECUTION_STATUS_COLUMN = "execution_status"
EXEC_AUTO_BUY_VALUE = "auto_buy_eligible"
INCREMENTAL_COLUMNS = ("incremental_order_amount", "incremental_amount_usd", "incremental_amount")
DESIRED_COLUMNS = (
    "afternoon_desired_amount",
    "afternoon_desired_amount_usd",
    "morning_desired_amount",       # morning stream buy-candidates augmentation
    "desired_amount_usd",
    "desired_amount",
    "hypothetical_amount_usd",
    "planned_amount_usd",
    "suggested_amount_usd",         # afternoon near_close_dca _signal_row()
)
TOP_UP_GUARD_COLUMNS = ("top_up_guard_result", "top_up_result", "skip_reason")
BLOCK_REASON_COLUMNS = ("block_reason", "loop_block_reason", "reason")
SPREAD_COLUMNS = ("spread", "spread_pct", "spread_bps")
CONFIDENCE_COLUMNS = ("data_confidence", "confidence", "confidence_status")
TREND_COLUMNS = ("intraday_trend_pct", "trend", "trend_status")
STATUS_COLUMNS = ("status", "execution_status", "order_submit_status")

# Values (case-insensitive) that count as boolean true.
_TRUE_TOKENS = {"true", "1", "yes", "y", "t"}

# buy_candidates.csv status values that indicate a deliberate non-submission.
# An afternoon signal with a matching candidate status is an intentional guard
# outcome, not drift, so we exclude it from the expected set to avoid false alerts.
_DELIBERATE_SKIP_STATUSES = frozenset({
    "blocked",
    "cap_exceeded",   # morning/midday: daily or weekly cap exhausted before placement
    "not_submitted",
    "skipped",
    "manual_review",
    "manual_review_candidate",
    "budget_exceeded",
    "spread_fail",
    "confidence_fail",
    "trend_blocked",
    "no_incremental",
})

# top_up_guard_result / skip_reason substrings that mean "the buy was
# deliberately reduced to $0 for this row" -- an explanation, not silent drift.
_BLOCK_TOKENS = (
    "top_up",
    "topup",
    "no_incremental",
    "price_not_below",
    "offset",
    "budget",
    "cap",
    "kill_switch",
    "killswitch",
    "manual_review",
    "spread",
    "confidence",
    "trend",
    "guard",
    "blocked",
)


def _get(row: Mapping[str, Any], columns: Sequence[str]) -> Optional[Any]:
    """Return the first present, non-empty value among ``columns`` (case-insensitive keys)."""
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for col in columns:
        if col in lower:
            value = lower[col]
            if value is not None and str(value).strip() != "":
                return value
    return None


def parse_bool(value: Any) -> bool:
    """Case-insensitive truthiness: ``true``/``1``/``yes``/``y``/``t`` are true."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_TOKENS


def parse_amount(value: Any) -> float:
    """Best-effort float parse; ``$1,234.50`` and blanks are handled, returns 0.0 on failure."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _symbol(row: Mapping[str, Any]) -> Optional[str]:
    value = _get(row, SYMBOL_COLUMNS)
    return str(value).strip().upper() if value is not None else None


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read a CSV into a list of dict rows. Missing file -> empty list."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- Located artifacts -------------------------------------------------------


@dataclass
class RunArtifacts:
    """Resolved paths + parsed contents for a single run directory."""

    run_dir: Path
    signals_path: Optional[Path]
    signal_rows: List[Dict[str, str]]
    orders_path: Optional[Path]
    order_rows: List[Dict[str, str]]
    order_source_name: Optional[str]
    error_rows: List[Dict[str, str]]
    error_source_name: Optional[str]
    raw_dir_present: bool
    run_state: Optional[Dict[str, Any]]
    buy_candidate_rows: List[Dict[str, str]] = field(default_factory=list)

    @property
    def submitted_symbols(self) -> set:
        symbols = set()
        for row in self.order_rows:
            if row.get("status") in _NOT_ACTUALLY_SUBMITTED_STATUSES:
                continue
            sym = _symbol(row)
            if sym:
                symbols.add(sym)
        # Morning/midday executors record submitted orders in run_state["broker_outcomes"]
        # rather than a separate order CSV; include status="submitted" tickers so their
        # auto_buy_eligible rows are not reported as false-alarm submitted_orders_missing.
        outcomes = (self.run_state or {}).get("broker_outcomes", {})
        if isinstance(outcomes, dict):
            for ticker, outcome in outcomes.items():
                if isinstance(outcome, dict) and outcome.get("status") == "submitted":
                    sym = str(ticker).strip().upper()
                    if sym:
                        symbols.add(sym)
        return symbols


def _first_existing(run_dir: Path, names: Sequence[str]) -> Optional[Path]:
    for name in names:
        candidate = run_dir / name
        if candidate.exists():
            return candidate
    return None


def _has_usable_header(path: Path) -> bool:
    """True when the CSV opens and its header carries a symbol column.

    Deliberately the SAME bar scripts/scheduled_artifacts.py's
    _signals_csv_errors() applies. A looser "any non-blank header" test would
    call a file such as ``garbage\n`` usable, select it over a valid sibling,
    read no symbols from it, and suppress missing-order alerts while the artifact
    contract passes on the sibling (Codex review).
    """
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            # The BODY must parse too, matching the validator. Selection happens
            # here but the rows are read later by read_csv_rows(), so a valid
            # header over a corrupt body would select the broken canonical file
            # over a valid sibling, raise inside load_run_artifacts(), and leave
            # the wrapper logging a detector failure with no drift reconciled
            # (Codex review). Usable has to mean "this consumer can read it".
            for _ in reader:
                pass
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    if not header or not any(str(column).strip() for column in header):
        return False
    return bool({str(column).strip().lower() for column in header} & set(SYMBOL_COLUMNS))


def _first_usable(run_dir: Path, names: Sequence[str]) -> Optional[Path]:
    """The first recognized file with a usable header, else the first existing.

    The artifact contract accepts any recognized signals filename that carries a
    usable header, so a reused run directory can legitimately hold a stale or
    truncated ``signals.csv`` beside a valid ``morning_signals.csv``. Picking the
    first file that merely EXISTS would read the malformed one, see no symbols,
    and report no expected orders while the contract advertises the run as valid
    (Codex review). Falling back to the first existing file keeps the old
    behavior when nothing is usable, so a genuinely broken directory still
    surfaces through the normal empty-signals path rather than as "no file".
    """
    for name in names:
        candidate = run_dir / name
        if candidate.exists() and _has_usable_header(candidate):
            return candidate
    return _first_existing(run_dir, names)


def load_run_artifacts(run_dir: Path) -> RunArtifacts:
    """Locate and parse the signal/order/error artifacts under ``run_dir``."""
    signals_path = _first_usable(run_dir, SIGNAL_CSV_NAMES)
    orders_path = _first_existing(run_dir, ORDER_CSV_NAMES)
    error_path = _first_existing(run_dir, ERROR_CSV_NAMES)
    buy_candidates_path = run_dir / BUY_CANDIDATES_CSV_NAME

    run_state: Optional[Dict[str, Any]] = None
    state_path = run_dir / RUN_STATE_NAME
    if state_path.exists():
        try:
            run_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            run_state = None

    return RunArtifacts(
        run_dir=run_dir,
        signals_path=signals_path,
        signal_rows=read_csv_rows(signals_path) if signals_path else [],
        orders_path=orders_path,
        order_rows=read_csv_rows(orders_path) if orders_path else [],
        order_source_name=orders_path.name if orders_path else None,
        error_rows=read_csv_rows(error_path) if error_path else [],
        error_source_name=error_path.name if error_path else None,
        raw_dir_present=(run_dir / RAW_DIR_NAME).is_dir(),
        run_state=run_state,
        buy_candidate_rows=read_csv_rows(buy_candidates_path),
    )


# --- Drift model -------------------------------------------------------------


@dataclass
class SymbolDrift:
    """One symbol expected to buy but with no order record."""

    symbol: str
    expected_amount_usd: float
    auto_buy_signal: bool
    suspected_cause: str
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "expected_amount_usd": round(self.expected_amount_usd, 2),
            "auto_buy_signal": self.auto_buy_signal,
            "suspected_cause": self.suspected_cause,
            "evidence": self.evidence,
        }


@dataclass
class DriftReport:
    run_dir: str
    drift_detected: bool
    affected_symbols: List[SymbolDrift]
    run_suspected_cause: str
    recommended_action: str
    signals_present: bool
    orders_present: bool
    submitted_orders_empty: bool
    raw_dir_present: bool
    expected_symbol_count: int
    submitted_symbol_count: int
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "drift_detected": self.drift_detected,
            "run_suspected_cause": self.run_suspected_cause,
            "recommended_action": self.recommended_action,
            "expected_symbol_count": self.expected_symbol_count,
            "submitted_symbol_count": self.submitted_symbol_count,
            "signals_present": self.signals_present,
            "orders_present": self.orders_present,
            "submitted_orders_empty": self.submitted_orders_empty,
            "raw_dir_present": self.raw_dir_present,
            "affected_symbols": [s.to_dict() for s in self.affected_symbols],
            "notes": self.notes,
        }

    def to_json(self, *, compact: bool = True) -> str:
        if compact:
            return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        status = "DRIFT DETECTED" if self.drift_detected else "OK — no drift"
        lines = [
            f"# DCA artifact drift report — {status}",
            "",
            f"- **Run directory:** `{self.run_dir}`",
            f"- **Expected buys (auto-buy + positive amount):** {self.expected_symbol_count}",
            f"- **Symbols with an order record:** {self.submitted_symbol_count}",
            f"- **submitted_orders.csv empty:** {self.submitted_orders_empty}",
            f"- **raw/ directory present:** {self.raw_dir_present}",
            f"- **Suspected cause:** {self.run_suspected_cause}",
            f"- **Recommended action:** {self.recommended_action}",
        ]
        if self.affected_symbols:
            lines += [
                "",
                "## Affected symbols",
                "",
                "| Symbol | Expected $ | Auto-buy | Suspected cause | Evidence |",
                "|---|---:|---|---|---|",
            ]
            for sym in self.affected_symbols:
                evidence = "; ".join(sym.evidence) if sym.evidence else "—"
                lines.append(
                    f"| {sym.symbol} | {sym.expected_amount_usd:.2f} | "
                    f"{str(sym.auto_buy_signal).lower()} | {sym.suspected_cause} | {evidence} |"
                )
        if self.notes:
            lines += ["", "## Notes", ""]
            lines += [f"- {note}" for note in self.notes]
        return "\n".join(lines) + "\n"


# --- Cause inference ---------------------------------------------------------

# Run-level causes, checked in priority order. Each is (predicate, cause, action).
CAUSE_RAW_MISSING = "raw_data_persistence_failure"
CAUSE_ORDERS_EMPTY = "submitted_orders_empty"
CAUSE_ORDERS_MISSING = "submitted_orders_missing"
CAUSE_ORDER_ERRORS = "order_errors_or_rejections"
CAUSE_RUN_STATE_ERROR = "run_state_error_or_missing_classification"
CAUSE_ROW_BLOCK = "per_row_guard_or_block"
CAUSE_UNKNOWN = "unknown_no_order_record"


def _row_block_evidence(row: Mapping[str, Any]) -> List[str]:
    """Collect any per-row guard/block/quality signals that explain a $0 buy."""
    evidence: List[str] = []
    guard = _get(row, TOP_UP_GUARD_COLUMNS)
    if guard is not None and any(tok in str(guard).lower() for tok in _BLOCK_TOKENS):
        evidence.append(f"top_up_guard/skip_reason={guard}")
    block = _get(row, BLOCK_REASON_COLUMNS)
    if block is not None:
        evidence.append(f"block_reason={block}")
    status = _get(row, STATUS_COLUMNS)
    if status is not None and str(status).strip().lower() in {
        "manual_review",
        "manual_review_candidate",
        "blocked",
        "not_submitted",
        "skipped",
    }:
        evidence.append(f"status={status}")
    confidence = _get(row, CONFIDENCE_COLUMNS)
    if confidence is not None and str(confidence).strip().lower() in {
        "unavailable",
        "low",
        "false",
        "0",
    }:
        evidence.append(f"data_confidence={confidence}")
    spread = _get(row, SPREAD_COLUMNS)
    if spread is not None:
        evidence.append(f"spread={spread}")
    return evidence


def _infer_run_cause(art: RunArtifacts, submitted_orders_empty: bool) -> str:
    """Pick the single most-likely run-level cause of missing orders."""
    state = art.run_state or {}
    # Morning and midday streams never write a raw/ directory; skip the raw-dir
    # check for them so a genuine placement failure isn't misdiagnosed as a raw
    # persistence problem (which would suggest re-running for raw persistence
    # rather than investigating the broker error).
    stream = str(state.get("stream", "")).lower()
    raw_required = stream not in _STREAMS_WITHOUT_RAW
    if raw_required and not art.raw_dir_present:
        return CAUSE_RAW_MISSING
    if art.orders_path is None:
        return CAUSE_ORDERS_MISSING
    if submitted_orders_empty:
        return CAUSE_ORDERS_EMPTY
    if art.error_rows:
        return CAUSE_ORDER_ERRORS
    submit_status = str(state.get("order_submit_status", "")).lower()
    if art.run_state is None or "error" in submit_status or "run_classification" not in state:
        return CAUSE_RUN_STATE_ERROR
    return CAUSE_UNKNOWN


_ACTION_BY_CAUSE = {
    CAUSE_RAW_MISSING: (
        "raw/ is missing: the run computed signals without persisting connector "
        "responses and likely never reached order placement. Re-run the live task "
        "so raw/ is persisted and orders are placed; page on-call immediately."
    ),
    CAUSE_ORDERS_EMPTY: (
        "submitted_orders.csv is empty despite auto-buy signals: orders were "
        "intended but none recorded. Verify broker connectivity/auth and re-run; "
        "page on-call immediately."
    ),
    CAUSE_ORDERS_MISSING: (
        "No order-record CSV was written: the run did not reach the order-writing "
        "stage. Inspect run_state.json and re-run; page on-call immediately."
    ),
    CAUSE_ORDER_ERRORS: (
        "Order errors/rejections are recorded: review order_errors.csv/rejections.csv "
        "for the broker reason, then re-submit the affected symbols if appropriate."
    ),
    CAUSE_RUN_STATE_ERROR: (
        "run_state.json is missing/errored or lacks run_classification: treat as an "
        "incomplete run. Inspect logs, restore a valid run_state, and re-run."
    ),
    CAUSE_ROW_BLOCK: (
        "Missing orders are explained by per-row guards (top-up/budget/spread/"
        "confidence/trend). Confirm each block is intended; no re-run needed if so."
    ),
    CAUSE_UNKNOWN: (
        "Auto-buy signals have no order record and no guard explains it. Manually "
        "inspect the run directory and broker order history before re-running."
    ),
}


def detect_drift(run_dir: Path) -> DriftReport:
    """Compare signals.csv intent against submitted_orders.csv reality for ``run_dir``."""
    art = load_run_artifacts(run_dir)
    submitted = art.submitted_symbols
    submitted_orders_empty = art.orders_path is not None and len(art.order_rows) == 0

    # Index buy_candidates.csv by ticker for amount lookup on afternoon rows.
    # The afternoon signals.csv carries action/multiplier but not dollar amounts;
    # planned_amount_usd lives in buy_candidates.csv keyed by ticker.
    buy_candidate_by_ticker: Dict[str, Dict[str, str]] = {}
    for bc_row in art.buy_candidate_rows:
        sym = _symbol(bc_row)
        if sym:
            buy_candidate_by_ticker[sym] = bc_row

    # Build expected buys: one entry per ticker regardless of how many provider
    # rows the stream emitted (e.g. near-close emits two rows — finance and
    # robinhood — for the same ticker). Dedup by ticker, keeping the entry with
    # the largest positive amount so we don't double-report a single missing order.
    expected: List[tuple] = []  # (symbol, amount, auto_buy, row_evidence)
    _symbol_to_expected_idx: Dict[str, int] = {}
    for row in art.signal_rows:
        symbol = _symbol(row)
        if symbol is None:
            continue
        exec_status = _get(row, (EXECUTION_STATUS_COLUMN,))
        auto_buy = (
            parse_bool(_get(row, AUTO_BUY_COLUMNS))
            or (exec_status is not None and str(exec_status).strip() == EXEC_AUTO_BUY_VALUE)
        )
        # Afternoon stream: action="buy" with buy_multiplier > 0 is an eligible buy.
        # These rows carry no boolean auto_buy_signal column; recognize the pattern here.
        # Track separately so we can require a buy_candidates.csv entry for these rows
        # (absence means reconciliation filtered the signal out, e.g. Finance-alone).
        auto_buy_from_action = False
        if not auto_buy:
            action_val = _get(row, ("action",))
            multiplier_val = _get(row, ("buy_multiplier",))
            if (
                action_val is not None
                and str(action_val).strip().lower() == "buy"
                and parse_amount(multiplier_val) > 0
            ):
                auto_buy = True
                auto_buy_from_action = True
        incremental = parse_amount(_get(row, INCREMENTAL_COLUMNS))
        desired = parse_amount(_get(row, DESIRED_COLUMNS))
        # For afternoon action="buy" rows, amounts live in buy_candidates.csv.
        # Also check the candidate's outcome status: a deliberate guard block is
        # not drift, so we exclude it from the expected set rather than firing a
        # false alert. Evidence from the candidate row is merged with signal evidence
        # for any remaining rows so deliberate blocks still surface in the report.
        bc_evidence: List[str] = []
        if auto_buy and symbol in buy_candidate_by_ticker:
            bc = buy_candidate_by_ticker[symbol]
            bc_status = str(_get(bc, STATUS_COLUMNS) or "").strip().lower()
            if bc_status in _DELIBERATE_SKIP_STATUSES:
                # Deliberate non-submission recorded in buy_candidates.csv — not drift.
                continue
            bc_evidence = _row_block_evidence(bc)
            if incremental == 0 and desired == 0:
                desired = parse_amount(_get(bc, DESIRED_COLUMNS))
                incremental = parse_amount(_get(bc, INCREMENTAL_COLUMNS))
        elif auto_buy:
            if auto_buy_from_action:
                # Afternoon action=buy row with no matching buy_candidates.csv entry:
                # reconciliation did not produce a candidate (e.g. Finance signal
                # without Robinhood agreement), so no order was ever intended.
                # Skip rather than raising a false drift alert.
                continue
            # Morning/midday executors don't write buy_candidates.csv; their
            # per-symbol execution outcomes (cap_exceeded, blocked, etc.) are
            # recorded in run_state["broker_outcomes"]. Check there so deliberate
            # safety blocks are not misreported as drift.
            _outcomes = (art.run_state or {}).get("broker_outcomes", {})
            if isinstance(_outcomes, dict) and symbol in _outcomes:
                _outcome = _outcomes[symbol]
                _ostatus = str(
                    (_outcome.get("status") if isinstance(_outcome, dict) else _outcome) or ""
                ).strip().lower()
                if _ostatus in _DELIBERATE_SKIP_STATUSES:
                    continue
        positive = incremental > 0 or desired > 0
        if auto_buy and positive:
            amount = incremental if incremental > 0 else desired
            evidence = _row_block_evidence(row) + bc_evidence
            if symbol in _symbol_to_expected_idx:
                idx = _symbol_to_expected_idx[symbol]
                if amount > expected[idx][1]:
                    expected[idx] = (symbol, amount, auto_buy, evidence)
            else:
                _symbol_to_expected_idx[symbol] = len(expected)
                expected.append((symbol, amount, auto_buy, evidence))

    run_cause = _infer_run_cause(art, submitted_orders_empty)

    affected: List[SymbolDrift] = []
    for symbol, amount, auto_buy, row_evidence in expected:
        if symbol in submitted:
            continue
        # A per-row guard explanation overrides the generic run-level cause.
        if row_evidence and run_cause in (CAUSE_UNKNOWN, CAUSE_RUN_STATE_ERROR):
            cause = CAUSE_ROW_BLOCK
        else:
            cause = run_cause
        affected.append(
            SymbolDrift(
                symbol=symbol,
                expected_amount_usd=amount,
                auto_buy_signal=auto_buy,
                suspected_cause=cause,
                evidence=row_evidence,
            )
        )

    drift_detected = bool(affected)
    # The report-level cause is the run cause unless every affected row is a
    # deliberate per-row block, in which case surface that instead.
    if affected and all(s.suspected_cause == CAUSE_ROW_BLOCK for s in affected):
        report_cause = CAUSE_ROW_BLOCK
    elif drift_detected:
        report_cause = run_cause
    else:
        report_cause = "none"

    action = _ACTION_BY_CAUSE.get(report_cause, _ACTION_BY_CAUSE[CAUSE_UNKNOWN]) if drift_detected else "None — signals and orders reconcile."

    notes: List[str] = []
    if art.signals_path is None:
        notes.append("No signals CSV found; nothing to reconcile against.")
    if art.error_source_name:
        notes.append(f"Order-error file present: {art.error_source_name} ({len(art.error_rows)} rows).")
    if art.order_source_name and art.order_source_name != "submitted_orders.csv":
        notes.append(f"submitted_orders.csv absent; used fallback order record {art.order_source_name}.")

    return DriftReport(
        run_dir=str(run_dir),
        drift_detected=drift_detected,
        affected_symbols=affected,
        run_suspected_cause=report_cause,
        recommended_action=action,
        signals_present=art.signals_path is not None,
        orders_present=art.orders_path is not None,
        submitted_orders_empty=submitted_orders_empty,
        raw_dir_present=art.raw_dir_present,
        expected_symbol_count=len(expected),
        submitted_symbol_count=len(submitted),
        notes=notes,
    )


# --- Slack (optional, no secrets) --------------------------------------------

DEFAULT_SLACK_WEBHOOK_ENV = "DCA_DRIFT_SLACK_WEBHOOK"


def build_slack_payload(report: DriftReport) -> Dict[str, Any]:
    """Build a Slack ``chat.postMessage``/incoming-webhook JSON payload from a report."""
    header = ":rotating_light: DCA drift detected" if report.drift_detected else ":white_check_mark: DCA run reconciled"
    symbols = ", ".join(
        f"{s.symbol} (${s.expected_amount_usd:.2f})" for s in report.affected_symbols
    ) or "none"
    text = (
        f"{header}\n"
        f"Run: {report.run_dir}\n"
        f"Affected: {symbols}\n"
        f"Cause: {report.run_suspected_cause}\n"
        f"Action: {report.recommended_action}"
    )
    return {"text": text}


def resolve_webhook(webhook: Optional[str], env_var: str) -> Optional[str]:
    """Return an explicit webhook, else the value of ``env_var``, else None. Never hardcoded."""
    if webhook:
        return webhook
    return os.environ.get(env_var) or None


class _UnsafeWebhookDestinationError(Exception):
    """Webhook URL scheme is not http(s), or its host resolves to a disallowed address."""


def _resolve_ips(hostname: str) -> "set[ipaddress._BaseAddress]":
    """Resolve ``hostname``'s IP addresses. A seam so tests can fake DNS."""
    return {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(hostname, None)}


def _is_disallowed_ip(ip: "ipaddress._BaseAddress") -> bool:
    """True unless ``ip`` is safe to open an outbound webhook connection to.

    ``is_global`` is documented as having "value opposite to is_private,
    except for the 100.64.0.0/10 [carrier-grade NAT / shared address space]
    range where they are both False" -- so checking only ``is_private`` (the
    pre-fix state) let a resolved address in that range through even though
    it can address services inside a VPC, Kubernetes cluster, or carrier
    network. ``is_multicast`` and IPv6's deprecated site-local space
    (``fec0::/10``) are checked explicitly because ``is_global`` reports
    both of those as ``True``. Mirrors ``dca_alerts._is_disallowed_ip`` --
    duplicated for the same reason as ``_assert_safe_webhook_destination``
    below.
    """
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or bool(getattr(ip, "is_site_local", False))
    )


def _proxy_configured_for(url: str) -> bool:
    """True if an environment-configured forward proxy applies to ``url``.

    Mirrors ``dca_alerts._proxy_configured_for`` -- see that docstring for
    why a proxy-only deployment needs to skip local DNS resolution here.
    """
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        return False
    proxy = urllib.request.getproxies().get(parsed.scheme.lower())
    if not proxy:
        return False
    return not urllib.request.proxy_bypass(hostname)


def _assert_safe_webhook_destination(url: str) -> None:
    """Raise :class:`_UnsafeWebhookDestinationError` unless ``url`` is http(s) and
    every address its host resolves to is a public, routable address.

    Restricting only the scheme (the pre-fix state) still lets an
    attacker-influenced webhook value point at ``http://127.0.0.1:8080/`` or the
    ``http://169.254.169.254/latest/meta-data/`` cloud-metadata endpoint. Every
    resolved address is checked, not just the first, since a hostname can
    resolve to a mix of public and private IPs. Mirrors
    ``dca_alerts._assert_safe_destination`` -- duplicated rather than imported to
    keep this module free of a dependency on the repo-root package (see the
    ``atomic_write_text`` import note above for why that's already an exception,
    not the rule).

    This is a preflight check only -- see ``dca_alerts._assert_safe_destination``'s
    docstring on the DNS-rebinding window a separate lookup here leaves open;
    :class:`_PinnedHTTPConnection` below closes it for the actual request by
    resolving, validating, and connecting in one step.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise _UnsafeWebhookDestinationError(f"unsupported scheme {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise _UnsafeWebhookDestinationError("missing host")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise _UnsafeWebhookDestinationError("malformed port") from exc
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        # A literal address (e.g. "https://169.254.169.254/...") needs no DNS
        # resolution at all -- checking it is free regardless of whether a
        # proxy is configured, and skipping it (the proxy-bypass branch
        # below) would let an attacker-controlled literal straight through
        # in proxy-enabled deployments (codex review on this PR).
        if _is_disallowed_ip(literal_ip):
            raise _UnsafeWebhookDestinationError("disallowed address")
        return
    try:
        ips = _resolve_ips(hostname)
    except OSError as exc:
        # Only a hostname that genuinely fails to resolve locally gets the
        # proxy exemption -- that's the deployment this exists for (the
        # proxy resolves it remotely). A hostname that DOES resolve locally
        # is still checked below even when some proxy happens to be
        # configured (e.g. a blanket corporate egress policy unrelated to
        # this URL): unconditionally skipping validation whenever any proxy
        # exists let an attacker-controlled hostname (as opposed to a
        # literal IP, handled above) resolve via the proxy to a private/
        # link-local address completely unchecked (codex review on this PR).
        if _proxy_configured_for(url):
            return
        raise _UnsafeWebhookDestinationError("host does not resolve") from exc
    if not ips or any(_is_disallowed_ip(ip) for ip in ips):
        raise _UnsafeWebhookDestinationError("host resolves to a disallowed address")


def _connect_pinned(conn: "http.client.HTTPConnection") -> None:
    """Resolve ``conn.host``, validate every resolved address, and connect to
    one of the validated addresses -- all in one step. Mirrors
    ``dca_alerts._connect_pinned``; see that docstring for the DNS-rebinding
    gap this closes (codex review discussion_r3679190498).
    """
    try:
        ips = list(_resolve_ips(conn.host))
    except OSError as exc:
        raise _UnsafeWebhookDestinationError("host does not resolve") from exc
    if not ips or any(_is_disallowed_ip(ip) for ip in ips):
        raise _UnsafeWebhookDestinationError("host resolves to a disallowed address")
    last_exc: Optional[OSError] = None
    for ip in ips:
        try:
            conn.sock = socket.create_connection(
                (str(ip), conn.port), conn.timeout, conn.source_address
            )
            return
        except OSError as exc:
            last_exc = exc
    raise last_exc if last_exc is not None else OSError("no address to connect to")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection pinned to a validated address (see :func:`_connect_pinned`)."""

    def connect(self) -> None:
        _connect_pinned(self)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The HTTPS equivalent of :class:`_PinnedHTTPConnection` (see that class
    and ``dca_alerts._PinnedHTTPSConnection`` for why only the raw connect
    step is overridden)."""

    def connect(self) -> None:
        _connect_pinned(self)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


def _request_is_proxied(req: "urllib.request.Request") -> bool:
    """True if ``req`` is being routed through a forward proxy. Mirrors
    ``dca_alerts._request_is_proxied`` -- see that docstring for why a
    proxied request must fall back to the stock connection classes.
    """
    if getattr(req, "_tunnel_host", None):
        return True
    return req.selector.startswith(("http://", "https://"))


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        if _request_is_proxied(req):
            return self.do_open(http.client.HTTPConnection, req)
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # Only pass context, not check_hostname: on Python 3.12+,
        # HTTPSHandler.__init__ folds check_hostname into the SSL context
        # and no longer sets self._check_hostname at all, and
        # http.client.HTTPSConnection.__init__ no longer accepts a
        # check_hostname argument either -- accessing/forwarding it here
        # raised AttributeError/TypeError on every real HTTPS request under
        # 3.12+ (Codex review on this PR). This handler is always
        # constructed with no arguments (build_opener() below), so
        # self._check_hostname is always the default None on 3.11 too,
        # making this identical in practice to the stock 3.11 behavior.
        if _request_is_proxied(req):
            return self.do_open(http.client.HTTPSConnection, req, context=self._context)
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target the same way as the initial request.

    ``urlopen``'s default redirect handling blindly follows ``Location``
    headers, so an initially-allowed public webhook URL could 302 to
    ``http://169.254.169.254/...`` and defeat
    :func:`_assert_safe_webhook_destination` entirely. Each hop through this
    handler re-runs the same scheme and resolved-address check before it is
    followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _assert_safe_webhook_destination(newurl)
        except _UnsafeWebhookDestinationError as exc:
            raise urllib.error.HTTPError(
                newurl, code, f"blocked unsafe redirect destination: {exc}", headers, fp
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Installed once at import time so every urllib.request.urlopen() call in this
# process -- not just the one in this module -- gets redirect validation and
# pinned-address connections.
urllib.request.install_opener(
    urllib.request.build_opener(_SafeRedirectHandler, _PinnedHTTPHandler, _PinnedHTTPSHandler)
)


def _sanitize_exception(exc: BaseException) -> str:
    """Render ``exc`` as its class name plus, only for known-safe cases, a
    short reason -- never its full message.

    A delivery failure can raise exceptions whose message embeds the
    complete offending URL/path -- e.g. ``http.client.InvalidURL`` for a
    webhook URL containing a space or control character quotes the whole
    path back -- and webhook URLs commonly carry auth tokens in that path.
    Logging ``str(exc)`` unconditionally would expose those tokens to stderr.
    Mirrored identically from dca_alerts._sanitize_exception.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"{type(exc).__name__}: HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        # ``reason`` can be a plain string OR an underlying exception object
        # (e.g. a gaierror) constructed from arbitrary context -- stringifying
        # it directly would risk reintroducing exactly the URL/path leak this
        # function exists to prevent, so only its TYPE (plus, for an OSError-like
        # reason, its numeric errno -- never its message) is reported.
        reason = exc.reason
        if isinstance(reason, OSError) and getattr(reason, "errno", None) is not None:
            return f"{type(exc).__name__}: errno {reason.errno}"
        if isinstance(reason, BaseException):
            return f"{type(exc).__name__}: {type(reason).__name__}"
        return type(exc).__name__
    return type(exc).__name__


def send_slack_alert(
    report: DriftReport,
    *,
    webhook: Optional[str] = None,
    env_var: str = DEFAULT_SLACK_WEBHOOK_ENV,
    timeout: float = 10.0,
) -> bool:
    """POST the report to a Slack incoming webhook. Returns False when no webhook is configured.

    The webhook URL is read from ``webhook`` or the ``env_var`` environment
    variable -- never embedded in source. Raises on HTTP/network failure so the
    caller can decide whether a failed alert should escalate.
    """
    url = resolve_webhook(webhook, env_var)
    if not url:
        return False

    url = url.strip()
    if not url:
        return False

    try:
        _assert_safe_webhook_destination(url)
    except _UnsafeWebhookDestinationError as exc:
        raise urllib.error.URLError(f"Invalid or unsafe webhook destination: {exc}") from exc

    payload = json.dumps(build_slack_payload(report)).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied webhook
        return 200 <= response.status < 300


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect DCA signals-vs-orders drift for a run directory.")
    parser.add_argument("--run-dir", required=True, type=Path, help="Run directory containing the artifacts.")
    parser.add_argument("--json-out", type=Path, help="Write the compact JSON report to this path.")
    parser.add_argument("--md-out", type=Path, help="Write the markdown report to this path.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON printed to stdout.")
    parser.add_argument("--slack", action="store_true", help="Send a Slack alert when drift is detected.")
    parser.add_argument(
        "--slack-always",
        action="store_true",
        help="With --slack, also send on a clean (no-drift) run.",
    )
    parser.add_argument("--slack-webhook", help="Slack webhook URL (prefer the env var; no secrets in configs).")
    parser.add_argument(
        "--slack-webhook-env",
        default=DEFAULT_SLACK_WEBHOOK_ENV,
        help=f"Env var holding the Slack webhook (default {DEFAULT_SLACK_WEBHOOK_ENV}).",
    )
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit non-zero (2) when drift is detected, for scheduler gating.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    run_dir: Path = args.run_dir
    if not run_dir.is_dir():
        print(f"error: run directory not found: {run_dir}", file=sys.stderr)
        return 1

    report = detect_drift(run_dir)

    if args.json_out:
        atomic_write_text(args.json_out, report.to_json(compact=True) + "\n")
    if args.md_out:
        atomic_write_text(args.md_out, report.to_markdown())

    print(report.to_json(compact=not args.pretty))

    if args.slack and (report.drift_detected or args.slack_always):
        try:
            sent = send_slack_alert(report, webhook=args.slack_webhook, env_var=args.slack_webhook_env)
            if not sent:
                print(
                    f"warning: --slack set but no webhook configured "
                    f"(pass --slack-webhook or set ${args.slack_webhook_env})",
                    file=sys.stderr,
                )
        except (urllib.error.URLError, OSError) as exc:
            print(f"warning: Slack alert failed: {_sanitize_exception(exc)}", file=sys.stderr)

    if args.fail_on_drift and report.drift_detected:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
