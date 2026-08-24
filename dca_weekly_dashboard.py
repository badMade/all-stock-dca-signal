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
import math
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


# Each morning/midday/market_open/intraday executor writes its OWN cap keys
# (MorningDCAExecutor.budget_daily_key/budget_weekly_key, overridden per
# stream subclass -- see morning_dca.py, midday_dca.py, market_open_dca.py,
# intraday_dca.py) rather than the generic daily_cap_usd/weekly_cap_usd an
# ordinary afternoon run_state carries. Without checking these, a retained
# blocked/partial-submission artifact from one of those streams (see
# _has_submitted_evidence) rendered a real partial run with a $0.00 daily/
# weekly cap in the budget and performance tables (Codex review, PR #538:
# "Normalize morning and midday cap fields in retained runs").
_STREAM_DAILY_CAP_KEYS = (
    "morning_daily_cap_usd",
    "midday_daily_cap_usd",
    "market_open_daily_cap_usd",
    "intraday_daily_cap_usd",
)
_STREAM_WEEKLY_CAP_KEYS = (
    "morning_weekly_cap_usd",
    "midday_weekly_cap_usd",
    "market_open_weekly_cap_usd",
    "intraday_weekly_cap_usd",
)


def _first_present(mapping: Dict[str, Any], keys: tuple) -> Any:
    """First value in `mapping` for a key in `keys`, in order; None if none present."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


# A restored same-slot retry candidate/outcome can carry "filled"/"pending"/
# "unconfirmed" instead of "submitted" -- all real evidence an order reached
# the broker (matches near_close_dca._derive_order_submit_status's status set
# and scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES, minus
# "not_submitted_confirmation_required" which never enters buy_candidates/
# broker_outcomes as a placed-order row). Checking only the exact "submitted"
# string here let one of those rows fall through and get silently dropped
# from the weekly dashboard/monthly cost tracker despite representing a real
# broker submission (Codex review, PR #538: "Recognize every placed-order
# status as submission evidence").
_PLACED_ORDER_STATUSES = frozenset({"submitted", "filled", "pending", "unconfirmed"})


def _has_submitted_evidence(data: Dict[str, Any]) -> bool:
    """True when a run_state carries at least one real submitted order.

    Morning/midday's ``_blocked_live_result()`` can mark a run
    ``status: "blocked"`` (a later candidate's raw persistence failed) while
    still carrying a genuine earlier submission in ``buy_candidates``/
    ``broker_outcomes`` -- that submission must not disappear from weekly
    order/notional metrics just because the run as a whole is flagged blocked.

    An ``artifact_contract_failure`` (from
    ``near_close_dca._write_and_validate_scheduled_contract``) -- including one
    whose ``compute_only`` flag is true, e.g. a partial run that placed one
    candidate but left another positive-planned candidate unplaced -- does NOT
    by itself disqualify a row that genuinely reached the broker.
    ``compute_only`` describes whether the run is valid COMPLETION evidence
    (``run_classification.valid_scheduled_evidence`` is denied for it
    separately), not whether any individual row's placement is trustworthy: a
    real order id on the placed row is real capital deployment regardless of
    what a DIFFERENT candidate in the same run failed to do. Disqualifying the
    whole artifact here dropped that real order from the weekly dashboard and
    monthly cost tracker entirely (Codex review, PR #538: "Retain submitted
    rows in compute-only partial failures" -- the same principle the earlier
    "Retain real orders when only the artifact contract failed" finding
    established for a companion-artifact-only failure, extended to the
    partial-placement compute-only case).
    """
    buy_candidates = data.get("buy_candidates")
    if isinstance(buy_candidates, list) and any(
        isinstance(row, dict) and row.get("status") in _PLACED_ORDER_STATUSES for row in buy_candidates
    ):
        return True
    broker_outcomes = data.get("broker_outcomes")
    if isinstance(broker_outcomes, dict) and any(
        isinstance(outcome, dict) and outcome.get("status") in _PLACED_ORDER_STATUSES
        for outcome in broker_outcomes.values()
    ):
        return True
    return False


def load_json_files(paths: Sequence[str | Path]) -> List[Dict[str, Any]]:
    """Load artifacts, skipping the minimal ``status: "blocked"`` run_state stubs
    written for date-gated days — they are not run days and would otherwise be
    counted into the weekly snapshot/table denominators — and skipping any
    manual run_states so manual candidates/notional never enter the
    scheduled-live weekly cost basis, reliability, or order-count metrics.

    A blocked run that nonetheless carries a real submitted order (a later
    candidate's raw persistence failed after an earlier one was placed) is
    NOT skipped -- see :func:`_has_submitted_evidence`.
    """
    loaded = [json.loads(Path(p).read_bytes()) for p in paths]
    return [
        data
        for data in loaded
        if (data.get("status") != "blocked" or _has_submitted_evidence(data))
        and not _is_manual_run_state(data)
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

    A retained blocked/partial-submission morning/midday artifact (see
    ``_has_submitted_evidence``) never populates ``budget.base_amount_usd`` --
    ``build_artifacts()`` records the ladder's base amount at
    ``ladder.base_amount_usd`` instead. Without this fallback, admitting one of
    these artifacts into ``build_graded_vs_fixed()`` raised here and aborted the
    entire weekly dashboard build (Codex review, PR #538 discussion_r3837514831:
    "Normalize the base amount for retained partial runs").
    """
    budget = rs.get("budget") or {}
    ladder = rs.get("ladder") or {}
    raw = budget.get("base_amount_usd", budget.get("base_buy_amount_usd", ladder.get("base_amount_usd")))
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


def _this_run_submitted_notional(rs: Dict[str, Any]) -> float:
    """Sum the notional this run's OWN rows actually submitted.

    ``budget.hypothetical_day_spend_usd`` on a retained blocked/partial
    morning/midday artifact is ``ledger.day_planned(trading_day)`` -- a
    CUMULATIVE running total for the whole trading day, seeded from any prior
    same-day allocations by ``_compute_top_up()`` (see ``build_artifacts()`` in
    morning_dca.py). Using it as "this run's executed notional" double-counts:
    an earlier same-day run's own submitted amount is both baked into that
    cumulative figure AND separately contributed by that earlier run's own
    run_state artifact, which is also present in the dashboard's artifact set.

    Instead, sum only the rows THIS run actually submitted: ``buy_candidates``
    entries whose status is in ``_PLACED_ORDER_STATUSES`` carry their real
    placed amount at ``planned_amount_usd`` (morning_dca.py's
    ``build_artifacts()`` writes ``oc["submitted_amount_usd"]`` there for a
    submitted outcome). Fall back to ``broker_outcomes`` for a ticker with a
    placed outcome but no matching ``buy_candidates`` row (defensive; normal
    artifacts always pair the two).

    Checks the SAME ``_PLACED_ORDER_STATUSES`` set as ``_has_submitted_evidence``
    rather than the exact ``"submitted"`` string: a restored same-slot retry row
    carrying "filled"/"pending"/"unconfirmed" is admitted by that function as
    real submission evidence, but summing only literal "submitted" rows here
    reported $0 executed/spent for that same real order (Codex review, PR #538:
    "Include every placed status in submitted notional").
    """
    total = 0.0
    seen_tickers: set[str] = set()
    for c in rs.get("buy_candidates") or []:
        if isinstance(c, dict) and c.get("status") in _PLACED_ORDER_STATUSES:
            # A missing/non-numeric planned_amount_usd must NOT mark this
            # ticker "seen" -- _num()'s 0.0 fallback is indistinguishable
            # from a genuine $0 amount, and marking it seen here skipped the
            # broker_outcomes fallback below even when THAT entry carries
            # the real submitted_amount_usd, silently reporting a real
            # order as zero executed notional (Codex review, PR #538:
            # "Fall back when the candidate notional is absent").
            try:
                raw_amount = c.get("planned_amount_usd")
                if raw_amount is None:
                    continue
                amount = float(raw_amount)
            except (TypeError, ValueError):
                continue
            total += amount
            ticker = c.get("ticker")
            if isinstance(ticker, str):
                seen_tickers.add(ticker)
    broker_outcomes = rs.get("broker_outcomes")
    if isinstance(broker_outcomes, dict):
        for ticker, outcome in broker_outcomes.items():
            if ticker in seen_tickers:
                continue
            if isinstance(outcome, dict) and outcome.get("status") in _PLACED_ORDER_STATUSES:
                total += _num(outcome.get("submitted_amount_usd"))
    return total


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
        # Prefer the ordinary completed-run field names (counts.submitted /
        # budget.day_notional_usd), but the blocked/partial-submission
        # artifacts _has_submitted_evidence() lets through above (a later
        # candidate's raw persistence failed after an earlier one was
        # placed) never populate those -- morning/midday's
        # _blocked_live_result()/_apply_blocked_before_placement_fields()
        # record the real values under the newer top-level
        # orders_submitted_count and (cumulatively, see
        # _this_run_submitted_notional) budget.hypothetical_day_spend_usd
        # instead. Without this fallback, a partial run with a genuine
        # submitted order still counted toward run days/candidates above
        # but was aggregated as zero orders and zero notional here (Codex
        # review, PR #538 discussion_r3837489854: "Normalize retained
        # partial runs before dashboard aggregation").
        submitted_count = counts.get("submitted")
        if submitted_count is None:
            submitted_count = rs.get("orders_submitted_count")
        total_submitted += _num(submitted_count)
        day_notional = budget.get("day_notional_usd")
        if day_notional is None:
            # budget.hypothetical_day_spend_usd is the day's CUMULATIVE planned
            # spend (ledger.day_planned), not this run's own contribution -- a
            # same-day prior run's amount is baked into it and would be
            # double-counted against that prior run's own artifact, also
            # present in run_states. Derive this run's own submitted notional
            # from its own rows instead (Codex review, PR #538 discussion
            # r3837514830: "Derive partial-run notional from submitted rows").
            day_notional = _this_run_submitted_notional(rs)
        total_notional += _num(day_notional)

        for c in rs.get("buy_candidates") or []:
            planned_notional += _num(c.get("planned_amount_usd"))

        week_notional_value = budget.get("week_notional_usd")
        if week_notional_value is None:
            # morning/midday/market_open/intraday's cumulative weekly planned
            # spend lives under this key instead (Codex review, PR #538:
            # "Normalize morning and midday cap fields in retained runs").
            week_notional_value = budget.get("hypothetical_week_spend_usd")
        wn = _num(week_notional_value)
        if wn > week_notional:
            week_notional = wn

        # Fall back to the pre-policy key so historical run_state artifacts (which
        # named the cap "weekly_notional_cap_usd") still populate the snapshot.
        weekly_cap_value = budget.get("weekly_cap_usd") or budget.get("weekly_notional_cap_usd")
        if weekly_cap_value is None:
            weekly_cap_value = _first_present(budget, _STREAM_WEEKLY_CAP_KEYS)
        wc = _num(weekly_cap_value)
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
        if daily_cap is None:
            # A morning/midday/market_open/intraday artifact carries its cap
            # under its OWN stream-specific key instead (Codex review, PR
            # #538: "Normalize morning and midday cap fields in retained
            # runs").
            daily_cap = _first_present(budget, _STREAM_DAILY_CAP_KEYS)
        reserve = budget.get("weekly_rollover_reserve_usd")
        if reserve is None:
            reserve = budget.get("carry_forward_reserve_usd")
        # A retained blocked/partial-submission morning/midday artifact (see
        # _has_submitted_evidence) never populates budget.day_notional_usd --
        # its real submitted spend lives only in its own submitted
        # buy_candidates/broker_outcomes rows. Without this fallback the
        # budget table reported $0.00 spent for a day that had a genuine
        # submitted order, even though build_performance_snapshot()'s own
        # notional total (fixed by the same normalization) reported it
        # correctly (Codex review, PR #538 discussion_r3837630719:
        # "Normalize partial runs for the budget table"). Reuses
        # _this_run_submitted_notional() rather than re-deriving the same
        # submitted-rows sum a second time.
        day_notional = budget.get("day_notional_usd")
        if day_notional is None:
            day_notional = _this_run_submitted_notional(rs)
        remaining = budget.get("remaining_spend_today_usd")
        if remaining is None:
            # morning/midday/market_open/intraday name this remaining_daily_usd
            # instead (Codex review, PR #538: "Normalize morning and midday cap
            # fields in retained runs").
            remaining = budget.get("remaining_daily_usd")
        lines.append(
            f"| {rs.get('trading_day', '?')} | {budget.get('budget_tier') or 'n/a'} | "
            f"${_num(day_notional):.2f} | "
            f"${_num(daily_cap):.2f} | "
            f"${_num(reserve):.2f} | "
            f"${_num(remaining):.2f} |"
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
        lines.append(f"- {total_manual} manual-review flag(s) across the week — reconcile finance vs Robinhood.")
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
