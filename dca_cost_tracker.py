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
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
    if root_path.is_file() and root_path.name == "run_state.json":
        return [root_path]
    return sorted(root_path.rglob("run_state.json"))


def _month_prefix(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def collect_month_run_states(
    root: str | Path,
    year: int,
    month: int,
) -> List[Dict[str, Any]]:
    """Load run_state artifacts under ``root`` whose trading_day is in the month.

    Files that cannot be parsed as JSON or lack a matching ``trading_day`` are
    skipped so a single bad artifact never breaks the monthly rollup. Minimal
    ``status: "blocked"`` stubs written for date-gated days are also skipped —
    they carry no usage data and are not analyzed runs.
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

        if data.get("status") == "blocked":
            continue
        trading_day = data.get("trading_day")
        if isinstance(trading_day, str) and trading_day.startswith(prefix):
            run_states.append(data)
    return run_states


def aggregate_usage(
    run_states: Sequence[Dict[str, Any]],
    *,
    month_label: str,
) -> Dict[str, Any]:
    """Aggregate usage proxies across run_states into a component breakdown.

    Returns totals (summed numeric proxies), a per-component list with estimated
    proxy units, the count of runs that required manual recovery, and the proxy
    disclaimer. Runs missing a ``usage_proxy`` block contribute nothing.
    """
    totals: Dict[str, float] = {field: 0.0 for field in _NUMERIC_PROXY_FIELDS}
    manual_recovery_runs = 0
    for rs in run_states:
        usage = rs.get("usage_proxy")
        if not isinstance(usage, dict):
            continue
        # ⚡ Bolt Optimization: Loop explicitly over target keys to avoid scanning
        # arbitrary/noisy usage dictionary keys unnecessarily.
        for field in _NUMERIC_PROXY_FIELDS:
            if field in usage:
                totals[field] += _num(usage[field])
        if usage.get("manual_recovery_required"):
            manual_recovery_runs += 1

    components: List[Dict[str, Any]] = []
    estimated_total = 0.0
    for name, fields, weight in COMPONENT_DEFINITIONS:
        count = sum(totals.get(field, 0.0) for field in fields)
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
    md_path.write_text(build_cost_tracker_markdown(aggregate), encoding="utf-8")
    payload_path = out / f"automation_cost_tracker_{month_label}.json"
    payload_path.write_text(
        json.dumps(build_cost_tracker_payload(aggregate, page_id=page_id), indent=2, sort_keys=True),
        encoding="utf-8"
    )
    return {"markdown": str(md_path), "payload": str(payload_path)}
