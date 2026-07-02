"""Weekly performance dashboard builder for the near-close graded DCA strategy.

Reads accumulated per-run artifacts (``run_state.json`` files) and, when
available, the per-run ``latest_run_summary.json`` files, then renders the full
set of Notion dashboard sections as markdown:

    - Performance snapshot
    - Graded DCA vs fixed daily DCA
    - Sector performance matrix
    - Budget and reserve effectiveness
    - Execution quality
    - Coverage and missed opportunities
    - Weekly review notes

Connector-agnostic and secret-free: the scheduled task feeds the loaded artifacts
in and forwards the rendered markdown to whatever Notion integration it owns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def load_json_files(paths: Sequence[str | Path]) -> List[Dict[str, Any]]:
    return [json.loads(Path(p).read_text()) for p in paths]


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_performance_snapshot(run_states: Sequence[Dict[str, Any]]) -> str:
    days = len(run_states)
    total_candidates = sum(_num((rs.get("counts") or {}).get("buy_candidates")) for rs in run_states)
    total_submitted = sum(_num((rs.get("counts") or {}).get("submitted")) for rs in run_states)
    total_notional = sum(_num((rs.get("budget") or {}).get("day_notional_usd")) for rs in run_states)
    planned_notional = sum(
        _num(c.get("planned_amount_usd"))
        for rs in run_states
        for c in rs.get("buy_candidates") or []
    )
    week_notional = max((_num((rs.get("budget") or {}).get("week_notional_usd")) for rs in run_states), default=0.0)
    weekly_cap = max((_num((rs.get("budget") or {}).get("weekly_notional_cap_usd")) for rs in run_states), default=0.0)

    lines = ["## Performance snapshot", ""]
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Run days | {days} |")
    lines.append(f"| Buy candidates | {int(total_candidates)} |")
    lines.append(f"| Orders submitted | {int(total_submitted)} |")
    lines.append(f"| Planned notional | ${planned_notional:.2f} |")
    lines.append(f"| Executed notional | ${total_notional:.2f} |")
    lines.append(f"| Week notional / cap | ${week_notional:.2f} / ${weekly_cap:.2f} |")
    lines.append("")
    return "\n".join(lines)


def build_graded_vs_fixed(run_states: Sequence[Dict[str, Any]]) -> str:
    """Compare graded-ladder spend to a flat 1x fixed daily DCA baseline.

    The fixed baseline buys every candidate at the 1x base amount; graded applies
    the ladder multiplier. The delta shows the extra capital the ladder deploys
    into deeper drops relative to a naive fixed-size DCA.
    """
    lines = ["## Graded DCA vs fixed daily DCA", ""]
    lines.append("| Day | Candidates | Graded $ | Fixed 1x $ | Delta $ |")
    lines.append("| --- | --- | --- | --- | --- |")
    graded_total = 0.0
    fixed_total = 0.0
    for rs in run_states:
        candidates = rs.get("buy_candidates", [])
        base = _num(rs.get("budget", {}).get("base_buy_amount_usd")) or 1.0
        graded = sum(_num(c.get("planned_amount_usd")) for c in candidates)
        fixed = base * len(candidates)
        graded_total += graded
        fixed_total += fixed
        lines.append(
            f"| {rs.get('trading_day', '?')} | {len(candidates)} | "
            f"${graded:.2f} | ${fixed:.2f} | ${graded - fixed:+.2f} |"
        )
    lines.append(
        f"| **Total** | | **${graded_total:.2f}** | **${fixed_total:.2f}** | "
        f"**${graded_total - fixed_total:+.2f}** |"
    )
    lines.append("")
    return "\n".join(lines)


def build_sector_matrix(
    run_states: Sequence[Dict[str, Any]],
    sector_map: Optional[Dict[str, str]] = None,
) -> str:
    """Aggregate planned notional and candidate counts by sector.

    ``sector_map`` maps ticker -> sector; unmapped tickers fall into 'Unclassified'.
    """
    sector_map = sector_map or {}
    by_sector: Dict[str, Dict[str, float]] = {}
    for rs in run_states:
        for candidate in rs.get("buy_candidates", []):
            ticker = candidate.get("ticker", "?")
            sector = sector_map.get(ticker, "Unclassified")
            bucket = by_sector.setdefault(sector, {"candidates": 0.0, "notional": 0.0})
            bucket["candidates"] += 1
            bucket["notional"] += _num(candidate.get("planned_amount_usd"))

    lines = ["## Sector performance matrix", ""]
    lines.append("| Sector | Candidates | Planned $ |")
    lines.append("| --- | --- | --- |")
    if by_sector:
        for sector in sorted(by_sector):
            bucket = by_sector[sector]
            lines.append(f"| {sector} | {int(bucket['candidates'])} | ${bucket['notional']:.2f} |")
    else:
        lines.append("| _no candidates_ | 0 | $0.00 |")
    lines.append("")
    return "\n".join(lines)


def build_budget_reserve_effectiveness(run_states: Sequence[Dict[str, Any]]) -> str:
    lines = ["## Budget and reserve effectiveness", ""]
    lines.append("| Day | Spent $ | Base cap $ | Reserve carry $ | Remaining $ |")
    lines.append("| --- | --- | --- | --- | --- |")
    for rs in run_states:
        budget = rs.get("budget", {})
        lines.append(
            f"| {rs.get('trading_day', '?')} | ${_num(budget.get('day_notional_usd')):.2f} | "
            f"${_num(budget.get('daily_base_cap_usd')):.2f} | "
            f"${_num(budget.get('carry_forward_reserve_usd')):.2f} | "
            f"${_num(budget.get('remaining_spend_today_usd')):.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_execution_quality(summaries: Sequence[Dict[str, Any]]) -> str:
    lines = ["## Execution quality", ""]
    lines.append("| Day | Priced fills | Missing target | Avg slip % | Max slip % | Worst |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    all_slips: List[float] = []
    for summary in summaries:
        exq = summary.get("execution_quality", {})
        avg = exq.get("avg_slippage_pct")
        if avg is not None:
            all_slips.append(avg)
        lines.append(
            f"| {summary.get('trading_day', '?')} | {exq.get('priced_fills', 0)} | "
            f"{exq.get('target_price_missing', 0)} | {avg if avg is not None else 'n/a'} | "
            f"{exq.get('max_slippage_pct', 'n/a')} | {exq.get('worst_ticker', 'n/a')} |"
        )
    week_avg = round(sum(all_slips) / len(all_slips), 4) if all_slips else "n/a"
    lines.append(f"| **Week avg** | | | **{week_avg}** | | |")
    lines.append("")
    return "\n".join(lines)


def build_coverage_missed(
    run_states: Sequence[Dict[str, Any]],
    summaries: Sequence[Dict[str, Any]],
) -> str:
    lines = ["## Coverage and missed opportunities", ""]
    lines.append("| Day | Candidates | Fills | Coverage % | Missing | Manual review |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    summary_by_day = {s.get("trading_day"): s for s in summaries}
    for rs in run_states:
        day = rs.get("trading_day")
        summary = summary_by_day.get(day, {})
        cov = summary.get("coverage", {})
        manual = rs.get("manual_review_tickers", [])
        lines.append(
            f"| {day or '?'} | {cov.get('buy_candidates', rs.get('counts', {}).get('buy_candidates', 0))} | "
            f"{cov.get('fills', 'n/a')} | {cov.get('coverage_pct', 'n/a')} | "
            f"{cov.get('missing_fills', 'n/a')} | {len(manual)} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_weekly_review_notes(
    run_states: Sequence[Dict[str, Any]],
    summaries: Sequence[Dict[str, Any]],
) -> str:
    """Auto-generate review prompts from the week's aggregates."""
    lines = ["## Weekly review notes", ""]
    total_missing = sum(_num((s.get("coverage") or {}).get("missing_fills")) for s in summaries)
    total_manual = sum(len(rs.get("manual_review_tickers") or []) for rs in run_states)
    missing_targets = sum(_num((s.get("execution_quality") or {}).get("target_price_missing")) for s in summaries)

    lines.append(f"- Runs analyzed: {len(run_states)} day(s).")
    if total_missing:
        lines.append(f"- {int(total_missing)} buy candidate(s) had no fill — review broker rejects / caps.")
    else:
        lines.append("- All buy candidates were covered by fills.")
    if missing_targets:
        lines.append(
            f"- {int(missing_targets)} fill(s) lacked a target price; slippage excluded — check signal capture."
        )
    if total_manual:
        lines.append(f"- {int(total_manual)} manual-review flag(s) across the week — reconcile finance vs Robinhood.")
    lines.append("- Add qualitative notes here before the Notion dashboard refresh.")
    lines.append("")
    return "\n".join(lines)


def build_weekly_dashboard(
    run_states: Sequence[Dict[str, Any]],
    summaries: Optional[Sequence[Dict[str, Any]]] = None,
    sector_map: Optional[Dict[str, str]] = None,
    week_label: Optional[str] = None,
) -> str:
    """Render the full weekly Notion dashboard markdown from accumulated artifacts."""
    summaries = list(summaries or [])
    title = f"# Weekly Performance Dashboard{f' — {week_label}' if week_label else ''}"
    sections = [
        title,
        "",
        build_performance_snapshot(run_states),
        build_graded_vs_fixed(run_states),
        build_sector_matrix(run_states, sector_map),
        build_budget_reserve_effectiveness(run_states),
        build_execution_quality(summaries),
        build_coverage_missed(run_states, summaries),
        build_weekly_review_notes(run_states, summaries),
    ]
    return "\n".join(sections)


def write_weekly_dashboard(
    run_states: Sequence[Dict[str, Any]],
    output_dir: str | Path,
    summaries: Optional[Sequence[Dict[str, Any]]] = None,
    sector_map: Optional[Dict[str, str]] = None,
    week_label: Optional[str] = None,
) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "weekly_performance_dashboard.md"
    path.write_text(build_weekly_dashboard(run_states, summaries, sector_map, week_label))
    return str(path)
