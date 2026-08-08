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

import functools
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from dca_fileio import atomic_write_text


def _is_manual_run_state(data: Dict[str, Any]) -> bool:
    """True if a run_state is a manual run per its run_classification block.

    The weekly Friday dashboard is scheduled-live only, so manual runs must be
    dropped before aggregation (run-classification contract). Defensive OR:
    either run_origin='manual' or manual_run=true marks it manual.
    """
    rc = data.get("run_classification") if isinstance(data, dict) else None
    if not isinstance(rc, dict):
        return False
    return rc.get("run_origin") == "manual" or rc.get("manual_run") is True


def load_scheduled_run_states(base: str | Path) -> List[Dict[str, Any]]:
    """Task 041d9d90 (weekly) entry point: load only SCHEDULED run_states.

    Globs ``<base>/scheduled_runs/**/run_state*.json`` — the ``manual_runs/``
    tree is never traversed — then routes the results through
    :func:`load_json_files`, which additionally drops any blocked stub or
    defensively manual-classified state. The Friday performance dashboard is
    scheduled-live only, so this keeps manual candidates/notional out of the
    cost-basis, reliability, and order-count metrics.
    """
    root = Path(base) / "scheduled_runs"
    paths = sorted(root.rglob("run_state*.json")) if root.exists() else []
    return load_json_files(paths)


@functools.lru_cache(maxsize=1024)
def _load_cached_json(path_str: str, mtime: float) -> Dict[str, Any]:
    return json.loads(Path(path_str).read_bytes())


def load_json_files(paths: Sequence[str | Path]) -> List[Dict[str, Any]]:
    """Load artifacts, skipping the minimal ``status: "blocked"`` run_state stubs
    written for date-gated days — they are not run days and would otherwise be
    counted into the weekly snapshot/table denominators — and skipping any
    manual run_states so manual candidates/notional never enter the
    scheduled-live weekly cost basis, reliability, or order-count metrics."""
    loaded = [_load_cached_json(str(p), os.path.getmtime(p)) for p in paths]
    return [
        data
        for data in loaded
        if data.get("status") != "blocked" and not _is_manual_run_state(data)
    ]


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0



# The base amount changed from $3 to $2 on this date (see the "Base-amount
# change (Jul 19 2026)" note in docs/strategy.md and config/scheduled_tasks.json).
# ISO date strings ("YYYY-MM-DD") compare correctly as plain strings.
_BASE_AMOUNT_CHANGE_DATE = "2026-07-19"
_PRE_CHANGE_BASE_AMOUNT_USD = 3.0


def _require_base_amount(rs: Dict[str, Any]) -> float:
    """Read the base DCA amount used for a run_state's budget.

    This is a financial config value driving the fixed-baseline comparison, so a
    missing or non-numeric key fails fast rather than silently assuming today's
    base amount. The one documented exception: a run_state dated before the
    Jul 19 2026 base-amount change that omits the field predates this field
    being tracked at all, and every such historical artifact used $3 -- that is
    a known fact, not a guess, so it is used instead of failing the report.
    """
    budget = rs.get("budget") or {}
    raw = budget.get("base_amount_usd", budget.get("base_buy_amount_usd"))
    trading_day = rs.get("trading_day", "?")
    if raw is None:
        if isinstance(trading_day, str) and trading_day < _BASE_AMOUNT_CHANGE_DATE:
            return _PRE_CHANGE_BASE_AMOUNT_USD
        raise ValueError(
            f"run_state for trading_day={trading_day!r} is missing "
            "budget.base_amount_usd/base_buy_amount_usd; refusing to guess a "
            "base DCA amount for the fixed-baseline comparison."
        )
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"run_state for trading_day={trading_day!r} has a non-numeric "
            f"budget.base_amount_usd/base_buy_amount_usd: {raw!r}"
        ) from None


def build_performance_snapshot(run_states: Sequence[Dict[str, Any]]) -> str:
    days = len(run_states)
    # ⚡ Bolt Optimization: Replace multiple O(N) generator/comprehension scans with a
    # single pass over run_states to compute all aggregates simultaneously.
    total_candidates = 0.0
    total_submitted = 0.0
    total_notional = 0.0
    planned_notional = 0.0
    week_notional = 0.0
    weekly_cap = 0.0

    for rs in run_states:
        counts = rs.get("counts") or {}
        budget = rs.get("budget") or {}

        total_candidates += _num(counts.get("buy_candidates"))
        total_submitted += _num(counts.get("submitted"))
        total_notional += _num(budget.get("day_notional_usd"))

        for c in rs.get("buy_candidates") or []:
            planned_notional += _num(c.get("planned_amount_usd"))

        wn = _num(budget.get("week_notional_usd"))
        if wn > week_notional:
            week_notional = wn

        # Fall back to the pre-policy key so historical run_state artifacts (which
        # named the cap "weekly_notional_cap_usd") still populate the snapshot.
        wc = _num(budget.get("weekly_cap_usd") or budget.get("weekly_notional_cap_usd"))
        if wc > weekly_cap:
            weekly_cap = wc

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


def _candidate_fixed_base_usd(candidate: Dict[str, Any], default_base: float) -> float:
    """Return one candidate's 1x fixed-baseline dollar amount.

    Prefers the candidate's own dynamic first-allocation base
    (``selected_base_amount_usd``, $1 or $2 -- see
    ``near_close_dca.select_first_allocation_base_usd``) so the fixed baseline
    reflects the SAME per-candidate base the graded ladder actually used (Codex
    review: "Apply the dynamic base to the analytics baseline"). Falls back to
    the run's flat ``default_base`` for candidates/artifacts that predate this
    field.
    """
    raw = candidate.get("selected_base_amount_usd")
    if raw is None:
        return default_base
    try:
        base = float(raw)
    except (TypeError, ValueError):
        return default_base
    if not math.isfinite(base) or base <= 0:
        return default_base
    return base


def build_graded_vs_fixed(run_states: Sequence[Dict[str, Any]]) -> str:
    """Compare graded-ladder spend to a flat 1x fixed daily DCA baseline.

    The fixed baseline buys every candidate at its own 1x base amount (the
    dynamic $1/$2 first-allocation base when the candidate recorded one,
    otherwise the run's flat base amount); graded applies the ladder
    multiplier. The delta shows the extra capital the ladder deploys into
    deeper drops relative to a naive fixed-size DCA.
    """
    lines = ["## Graded DCA vs fixed daily DCA", ""]
    lines.append("| Day | Candidates | Graded $ | Fixed 1x $ | Delta $ |")
    lines.append("| --- | --- | --- | --- | --- |")
    graded_total = 0.0
    fixed_total = 0.0
    for rs in run_states:
        candidates = rs.get("buy_candidates", [])
        base = _require_base_amount(rs)
        # ⚡ Bolt Optimization: Replace multiple O(N) generator passes with a
        # single manual loop over candidates to aggregate graded and fixed amounts.
        graded = 0.0
        fixed = 0.0
        for c in candidates:
            graded += _num(c.get("planned_amount_usd"))
            fixed += _candidate_fixed_base_usd(c, base)
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
    lines.append("| Day | Tier | Spent $ | Daily cap $ | Rollover reserve $ | Remaining $ |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for rs in run_states:
        budget = rs.get("budget", {})
        # Fall back to the pre-policy keys so historical run_state artifacts (which
        # named these ``daily_base_cap_usd`` / ``carry_forward_reserve_usd``) still
        # render populated caps and reserve figures.
        daily_cap = budget.get("daily_cap_usd")
        if daily_cap is None:
            daily_cap = budget.get("daily_base_cap_usd")
        reserve = budget.get("weekly_rollover_reserve_usd")
        if reserve is None:
            reserve = budget.get("carry_forward_reserve_usd")
        lines.append(
            f"| {rs.get('trading_day', '?')} | {budget.get('budget_tier') or 'n/a'} | "
            f"${_num(budget.get('day_notional_usd')):.2f} | "
            f"${_num(daily_cap):.2f} | "
            f"${_num(reserve):.2f} | "
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
        exq = summary.get("execution_quality") or {}
        avg = exq.get("avg_slippage_pct")
        if avg is not None:
            all_slips.append(avg)
        avg_str = f"{avg}%" if avg is not None else "n/a"
        max_slip = exq.get("max_slippage_pct")
        max_slip_str = f"{max_slip}%" if max_slip is not None else "n/a"
        worst = exq.get("worst_ticker")
        worst_str = worst if worst is not None else "n/a"
        lines.append(
            f"| {summary.get('trading_day', '?')} | {exq.get('priced_fills', 0)} | "
            f"{exq.get('target_price_missing', 0)} | {avg_str} | "
            f"{max_slip_str} | {worst_str} |"
        )
    week_avg = round(sum(all_slips) / len(all_slips), 4) if all_slips else None
    week_avg_str = f"{week_avg}%" if week_avg is not None else "n/a"
    lines.append(f"| **Week avg** | | | **{week_avg_str}** | | |")
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
        summary = summary_by_day.get(day) or {}
        cov = summary.get("coverage") or {}
        manual = rs.get("manual_review_tickers") or []

        candidates = cov.get("buy_candidates", (rs.get("counts") or {}).get("buy_candidates", 0))
        fills = cov.get("fills")
        fills_str = str(fills) if fills is not None else "n/a"
        coverage = cov.get("coverage_pct")
        coverage_str = f"{coverage}%" if coverage is not None else "n/a"
        missing = cov.get("missing_fills")
        missing_str = str(missing) if missing is not None else "n/a"

        lines.append(
            f"| {day or '?'} | {candidates} | "
            f"{fills_str} | {coverage_str} | "
            f"{missing_str} | {len(manual)} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_weekly_review_notes(
    run_states: Sequence[Dict[str, Any]],
    summaries: Sequence[Dict[str, Any]],
) -> str:
    """Auto-generate review prompts from the week's aggregates."""
    lines = ["## Weekly review notes", ""]
    # ⚡ Bolt Optimization: Use a single pass over summaries and run_states
    # instead of multiple O(N) generators to reduce iteration overhead.
    total_missing = 0.0
    missing_targets = 0.0
    for s in summaries:
        cov = s.get("coverage") or {}
        exq = s.get("execution_quality") or {}
        total_missing += _num(cov.get("missing_fills"))
        missing_targets += _num(exq.get("target_price_missing"))

    total_manual = 0
    for rs in run_states:
        total_manual += len(rs.get("manual_review_tickers") or [])

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
    atomic_write_text(path, build_weekly_dashboard(run_states, summaries, sector_map, week_label))
    return str(path)
