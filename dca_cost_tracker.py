"""Monthly usage/cost tracker for the near-close graded DCA automation.

Aggregates the lightweight usage-proxy blocks that ``near_close_dca`` writes into
each daily ``run_state.json`` and rolls them up, by component, into a markdown
table + connector-agnostic payload for the Notion "Automation Cost Tracker"
section.

Everything here is an *estimate/proxy*, not exact credit spend. The inputs are
activity counters (calls attempted, rows written, orders placed, artifact bytes)
captured locally during each run. The per-component "estimated proxy units" are
those counts multiplied by fixed, unitless relative weights so month-over-month
comparisons show which components dominate automation load. They are NOT dollars
and NOT API/model credits, and must never be presented as such.

This module is connector-agnostic and secret-free: it reads local artifacts and
emits markdown/JSON. It never contacts Notion, a broker, or any network service,
and holds no tokens, page ids, or credentials. The Notion page id is supplied by
the caller/scheduler.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from dca_fileio import atomic_write_text

logger = logging.getLogger("near_close_dca.cost_tracker")

COST_TRACKER_TITLE = "Automation Cost Tracker"

PROXY_DISCLAIMER = (
    "Estimates only — derived from local activity proxies (call/row/order/byte "
    "counts), not exact credit or dollar spend. Relative proxy weights are unitless "
    "and exist only to rank which components dominate automation load."
)

# Numeric usage-proxy fields summed across a month. ``manual_recovery_required`` is
# a per-run boolean handled separately; ``cost_control_notes`` is non-numeric.
_NUMERIC_PROXY_FIELDS = (
    "tickers_scanned",
    "finance_calls_attempted",
    "robinhood_calls_attempted",
    "signal_rows_written",
    "buy_candidates_written",
    "order_reviews_attempted",
    "orders_placed_count",
    "state_file_bytes",
    "csv_artifact_count",
    "total_artifact_bytes",
    "notifications_sent",
    "errors_count",
)

# Component -> (usage-proxy fields feeding its count, unitless proxy weight).
# Weights are deliberately coarse and relative; artifact I/O is weighted per-KB so
# byte counts stay on a comparable scale with call/order counts. NOT credits.
COMPONENT_DEFINITIONS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("finance_data", ("finance_calls_attempted",), 1.0),
    ("robinhood_data", ("robinhood_calls_attempted",), 1.0),
    ("order_execution", ("order_reviews_attempted", "orders_placed_count"), 2.0),
    ("signal_scan", ("signal_rows_written", "buy_candidates_written"), 0.1),
    ("notifications", ("notifications_sent",), 0.5),
    ("artifact_io_kb", ("total_artifact_bytes",), 0.001),
    ("errors_recovery", ("errors_count",), 1.0),
)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def find_run_state_files(root: str | Path) -> List[Path]:
    """Return every ``run_state.json`` at or below ``root`` (sorted)."""
    root_path = Path(root)
    if root_path.is_file():
        if root_path.name == "run_state.json":
            return [root_path]
        return []
    return sorted(root_path.rglob("run_state.json"))


def _month_prefix(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


#: Candidate statuses that mean a broker order really exists for that row.
#: Mirrors dca_risk.DEPLOYED_CANDIDATE_STATUSES; kept as a local copy rather than an
#: import because dca_risk pulls in near_close_dca, and this reporting module
#: is deliberately stdlib-plus-dca_fileio only.
_ORDER_BEARING_STATUSES = frozenset({"submitted", "filled", "pending", "unconfirmed"})


def _is_empty_blocked_stub(data: Dict[str, Any]) -> bool:
    """True for a ``blocked`` run_state that did no work worth rolling up.

    Blocked is not by itself a reason to drop a run. A run can be marked
    blocked AFTER an earlier candidate was successfully submitted -- raw
    persistence failing on a LATER candidate is the concrete case -- and it
    still carries real API activity, an order count, artifact bytes and a
    recovery signal. Skipping every blocked state unconditionally dropped all
    of that from the monthly rollup, understating cost for exactly the runs
    that went wrong (Codex review: "Retain blocked runs that contain submitted
    orders").

    What SHOULD be skipped is the minimal date-gate stub: no usage proxy, no
    evaluated signals, no orders, and no candidate row OR broker outcome
    carrying broker-order evidence. Anything else is an analyzed run whose
    cost is real.

    Order evidence is looked for in every place a valid artifact may carry it,
    not only in ``buy_candidates`` and the top-level ``orders_submitted_count``.
    Both of those are OPTIONAL, and a blocked artifact can record its accepted
    order in ``broker_outcomes`` or in ``counts.submitted`` alone -- which is
    exactly why ``MorningDCAExecutor._existing_submitted_run_state()`` reads
    outcome-only evidence too. Checking only the first two classified such an
    artifact as an empty date-gate stub and dropped the run, losing its
    API/recovery cost from the monthly rollup despite the broker order it
    records (Codex review discussion_r3868141060: "Retain blocked runs with
    outcome-only order evidence").

    A scheduled run can also block AFTER it evaluated every signal but BEFORE
    it ever reached placement -- a raw-persistence failure or an account-
    pinning mismatch are the concrete cases -- carrying real connector/API
    cost (a quote and a signal computation per ticker) with no order-bearing
    evidence anywhere, since none was ever attempted. ``build_artifacts()``
    always populates ``counts.evaluated``/``signal_rows`` before any such
    failure is classified, so a nonzero evaluated count or a non-empty
    ``signal_rows`` is checked first and is evidence on its own, independent
    of whether any order was ever placed (Codex review: "Retain evaluated
    blocked runs without usage proxies").

    A count that parses as a float but is not a finite, non-negative number
    (``"NaN"``, ``"Infinity"``, or a negative sentinel like ``-1``) is treated
    the same as an unparseable one: it is not affirmative evidence the run did
    no work, so the run is retained rather than silently classified as an
    empty stub and dropped from the rollup (Codex review: "Retain blocked runs
    with invalid submitted counts").
    """
    if data.get("status") != "blocked":
        return False
    if isinstance(data.get("usage_proxy"), dict):
        return False
    counts = data.get("counts")
    try:
        evaluated = float(counts.get("evaluated") or 0) if isinstance(counts, dict) else 0.0
    except (TypeError, ValueError):
        return False
    if not math.isfinite(evaluated) or evaluated < 0:
        return False
    if evaluated > 0:
        return False
    signal_rows = data.get("signal_rows")
    if isinstance(signal_rows, list) and any(isinstance(row, dict) for row in signal_rows):
        return False
    for count in (
        data.get("orders_submitted_count"),
        counts.get("submitted") if isinstance(counts, dict) else None,
    ):
        try:
            parsed_count = float(count or 0)
        except (TypeError, ValueError):
            # An unparseable count is not evidence of absence -- keep the run
            # and let the aggregation ignore what it cannot read.
            return False
        if not math.isfinite(parsed_count) or parsed_count < 0:
            # A NaN/Infinity/negative count is float-convertible but no more
            # meaningful than an unparseable one -- "NaN > 0" and "-1 > 0"
            # are both False, so treating it as a real count read a corrupt
            # value as proof zero orders were submitted and dropped a run
            # that might carry other evidence (Codex review: "Retain blocked
            # runs with invalid submitted counts"). Same fail-closed handling
            # as the unparseable case above.
            return False
        if parsed_count > 0:
            return False
    outcomes = data.get("broker_outcomes")
    if isinstance(outcomes, dict):
        for outcome in outcomes.values():
            if not isinstance(outcome, dict):
                continue
            if outcome.get("order_id"):
                return False
            status = outcome.get("status")
            if isinstance(status, str) and status in _ORDER_BEARING_STATUSES:
                return False
    candidates = data.get("buy_candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("order_id"):
                return False
            status = candidate.get("status")
            # isinstance first: a malformed status must not raise here (a
            # corrupt artifact would otherwise take down the whole rollup).
            if isinstance(status, str) and status in _ORDER_BEARING_STATUSES:
                return False
    return True


def collect_month_run_states(
    root: str | Path,
    year: int,
    month: int,
) -> List[Dict[str, Any]]:
    """Load run_state artifacts under ``root`` whose trading_day is in the month.

    Files that cannot be parsed as JSON or lack a matching ``trading_day`` are
    skipped so a single bad artifact never breaks the monthly rollup. Minimal
    ``status: "blocked"`` stubs written for date-gated days are also skipped —
    they carry no usage data and are not analyzed runs — but a blocked state
    that DID real work is kept; see :func:`_is_empty_blocked_stub`.
    """
    prefix = _month_prefix(year, month)
    run_states: List[Dict[str, Any]] = []
    for path in find_run_state_files(root):
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("skipping unreadable run-state artifact %s: %s", path, exc)
            continue

        # ⚡ Bolt Optimization: Fast JSON filtering via substring check.
        # Skip the expensive JSON parse if the file doesn't match the month prefix.
        if prefix not in raw_text:
            continue

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.warning("skipping unparseable run-state artifact %s: %s", path, exc)
            continue

        if _is_empty_blocked_stub(data):
            continue
        trading_day = data.get("trading_day")
        if isinstance(trading_day, str) and trading_day.startswith(prefix):
            run_states.append(data)
    return run_states


def _fallback_usage_proxy(data: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """A conservative usage proxy for a run :func:`_is_empty_blocked_stub`
    retains that never received one at all.

    A run that evaluated every signal and then blocked BEFORE reaching
    placement (a raw-persistence failure or an account-pinning mismatch)
    carries no ``usage_proxy`` block -- that field is populated by a
    separate, later process this module does not control -- so its real
    connector cost was otherwise completely invisible to every totaled field
    even after ``_is_empty_blocked_stub()`` started retaining the run itself
    (Codex review: "Derive usage for evaluated blocked runs").

    Only ``tickers_scanned``/``signal_rows_written`` are derived here -- the
    ONE thing provably true from ``signal_rows``/``counts.evaluated`` (one
    row per evaluated ticker) -- rather than guessing at connector-call
    attribution (finance vs. Robinhood, notifications, artifact bytes) this
    shape cannot support without fabricating the exact precision the proxy
    disclaimer explicitly promises against. Returns ``None`` when neither
    field yields a usable count, so a genuine date-gate stub still
    contributes nothing.
    """
    signal_rows = data.get("signal_rows")
    scanned = len(signal_rows) if isinstance(signal_rows, list) else 0
    if scanned <= 0:
        counts = data.get("counts")
        try:
            scanned = int(float(counts.get("evaluated") or 0)) if isinstance(counts, dict) else 0
        except (TypeError, ValueError, OverflowError):
            # _is_empty_blocked_stub() deliberately RETAINS a blocked run
            # whose counts.evaluated is "Infinity" (or any other
            # float-convertible-but-not-finite value) as real evidence
            # rather than an empty stub -- but int(float("inf")) raises
            # OverflowError, which int(float(...)) on a merely unparseable
            # string does not. Catching only TypeError/ValueError let that
            # exact malformed count crash the whole monthly rollup instead
            # of being ignored conservatively here, the same way the
            # unparseable case already is (Codex review: "Handle infinite
            # evaluated counts in the fallback proxy").
            scanned = 0
    if scanned <= 0:
        return None
    return {"tickers_scanned": float(scanned), "signal_rows_written": float(scanned)}


def aggregate_usage(
    run_states: Sequence[Dict[str, Any]],
    *,
    month_label: str,
) -> Dict[str, Any]:
    """Aggregate usage proxies across run_states into a component breakdown.

    Returns totals (summed numeric proxies), a per-component list with estimated
    proxy units, the count of runs that required manual recovery, and the proxy
    disclaimer. Runs missing a ``usage_proxy`` block contribute nothing, UNLESS
    :func:`_fallback_usage_proxy` can derive a conservative one from evaluated
    signal counts -- see its own docstring.
    """
    totals: Dict[str, float] = {field: 0.0 for field in _NUMERIC_PROXY_FIELDS}
    manual_recovery_runs = 0

    valid_usages = []
    for rs in run_states:
        usage = rs.get("usage_proxy")
        if isinstance(usage, dict):
            valid_usages.append(usage)
            if usage.get("manual_recovery_required"):
                manual_recovery_runs += 1
            continue
        fallback = _fallback_usage_proxy(rs)
        if fallback is not None:
            valid_usages.append(fallback)

    # Field-wise sum() is retained here (rather than a manual nested loop) because
    # it benchmarks faster for the larger record counts this rollup sees in practice.
    for field in _NUMERIC_PROXY_FIELDS:
        totals[field] = sum(_num(u.get(field)) for u in valid_usages)

    components: List[Dict[str, Any]] = []
    estimated_total = 0.0
    for name, fields, weight in COMPONENT_DEFINITIONS:
        count = 0.0
        for field in fields:
            count += totals.get(field, 0.0)
        estimated = round(count * weight, 3)
        estimated_total += estimated
        components.append(
            {
                "component": name,
                "fields": list(fields),
                "count": round(count, 3),
                "proxy_weight": weight,
                "estimated_proxy_units": estimated,
            }
        )

    return {
        "title": COST_TRACKER_TITLE,
        "month_label": month_label,
        "run_count": len(run_states),
        "totals": {field: round(value, 3) for field, value in totals.items()},
        "manual_recovery_runs": manual_recovery_runs,
        "components": components,
        "estimated_total_proxy_units": round(estimated_total, 3),
        "disclaimer": PROXY_DISCLAIMER,
    }


def build_cost_tracker_markdown(aggregate: Dict[str, Any]) -> str:
    """Render the "Automation Cost Tracker" markdown section from an aggregate."""
    totals = aggregate.get("totals") or {}
    components = aggregate.get("components") or []

    lines: List[str] = []
    lines.append(f"## {COST_TRACKER_TITLE} — {aggregate.get('month_label', 'unknown')}")
    lines.append("")
    lines.append(f"> {aggregate.get('disclaimer', PROXY_DISCLAIMER)}")
    lines.append("")
    lines.append(f"- Runs analyzed: {aggregate.get('run_count', 0)}")
    lines.append(f"- Runs needing manual recovery: {aggregate.get('manual_recovery_runs', 0)}")
    lines.append(
        f"- Estimated total proxy units (not credits): "
        f"{aggregate.get('estimated_total_proxy_units', 0.0)}"
    )
    lines.append("")
    lines.append("### By component (estimated, proxy — not credits)")
    lines.append("")
    lines.append("| Component | Count | Proxy weight | Est. proxy units |")
    lines.append("| --- | --- | --- | --- |")
    for comp in components:
        lines.append(
            f"| {comp['component']} | {comp['count']} | {comp['proxy_weight']} | "
            f"{comp['estimated_proxy_units']} |"
        )
    if not components:
        lines.append("| _no runs_ | 0 | - | 0 |")
    lines.append("")
    lines.append("### Raw monthly usage proxies")
    lines.append("")
    lines.append("| Proxy metric | Total |")
    lines.append("| --- | --- |")
    for field in _NUMERIC_PROXY_FIELDS:
        lines.append(f"| {field} | {totals.get(field, 0.0)} |")
    lines.append("")
    return "\n".join(lines)


def build_cost_tracker_payload(
    aggregate: Dict[str, Any],
    page_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a connector-agnostic Notion payload for the cost-tracker section.

    ``page_id`` is supplied by the caller/scheduler and never embedded here.
    """
    return {
        "title": COST_TRACKER_TITLE,
        "page_id": page_id,
        "month_label": aggregate.get("month_label"),
        "markdown": build_cost_tracker_markdown(aggregate),
        "aggregate": aggregate,
        "value_type": "estimate_proxy",
    }


def generate_cost_tracker(
    root: str | Path,
    year: int,
    month: int,
    output_dir: str | Path,
    page_id: Optional[str] = None,
) -> Dict[str, str]:
    """Aggregate a month of run artifacts and write markdown + payload; return paths."""
    month_label = _month_prefix(year, month)
    run_states = collect_month_run_states(root, year, month)
    aggregate = aggregate_usage(run_states, month_label=month_label)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"automation_cost_tracker_{month_label}.md"
    atomic_write_text(md_path, build_cost_tracker_markdown(aggregate))
    payload_path = out / f"automation_cost_tracker_{month_label}.json"
    atomic_write_text(
        payload_path,
        json.dumps(build_cost_tracker_payload(aggregate, page_id=page_id), indent=2, sort_keys=True),
    )
    return {"markdown": str(md_path), "payload": str(payload_path)}
