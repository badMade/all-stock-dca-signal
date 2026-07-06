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
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def load_run_state(path: str | Path) -> Dict[str, Any]:
    """Load a ``run_state.json`` artifact produced by the live-run path."""
    return json.loads(Path(path).read_text())


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
    """Return buy candidates that never produced a fill."""
    filled_tickers = {f.get("ticker") or f.get("symbol") for f in fills}
    missing: List[Dict[str, Any]] = []
    for candidate in buy_candidates:
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
    priced = [r for r in slippage_rows if not r["target_price_missing"] and r["slippage_pct"] is not None]
    missing_target = [r for r in slippage_rows if r["target_price_missing"]]

    slippage_values = [r["slippage_pct"] for r in priced]
    avg_slippage = round(sum(slippage_values) / len(slippage_values), 4) if slippage_values else None
    max_slippage = round(max(slippage_values), 4) if slippage_values else None
    min_slippage = round(min(slippage_values), 4) if slippage_values else None

    candidate_count = len(buy_candidates)
    filled_count = len(slippage_rows)
    coverage_pct = round(filled_count / candidate_count * 100.0, 2) if candidate_count else None

    budget = run_state.get("budget") or {}
    return {
        "run_id": run_state.get("run_id"),
        "trading_day": run_state.get("trading_day"),
        "generated_at": run_state.get("generated_at"),
        "broker": run_state.get("broker"),
        "dry_run": run_state.get("dry_run"),
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
            "day_notional_usd": budget.get("day_notional_usd"),
            "planned_notional_usd": round(
                sum(_to_float(c.get("planned_amount_usd")) or 0.0 for c in buy_candidates), 2
            ),
            "carry_forward_reserve_usd": budget.get("carry_forward_reserve_usd"),
            "remaining_spend_today_usd": budget.get("remaining_spend_today_usd"),
            "week_notional_usd": budget.get("week_notional_usd"),
            "weekly_notional_cap_usd": budget.get("weekly_notional_cap_usd"),
        },
        "manual_review_tickers": run_state.get("manual_review_tickers", []),
    }


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
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def generate_reports(
    run_state: Dict[str, Any],
    fills: Sequence[Dict[str, Any]],
    output_dir: str | Path,
) -> Dict[str, str]:
    """Generate all four post-trade report files; return a map of their paths."""
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
    report_path.write_text(report_md)

    summary_path = out / "latest_run_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True))

    return {
        "slippage_ranked": str(slippage_path),
        "missing_fills_report": str(missing_path),
        "slippage_and_coverage_report": str(report_path),
        "latest_run_summary": str(summary_path),
    }
