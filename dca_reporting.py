"""Post-trade reporting for the near-close graded DCA strategy.

Reads the structured live-run artifacts written by ``near_close_dca.run_once``
(``run_state.json`` and its CSVs) plus broker fill/order data, then emits:

    - ``slippage_ranked.csv``          per-fill slippage, ranked worst-first
    - ``missing_fills_report.csv``     buy candidates that never produced a fill
    - ``slippage_and_coverage_report.md`` human-readable summary
    - ``latest_run_summary.json``      compact machine-readable summary

Slippage is computed only when a ``target_price`` exists for the candidate. When
it is absent the row is emitted with ``target_price_missing=True`` and null
slippage rather than a fabricated number, so downstream consumers never treat a
missing target as zero slippage.

This module is broker-agnostic: it takes fills as plain dicts. Nothing here
places trades, calls a broker, or reads credentials.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from dca_fileio import atomic_open_w, atomic_write_text


def load_run_state(path: str | Path) -> Dict[str, Any]:
    """Load a ``run_state.json`` artifact produced by the live-run path."""
    return json.loads(Path(path).read_text())


def _is_manual_run_state(run_state: Dict[str, Any]) -> bool:
    """True if a run_state is a manual run per its run_classification block."""
    rc = run_state.get("run_classification") if isinstance(run_state, dict) else None
    if not isinstance(rc, dict):
        return False
    return rc.get("run_origin") == "manual" or rc.get("manual_run") is True


def _drop_excluded_fills(
    fills: Sequence[Dict[str, Any]], excluded_tickers: frozenset[str]
) -> List[Dict[str, Any]]:
    """Return fills filtering out any that belong to an excluded ticker."""
    if not excluded_tickers:
        return list(fills)
    return [
        f for f in fills
        if (f.get("ticker") or f.get("symbol")) not in excluded_tickers
    ]


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_slippage_rows(
    buy_candidates: Sequence[Dict[str, Any]],
    fills: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Join candidates to fills by ticker and compute per-fill slippage.

    ``fills`` items are expected to carry at least ``ticker`` and ``fill_price``
    (``filled_qty``/``filled_avg_price``/``order_id`` are used when present).
    Slippage is only calculated when the candidate carried a ``target_price``;
    otherwise the row is flagged ``target_price_missing`` with null slippage.

    Positive ``slippage_pct`` means the fill price was above target (paid up);
    negative means filled below target (favorable for a buy).
    """
    fills_by_ticker: Dict[str, Dict[str, Any]] = {}
    for fill in fills:
        ticker = fill.get("ticker") or fill.get("symbol")
        if not ticker:
            continue

        qty = _to_float(fill.get("filled_qty")) or 0.0
        price = _to_float(fill.get("fill_price") or fill.get("filled_avg_price")) or 0.0

        if ticker not in fills_by_ticker:
            fills_by_ticker[ticker] = {"filled_qty": 0.0, "total_cost": 0.0, "order_id": fill.get("order_id")}
        
        entry = fills_by_ticker[ticker]
        entry["filled_qty"] += qty
        entry["total_cost"] += qty * price

    for data in fills_by_ticker.values():
        if data["filled_qty"] > 0:
            data["filled_avg_price"] = data["total_cost"] / data["filled_qty"]

    rows: List[Dict[str, Any]] = []
    for candidate in buy_candidates:
        ticker = candidate.get("ticker")
        if not isinstance(ticker, str):
            continue
        fill = fills_by_ticker.get(ticker)
        if fill is None:
            continue

        target_price = _to_float(candidate.get("target_price"))
        target_missing = candidate.get("target_price_missing", target_price is None) or target_price is None
        fill_price = _to_float(fill.get("fill_price") or fill.get("filled_avg_price"))

        slippage_abs: Optional[float] = None
        slippage_pct: Optional[float] = None
        if not target_missing and target_price is not None and fill_price is not None:
            slippage_abs = fill_price - target_price
            if target_price != 0:
                slippage_pct = (fill_price / target_price - 1.0) * 100.0

        rows.append(
            {
                "ticker": ticker,
                "planned_amount_usd": _to_float(candidate.get("planned_amount_usd")),
                "buy_multiplier": _to_float(candidate.get("buy_multiplier")),
                "target_price": target_price,
                "target_price_missing": bool(target_missing),
                "fill_price": fill_price,
                "filled_qty": _to_float(fill.get("filled_qty")),
                "order_id": fill.get("order_id") or candidate.get("order_id"),
                "slippage_abs": None if slippage_abs is None else round(slippage_abs, 6),
                "slippage_pct": None if slippage_pct is None else round(slippage_pct, 4),
            }
        )
    return rows


def fills_from_order_history_payload(
    payload: Any, *, order_id_to_ticker: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    """Normalize a persisted Robinhood order-history payload into ``fills`` dicts.

    Added 2026-07-10: the 3:35 slippage task persists broker order-history
    responses (e.g. ``orders_after_place.json``) before slippage computation and
    computes average fill price / executions from the saved payloads rather than
    re-querying the broker. This accepts:

      * a bare list of order dicts,
      * ``{"orders": [...]}`` / ``{"results": [...]}`` broker envelopes,
      * a normalized connector pointer ``{"output_path": ..., "data": <any above>}``
        (see scripts/run_classification.normalize_connector_payload), unwrapped
        recursively in case the pointer wraps another ``{"data": ...}`` envelope.

    Each order contributes a fill carrying ``ticker``, ``order_id``,
    ``filled_qty`` (summed executed quantity), and ``filled_avg_price``
    (quantity-weighted average of its executions, falling back to the order's own
    average_price/price). An order is skipped when:

      * it has no confirmed executed quantity -- ``executions``, ``filled_qty``,
        or ``cumulative_quantity``. The order's own top-level ``quantity`` is the
        originally *requested* size, not a confirmation of execution, so an
        unfilled/open/rejected order is never reported as a fill;
      * it has an executed quantity but no resolvable price anywhere (no priced
        executions, no ``filled_avg_price``/``average_price``/``price``) --
        reporting a priced-at-zero fill would understate cost and show a false
        -100% slippage rather than a missing/unpriced fill.

    ``order_id_to_ticker`` lets the caller supply ticker context for the
    "live adapter" order shape (``id``/``cumulative_quantity``/``average_price``),
    which is often symbol- or order-id-scoped and carries no inline ticker field.
    """
    orders = _order_list_from_payload(payload)
    fills: List[Dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_id = order.get("order_id") or order.get("id") or order.get("broker_order_id")
        ticker = order.get("ticker") or order.get("symbol") or order.get("instrument_symbol")
        if not ticker and order_id_to_ticker:
            ticker = order_id_to_ticker.get(order_id)
        if not ticker:
            continue

        executions = order.get("executions")
        exec_qty = 0.0
        exec_cost = 0.0
        if isinstance(executions, list):
            for ex in executions:
                if not isinstance(ex, dict):
                    continue
                q = _to_float(ex.get("quantity") or ex.get("filled_qty")) or 0.0
                p = _to_float(ex.get("price") or ex.get("fill_price"))
                # Skip executions without a positive price entirely (neither
                # quantity nor cost contributes) rather than coercing a
                # missing/blank price to 0.0: folding a zero-priced execution
                # into the weighted average turns an unpriced execution into
                # false coverage and a false -100% slippage number instead of
                # a missing/unpriced fill. A corrupted broker record can carry
                # Infinity/NaN, which passes `<= 0` unscathed (NaN compares
                # False to everything; Infinity is > 0) -- isfinite() closes
                # that gap.
                if q <= 0 or not math.isfinite(q) or p is None or p <= 0 or not math.isfinite(p):
                    continue
                exec_qty += q
                exec_cost += q * p

        # Prefer confirmed-executed quantity fields; bare "quantity" is accepted
        # only as a last resort when state=="filled" guarantees full execution.
        qty = exec_qty or _to_float(order.get("filled_qty") or order.get("cumulative_quantity")) or 0.0
        if not math.isfinite(qty):
            qty = 0.0
        if qty <= 0:
            # Mirror RobinhoodBroker.get_order_fill(): accept bare `quantity` only
            # when the order is fully filled and a fill price is present. Restricting
            # to state=="filled" avoids inflating coverage for partially_filled orders
            # where `quantity` is the requested size, not the executed size.
            order_state = order.get("state", "")
            avg_price_hint = _to_float(order.get("average_price") or order.get("price"))
            if (
                order_state == "filled"
                and avg_price_hint
                and avg_price_hint > 0
                and math.isfinite(avg_price_hint)
            ):
                qty = _to_float(order.get("quantity")) or 0.0
                if not math.isfinite(qty):
                    qty = 0.0
        if qty <= 0:
            continue

        if exec_qty > 0:
            # exec_qty > 0 implies exec_cost > 0 (both accumulate only when p > 0).
            avg_price = exec_cost / exec_qty
        else:
            avg_price = _to_float(
                order.get("filled_avg_price")
                or order.get("average_price")
                or order.get("price")
            )
        # Skip fills with no resolvable price or a zero/negative price:
        # compute_slippage_rows() coerces missing prices to 0.0, which would
        # produce spurious -100% slippage rather than a missing/unpriced fill.
        # A corrupted broker record's average_price/price can be Infinity or
        # NaN, which passes `<= 0` unscathed -- isfinite() closes that gap
        # (Codex review, PR #88: "Reject non-finite broker fill prices").
        if avg_price is None or avg_price <= 0 or not math.isfinite(avg_price):
            continue

        fills.append(
            {
                "ticker": ticker,
                "order_id": order_id,
                "filled_qty": qty,
                "filled_avg_price": avg_price,
                "fill_price": avg_price,
                "executions": len(executions) if isinstance(executions, list) else None,
            }
        )
    return fills


def _unwrap_payload(payload: Any) -> Any:
    """Unwrap normalized connector payload envelopes recursively.

    ``save_raw_input()`` can normalize a raw broker envelope that is itself
    ``{"data": {...}}``, producing a doubly (or more) nested pointer such as
    ``{"output_path": ..., "data": {"data": {"orders": [...]}}}``. Keep
    unwrapping ``data`` layers -- not just one -- until the payload stops
    being an unwrappable dict.
    """
    seen_ids: set[int] = set()
    while isinstance(payload, dict) and "data" in payload:
        if id(payload) in seen_ids:
            break  # defend against a cyclic/self-referential payload
        seen_ids.add(id(payload))
        payload = payload["data"]
    return payload


def _order_list_from_payload(payload: Any) -> List[Any]:
    """Extract the list of order dicts from the accepted payload shapes."""
    payload = _unwrap_payload(payload)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("orders", "results", "order_history"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # Single-order shape: {"order": {...}} (e.g. order-ID-scoped Robinhood response).
        order = payload.get("order")
        if isinstance(order, dict):
            return [order]
        # Bare single-order object: top-level dict that IS an order (has id/order_id).
        if payload.get("id") or payload.get("order_id"):
            return [payload]
    return []


def rank_slippage(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank fills worst-first by slippage percent; missing-target rows sort last."""

    def sort_key(row: Dict[str, Any]) -> tuple[int, float]:
        pct = row.get("slippage_pct")
        if pct is None:
            return (1, 0.0)
        return (0, -pct)

    return sorted(rows, key=sort_key)


def build_missing_fills(
    buy_candidates: Sequence[Dict[str, Any]],
    fills: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return buy candidates that never produced a fill.

    Candidates flagged ``top_up_skipped`` are excluded: the same-day top-up guard
    intentionally ordered $0 for them because an earlier same-day run already
    covered the ticker, so a missing fill there is expected, not a coverage gap.
    """
    filled_tickers = {f.get("ticker") or f.get("symbol") for f in fills}
    missing: List[Dict[str, Any]] = []
    for candidate in buy_candidates:
        if candidate.get("top_up_skipped"):
            continue
        ticker = candidate.get("ticker")
        if ticker in filled_tickers:
            continue
        missing.append(
            {
                "ticker": ticker,
                "planned_amount_usd": _to_float(candidate.get("planned_amount_usd")),
                "buy_multiplier": _to_float(candidate.get("buy_multiplier")),
                "target_price": _to_float(candidate.get("target_price")),
                "target_price_missing": bool(candidate.get("target_price_missing", False)),
                "status": candidate.get("status"),
                "skip_reason": candidate.get("skip_reason") or "no fill recorded",
                "order_id": candidate.get("order_id"),
            }
        )
    return missing


def build_latest_run_summary(
    run_state: Dict[str, Any],
    slippage_rows: Sequence[Dict[str, Any]],
    missing_fills: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the compact ``latest_run_summary.json`` structure.

    Aggregates coverage and execution-quality metrics from the run state and the
    computed slippage rows. Slippage aggregates count only rows with a present
    target price; ``target_price_missing`` rows are surfaced separately.
    """
    buy_candidates = run_state.get("buy_candidates") or []
    # Top-up-guard candidates intentionally bought $0 (already covered by an earlier
    # same-day run) are excluded from the coverage universe; otherwise a fully
    # offset scheduled run would report every offset ticker as a missing fill.
    coverage_candidates = [c for c in buy_candidates if not c.get("top_up_skipped")]
    priced = [r for r in slippage_rows if not r["target_price_missing"] and r["slippage_pct"] is not None]
    missing_target = [r for r in slippage_rows if r["target_price_missing"]]

    slippage_values = [r["slippage_pct"] for r in priced]
    avg_slippage = round(sum(slippage_values) / len(slippage_values), 4) if slippage_values else None
    max_slippage = round(max(slippage_values), 4) if slippage_values else None
    min_slippage = round(min(slippage_values), 4) if slippage_values else None

    candidate_count = len(coverage_candidates)
    filled_count = len(slippage_rows)
    coverage_pct = round(filled_count / candidate_count * 100.0, 2) if candidate_count else None

    budget = run_state.get("budget") or {}

    # ⚡ Bolt Optimization: Pre-compute planned_notional_usd outside the dict literal
    # to avoid generator-expression overhead during dict construction in this hot path.
    planned_notional = 0.0
    for c in buy_candidates:
        planned_notional += _to_float(c.get("planned_amount_usd")) or 0.0

    # run_summary.json MUST mirror the SAME non-null run_classification object as
    # run_state.json for scheduled live runs. The Jul 22 2026 3:20 incident wrote
    # a valid run_state.run_classification but a null run_summary.run_classification,
    # which broke scheduled-vs-manual attribution downstream. When the run_state
    # classification is missing, stamp an artifact_contract_failure instead of
    # silently emitting null.
    run_classification = run_state.get("run_classification")
    if isinstance(run_classification, dict):
        summary_classification: Any = dict(run_classification)
        contract_failure: Optional[Dict[str, Any]] = None
    else:
        summary_classification = None
        contract_failure = {
            "failure": "artifact_contract_failure",
            "reason": "run_state run_classification is null or absent; run_summary "
            "cannot mirror a non-null classification",
        }
    summary: Dict[str, Any] = {
        "run_id": run_state.get("run_id"),
        "trading_day": run_state.get("trading_day"),
        "generated_at": run_state.get("generated_at"),
        "broker": run_state.get("broker"),
        "dry_run": run_state.get("dry_run"),
        "run_classification": summary_classification,
        "coverage": {
            "buy_candidates": candidate_count,
            "fills": filled_count,
            "missing_fills": len(missing_fills),
            "coverage_pct": coverage_pct,
        },
        "execution_quality": {
            "priced_fills": len(priced),
            "target_price_missing": len(missing_target),
            "avg_slippage_pct": avg_slippage,
            "max_slippage_pct": max_slippage,
            "min_slippage_pct": min_slippage,
            "worst_ticker": rank_slippage(priced)[0]["ticker"] if priced else None,
        },
        "budget": {
            "budget_tier": budget.get("budget_tier"),
            "day_notional_usd": budget.get("day_notional_usd"),
            "planned_notional_usd": round(planned_notional, 2),
            "daily_cap_usd": budget.get("daily_cap_usd"),
            "weekly_rollover_reserve_usd": budget.get("weekly_rollover_reserve_usd"),
            "remaining_spend_today_usd": budget.get("remaining_spend_today_usd"),
            "week_notional_usd": budget.get("week_notional_usd"),
            "weekly_cap_usd": budget.get("weekly_cap_usd"),
        },
        "manual_review_tickers": run_state.get("manual_review_tickers", []),
    }
    if contract_failure is not None:
        summary["artifact_contract_failure"] = contract_failure
    return summary


def build_coverage_report_md(
    summary: Dict[str, Any],
    slippage_rows: Sequence[Dict[str, Any]],
    missing_fills: Sequence[Dict[str, Any]],
) -> str:
    """Render the human-readable slippage & coverage markdown report."""
    cov = summary["coverage"]
    exq = summary["execution_quality"]
    lines: List[str] = []
    lines.append(f"# Slippage & Coverage Report — {summary.get('trading_day', 'unknown')}")
    lines.append("")
    lines.append(f"- Run: `{summary.get('run_id')}` (broker: {summary.get('broker')}, dry_run: {summary.get('dry_run')})")
    lines.append(f"- Buy candidates: {cov['buy_candidates']}")
    coverage = f"{cov['coverage_pct']}%" if cov["coverage_pct"] is not None else "n/a"
    lines.append(f"- Fills: {cov['fills']}  |  Missing fills: {cov['missing_fills']}  |  Coverage: {coverage}")
    lines.append(
        f"- Priced fills: {exq['priced_fills']}  |  target_price_missing: {exq['target_price_missing']}"
    )
    avg_slip = exq.get("avg_slippage_pct")
    max_slip = exq.get("max_slippage_pct")
    min_slip = exq.get("min_slippage_pct")
    avg_slip_str = f"{avg_slip}%" if avg_slip is not None else "n/a"
    max_slip_str = f"{max_slip}%" if max_slip is not None else "n/a"
    min_slip_str = f"{min_slip}%" if min_slip is not None else "n/a"
    lines.append(
        f"- Slippage %: avg {avg_slip_str}  |  max {max_slip_str}  |  min {min_slip_str}"
    )
    lines.append("")

    lines.append("## Slippage (ranked worst-first)")
    lines.append("")
    if slippage_rows:
        lines.append("| Ticker | Target | Fill | Slippage % | Note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in rank_slippage(slippage_rows):
            note = "target_price_missing" if row["target_price_missing"] else ""
            target = row["target_price"] if row["target_price"] is not None else "n/a"
            fill = row["fill_price"] if row["fill_price"] is not None else "n/a"
            slip = f"{row['slippage_pct']}%" if row["slippage_pct"] is not None else "n/a"
            lines.append(
                f"| {row['ticker']} | {target} | {fill} | {slip} | {note} |"
            )
    else:
        lines.append("_No fills to rank._")
    lines.append("")

    lines.append("## Missing fills")
    lines.append("")
    if missing_fills:
        lines.append("| Ticker | Planned $ | Status | Reason |")
        lines.append("| --- | --- | --- | --- |")
        for row in missing_fills:
            lines.append(
                f"| {row['ticker']} | {row['planned_amount_usd']} | {row['status']} | {row['skip_reason']} |"
            )
    else:
        lines.append("_No missing fills._")
    lines.append("")
    return "\n".join(lines)


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    with atomic_open_w(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def generate_reports(
    run_state: Dict[str, Any],
    fills: Sequence[Dict[str, Any]],
    output_dir: str | Path,
    *,
    excluded_tickers: Optional[frozenset[str]] = None,
    require_scheduled: bool = False,
) -> Dict[str, str]:
    """Generate all four post-trade report files; return a map of their paths.

    Run-classification policy (task 67aa802b): the scheduled post-trade sync
    passes ``require_scheduled=True`` so a manual ``run_state`` is refused rather
    than published as scheduled-live metrics, and ``excluded_tickers`` drops any fills
    whose ticker belongs to an excluded list so same-day unrelated fills in broker
    history cannot leak into the scheduled-live slippage/coverage numbers.
    """
    if require_scheduled and _is_manual_run_state(run_state):
        raise ValueError(
            "refusing to generate scheduled-live reports from a manual run_state"
        )
    fills = _drop_excluded_fills(fills, excluded_tickers or frozenset())

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    buy_candidates = run_state.get("buy_candidates", [])
    slippage_rows = compute_slippage_rows(buy_candidates, fills)
    ranked = rank_slippage(slippage_rows)
    missing_fills = build_missing_fills(buy_candidates, fills)
    summary = build_latest_run_summary(run_state, slippage_rows, missing_fills)
    report_md = build_coverage_report_md(summary, slippage_rows, missing_fills)

    slippage_path = out / "slippage_ranked.csv"
    _write_csv(
        slippage_path,
        ranked,
        [
            "ticker",
            "planned_amount_usd",
            "buy_multiplier",
            "target_price",
            "target_price_missing",
            "fill_price",
            "filled_qty",
            "order_id",
            "slippage_abs",
            "slippage_pct",
        ],
    )

    missing_path = out / "missing_fills_report.csv"
    _write_csv(
        missing_path,
        missing_fills,
        [
            "ticker",
            "planned_amount_usd",
            "buy_multiplier",
            "target_price",
            "target_price_missing",
            "status",
            "skip_reason",
            "order_id",
        ],
    )

    report_path = out / "slippage_and_coverage_report.md"
    atomic_write_text(report_path, report_md)

    summary_path = out / "latest_run_summary.json"
    atomic_write_text(summary_path, json.dumps(summary, indent=2, sort_keys=True))

    return {
        "slippage_ranked": str(slippage_path),
        "missing_fills_report": str(missing_path),
        "slippage_and_coverage_report": str(report_path),
        "latest_run_summary": str(summary_path),
    }


def _run_classification_helpers():
    """Import the run-classification contract helpers, tolerating either import
    layout (repo-root namespace package ``scripts`` or ``scripts/`` on sys.path)."""
    try:
        from scripts.run_classification import (  # type: ignore
            collect_manual_order_ids,
            select_scheduled_state_for_reporting,
        )
    except ImportError:  # scripts/ directly on sys.path
        from run_classification import (  # type: ignore
            collect_manual_order_ids,
            select_scheduled_state_for_reporting,
        )
    return collect_manual_order_ids, select_scheduled_state_for_reporting


def _model_usage_log_helper():
    """Import the model usage log writer, tolerating either import layout
    (repo-root namespace package ``scripts`` or ``scripts/`` on sys.path)."""
    try:
        from scripts.model_usage_log import log_model_usage  # type: ignore
    except ImportError:  # scripts/ directly on sys.path
        from model_usage_log import log_model_usage  # type: ignore
    return log_model_usage



@dataclass(frozen=True)
class ModelUsageConfig:
    """Model-usage-tracking overrides for :func:`generate_scheduled_reports`.

    Bundles the audit-log root and the ``log_model_usage`` cost-conscious
    defaults' overrides (see ``scripts/model_usage_log.py``) into a single
    opt-in object: ``log_root=None`` (the default) skips the audit-log
    append entirely, and the remaining fields stay unset unless the caller
    explicitly knows it used a permitted premium escalation. See
    ``docs/cost_tracking.md`` ("What Python cannot know") for the full
    contract these fields satisfy.
    """

    log_root: Optional[str | Path] = None
    model_requested: Optional[str] = None
    model_actual: Optional[str] = None
    model_routing_mode: Optional[str] = None
    reason_for_premium_model: Optional[str] = None


def generate_scheduled_reports(
    trading_day: str,
    fills: Sequence[Dict[str, Any]],
    output_dir: str | Path,
    base: Optional[str | Path] = None,
    model_usage_log_root: Optional[str | Path] = None,
    model_usage_model_requested: Optional[str] = None,
    model_usage_model_actual: Optional[str] = None,
    model_usage_model_routing_mode: Optional[str] = None,
    model_usage_reason_for_premium_model: Optional[str] = None,
    *,
    model_usage_config: Optional[ModelUsageConfig] = None,
) -> Dict[str, str]:
    """Task 67aa802b entry point: post-trade reports for the SCHEDULED run only.

    Routes the whole flow through the run-classification contract:
      * selects and validates the day's scheduled run_state (never infers a
        scheduled run from broker orders alone),
      * excludes every manual-run order ID from the broker ``fills``,
      * refuses to emit reports unless a valid scheduled state exists.

    ``fills`` is the same broker fill list ``generate_reports`` takes; ``base``
    overrides the artifact root (defaults to the contract's STATE_ROOT).
    ``model_usage_config.log_root`` is opt-in (see scripts/model_usage_log.py,
    config/scheduled_tasks.json -> model_usage_log): None (the default) skips
    the audit-log append entirely (appended even when report generation
    raises, so a failed run is still recorded). The other ``model_usage_config`` fields override
    ``log_model_usage``'s cost-conscious defaults -- this function has no
    visibility into which model/agent actually drove the run, so they stay
    unset unless the scheduler invoking this call explicitly knows it used a
    permitted premium escalation and passes them.

    The ``model_usage_log_root``/``model_usage_model_*``/
    ``model_usage_reason_for_premium_model`` parameters are the pre-existing
    interface documented at ``docs/cost_tracking.md:206-220`` and are kept
    in their original positional-or-keyword slots (unchanged) for the
    externally managed 67aa802b scheduler, which still calls this entry
    point with them -- including positionally. ``model_usage_config`` is
    the new keyword-only alternative; any of the legacy parameters that is
    passed (non-``None``) overrides the corresponding ``model_usage_config``
    field, so old and new call sites can both be satisfied.
    """
    if any(
        v is not None
        for v in (
            model_usage_log_root,
            model_usage_model_requested,
            model_usage_model_actual,
            model_usage_model_routing_mode,
            model_usage_reason_for_premium_model,
        )
    ):
        base_config = model_usage_config or ModelUsageConfig()
        model_usage_config = ModelUsageConfig(
            log_root=(
                model_usage_log_root
                if model_usage_log_root is not None
                else base_config.log_root
            ),
            model_requested=(
                model_usage_model_requested
                if model_usage_model_requested is not None
                else base_config.model_requested
            ),
            model_actual=(
                model_usage_model_actual
                if model_usage_model_actual is not None
                else base_config.model_actual
            ),
            model_routing_mode=(
                model_usage_model_routing_mode
                if model_usage_model_routing_mode is not None
                else base_config.model_routing_mode
            ),
            reason_for_premium_model=(
                model_usage_reason_for_premium_model
                if model_usage_reason_for_premium_model is not None
                else base_config.reason_for_premium_model
            ),
        )
    collect_manual_order_ids, select_scheduled = _run_classification_helpers()
    base_path = Path(base) if base is not None else None
    selection = select_scheduled(trading_day, base=base_path)
    if not selection.ok:
        raise ValueError(
            f"no valid scheduled run_state for {trading_day}: "
            + "; ".join(selection.errors)
        )
    manual_ids = collect_manual_order_ids(base_path)
    manual_ids_set = {oid for oid in manual_ids if oid}
    # Restrict fills to order IDs the classification contract actually attributed
    # to this scheduled run (selection.order_ids is already manual-id-excluded,
    # and is forced empty for a confirmation-blocked run). Without this,
    # compute_slippage_rows joins fills to candidates by ticker alone, so any
    # same-ticker broker fill unrelated to this run -- or any fill at all on a
    # confirmation-blocked run, which by contract placed zero orders -- could be
    # published as scheduled evidence. The manual_ids_set check is a redundant,
    # order-ID-scoped safety net on top of that: it must stay order-ID-based
    # (not ticker-based) so a manual order never suppresses a *different*,
    # legitimately scheduled fill that happens to trade the same ticker the
    # same day.
    scheduled_ids = set(selection.order_ids)
    fills = [
        f for f in fills
        if (f.get("order_id") or f.get("broker_order_id")) in scheduled_ids
        and (f.get("order_id") or f.get("broker_order_id")) not in manual_ids_set
    ]

    # Fallback: load fills from the persisted orders_after_place.json for any
    # scheduled order IDs not already covered by the caller-supplied fills.
    # This activates when the caller passes an empty sequence (the scheduler
    # persisted the raw payload but did not re-query the broker for this report).
    if scheduled_ids and selection.state_path:
        covered_ids = {
            f.get("order_id") or f.get("broker_order_id") for f in fills
        } - {None}
        missing_ids = scheduled_ids - covered_ids
        if missing_ids:
            raw_orders_path = Path(selection.state_path).parent / "raw" / "orders_after_place.json"
            if raw_orders_path.exists():
                try:
                    raw_payload = json.loads(raw_orders_path.read_text(encoding="utf-8"))
                    # Map every tracked order ID (order_id or broker_order_id)
                    # to its ticker so fills_from_order_history_payload() can
                    # resolve symbols for order-ID-scoped payloads without a
                    # symbol field. Covers both buy_candidates and broker_outcomes.
                    ticker_by_order_id: Dict[str, str] = {}
                    state = selection.state or {}
                    # .get(key, default) only substitutes the default when the
                    # key is ABSENT; a state with "buy_candidates": null or
                    # "broker_outcomes": null (structurally corrupted but still
                    # a valid scheduled state otherwise) would return None and
                    # raise TypeError/AttributeError below instead of simply
                    # falling back to the caller-supplied fills.
                    buy_candidates = state.get("buy_candidates")
                    if not isinstance(buy_candidates, list):
                        buy_candidates = []
                    for c in buy_candidates:
                        if not isinstance(c, dict) or not c.get("ticker"):
                            continue
                        for key in ("order_id", "broker_order_id"):
                            oid = c.get(key)
                            if oid:
                                ticker_by_order_id[oid] = c["ticker"]
                    broker_outcomes = state.get("broker_outcomes")
                    if not isinstance(broker_outcomes, dict):
                        broker_outcomes = {}
                    for tkr, oc in broker_outcomes.items():
                        if isinstance(oc, dict) and isinstance(tkr, str):
                            oid = oc.get("order_id") or oc.get("broker_order_id")
                            if oid:
                                ticker_by_order_id[oid] = tkr
                    raw_fills = fills_from_order_history_payload(
                        raw_payload, order_id_to_ticker=ticker_by_order_id
                    )
                    raw_scheduled = [
                        f for f in raw_fills
                        if (f.get("order_id") or f.get("broker_order_id")) in missing_ids
                    ]
                    fills = list(fills) + raw_scheduled
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    # A truncated or non-UTF-8 orders_after_place.json (e.g.
                    # a crash mid-write) must degrade to no raw-fill fallback,
                    # not crash the whole reporting job -- UnicodeDecodeError
                    # is a ValueError subclass, not an OSError, so it needs
                    # its own arm here (Codex review, PR #88: "Handle non-
                    # UTF-8 persisted raw orders as a fallback miss").
                    pass

    # This reporting task's own identity/artifact -- distinct from the live
    # run (selection.state_path) it is reporting ON, so the audit log points
    # an auditor at what THIS task produced (output_dir), not the afternoon
    # 15:30 live task's run_state.json. The live run's own run_id is kept in
    # `notes` as an explicit input reference.
    report_run_id = f"reporting_{trading_day}_1535ET"
    source_run_id = (selection.state or {}).get("run_id")
    log_model_usage = _model_usage_log_helper()
    routing_overrides = {}
    if model_usage_config:
        routing_overrides = {
            "model_requested": model_usage_config.model_requested,
            "model_actual": model_usage_config.model_actual,
            "model_routing_mode": model_usage_config.model_routing_mode,
            "reason_for_premium_model": model_usage_config.reason_for_premium_model,
        }
    try:
        report_paths = generate_reports(
            selection.state,
            fills,
            output_dir,
            require_scheduled=True,
        )
    finally:
        # Best-effort append even when generate_reports raises, so a failed
        # report run is still recorded per the one-entry-per-task-run contract.
        log_model_usage(
            root=model_usage_config.log_root if model_usage_config else None,
            run_id=report_run_id,
            cron_id="67aa802b",
            run_origin="scheduled",
            artifact_path=str(output_dir),
            triggered_at_utc=datetime.now(timezone.utc),
            notes=f"post-trade reports for live run {source_run_id}",
            **{k: v for k, v in routing_overrides.items() if v is not None},
        )
    return report_paths
