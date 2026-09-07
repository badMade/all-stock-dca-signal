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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from dca_fileio import atomic_open_w, atomic_write_text


def load_run_state(path: str | Path) -> Dict[str, Any]:
    """Load a ``run_state.json`` artifact produced by the live-run path."""
    return json.loads(Path(path).read_bytes())


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


def _normalize_order_id(value: Any) -> Optional[str]:
    """Normalize an order id to a stripped string, or None if absent/blank.

    A candidate's order_id is always the adapter-normalized string form, but a
    persisted broker payload (e.g. fills_from_order_history_payload's raw
    order dicts) can carry the SAME id as a number. An exact dict-key/equality
    match against the un-normalized value then silently fails even though the
    ids are logically identical, dropping a real fill from coverage/slippage
    (Codex review: "Normalize order IDs before exact fill joins").
    """
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _candidate_order_ids(candidate: Mapping[str, Any]) -> List[str]:
    """Return every order id a candidate is known under: its scalar ``order_id``
    plus any additional ids aggregated into ``whole_share_order_ids`` (see
    ``_merge_whole_share_submission_outcomes`` in near_close_dca.py, which keeps
    only the newest id as the scalar field while preserving every real id in the
    list). Matching against only the scalar id drops the other orders from a
    whole-share candidate's quantity-weighted fill/coverage join (Codex review:
    "Include every aggregated whole-share order ID").
    """
    ids: List[str] = []
    order_id = _normalize_order_id(candidate.get("order_id"))
    if order_id:
        ids.append(order_id)
    extra = candidate.get("whole_share_order_ids")
    if isinstance(extra, (list, tuple)):
        for oid in extra:
            normalized = _normalize_order_id(oid)
            if normalized and normalized not in ids:
                ids.append(normalized)
    return ids


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

    Matched by ``order_id`` whenever both the candidate and at least one
    same-ticker fill carry one: a recovered crash-lease fill for an OLDER,
    unrelated order and a genuinely unfilled DISTINCT current candidate can
    share a ticker (the synthetic recovery bookkeeping exists precisely so
    both can coexist), and a pure ticker-level aggregate would otherwise
    attach that unrelated fill to the current candidate's own row too,
    contradicting build_missing_fills() reporting it as missing (Codex
    review: "Apply order-ID matching to slippage coverage"). Falls back to
    the prior ticker-aggregate join when no order_id is available on either
    side to disambiguate with.
    """
    fills_by_ticker: Dict[str, Dict[str, Any]] = {}
    fills_by_ticker_no_id: Dict[str, Dict[str, Any]] = {}
    fills_by_order_id: Dict[str, Dict[str, Any]] = {}
    tickers_with_order_id_fills: set = set()
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

        order_id = _normalize_order_id(fill.get("order_id"))
        if order_id:
            tickers_with_order_id_fills.add(ticker)
            fills_by_order_id[order_id] = {
                "filled_qty": qty,
                "filled_avg_price": price if qty > 0 else None,
                "order_id": order_id,
            }
        else:
            # Aggregated separately from fills_by_ticker: an ID-bearing fill
            # (a recovery, in particular) belongs to a DIFFERENT,
            # identifiable order and must not be folded into the ticker
            # fallback a no-ID candidate uses below -- only a fill that ALSO
            # carries no order_id is genuinely undisambiguated on both sides
            # (Codex review: "Keep no-ID current candidates missing beside
            # recoveries").
            if ticker not in fills_by_ticker_no_id:
                fills_by_ticker_no_id[ticker] = {"filled_qty": 0.0, "total_cost": 0.0, "order_id": None}
            no_id_entry = fills_by_ticker_no_id[ticker]
            no_id_entry["filled_qty"] += qty
            no_id_entry["total_cost"] += qty * price

    for data in fills_by_ticker.values():
        if data["filled_qty"] > 0:
            data["filled_avg_price"] = data["total_cost"] / data["filled_qty"]
    for data in fills_by_ticker_no_id.values():
        if data["filled_qty"] > 0:
            data["filled_avg_price"] = data["total_cost"] / data["filled_qty"]

    rows: List[Dict[str, Any]] = []
    for candidate in buy_candidates:
        ticker = candidate.get("ticker")
        if not isinstance(ticker, str):
            continue
        candidate_order_ids = _candidate_order_ids(candidate)
        if candidate_order_ids and ticker in tickers_with_order_id_fills:
            matched = [
                fills_by_order_id[oid] for oid in candidate_order_ids if oid in fills_by_order_id
            ]
            if not matched:
                continue
            total_qty = sum(m["filled_qty"] for m in matched)
            priced_cost = sum(
                m["filled_qty"] * m["filled_avg_price"]
                for m in matched
                if m["filled_avg_price"] is not None
            )
            fill = {
                "filled_qty": total_qty,
                "filled_avg_price": (priced_cost / total_qty) if total_qty > 0 else None,
                "order_id": matched[0].get("order_id") if len(matched) == 1 else None,
            }
        elif not candidate_order_ids and ticker in tickers_with_order_id_fills:
            # This candidate has no order_id of its own, but the ticker also
            # has an ID-bearing fill (a recovery, in particular) -- that
            # fill belongs to a DIFFERENT, identifiable order and must not
            # be attached to this candidate just because the ticker
            # matches. Falls back to the aggregate of ONLY this ticker's
            # non-ID-bearing fills, if any.
            fill = fills_by_ticker_no_id.get(ticker)
        else:
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
        if not ticker and order_id_to_ticker and order_id is not None:
            ticker = order_id_to_ticker.get(str(order_id))
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

    Matching is by ``order_id`` whenever both the candidate and at least one
    same-ticker fill carry one: a recovered crash-lease fill for an OLDER,
    unrelated order can otherwise mask a genuinely missing fill for a
    DISTINCT current candidate that happens to share the same ticker -- the
    synthetic recovery bookkeeping exists precisely so both can coexist
    (Codex review: "Do not let recovery fills cover separate same-ticker
    orders"). Falls back to plain ticker matching, exactly as before, when
    neither side has an order_id to disambiguate with.
    """
    filled_tickers = {f.get("ticker") or f.get("symbol") for f in fills}
    fills_by_ticker: Dict[Any, List[Dict[str, Any]]] = {}
    for f in fills:
        fills_by_ticker.setdefault(f.get("ticker") or f.get("symbol"), []).append(f)
    missing: List[Dict[str, Any]] = []
    for candidate in buy_candidates:
        if candidate.get("top_up_skipped"):
            continue
        ticker = candidate.get("ticker")
        candidate_order_ids = _candidate_order_ids(candidate)
        ticker_fills_with_order_id = [
            f for f in fills_by_ticker.get(ticker, []) if _normalize_order_id(f.get("order_id"))
        ]
        if candidate_order_ids and ticker_fills_with_order_id:
            covered = any(
                _normalize_order_id(f.get("order_id")) in candidate_order_ids
                for f in ticker_fills_with_order_id
            )
        elif not candidate_order_ids and ticker_fills_with_order_id:
            # This candidate has no order_id of its own (e.g. failed or
            # blocked before placement ever assigned one), but at least one
            # same-ticker fill carries one -- an ID-bearing fill belongs to a
            # DIFFERENT, identifiable order (a recovery, in particular), so
            # it must not cover a distinct no-ID candidate just because the
            # ticker matches (Codex review: "Keep no-ID current candidates
            # missing beside recoveries"). Only a fill that ALSO carries no
            # order_id -- genuinely undisambiguated on both sides -- can
            # still satisfy the ticker fallback.
            covered = any(
                not _normalize_order_id(f.get("order_id"))
                for f in fills_by_ticker.get(ticker, [])
            )
        else:
            covered = ticker in filled_tickers
        if covered:
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

    priced = []
    missing_target = []
    for r in slippage_rows:
        if r["target_price_missing"]:
            missing_target.append(r)
        elif r["slippage_pct"] is not None:
            priced.append(r)

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


def _foreground_primary_helpers() -> tuple[Any, Any, Any]:
    """Import the Jul 30 2026 foreground-primary gate, tolerating either import
    layout (repo-root namespace package ``scripts`` or ``scripts/`` on sys.path)."""
    try:
        from scripts.foreground_primary import (  # type: ignore
            detect_foreground_primary_context,
            evaluate_scheduled_evidence_alert,
            reconcile_foreground_orders,
        )
    except ImportError:  # scripts/ directly on sys.path
        from foreground_primary import (  # type: ignore
            detect_foreground_primary_context,
            evaluate_scheduled_evidence_alert,
            reconcile_foreground_orders,
        )
    return detect_foreground_primary_context, evaluate_scheduled_evidence_alert, reconcile_foreground_orders


def _model_usage_log_helper():
    """Import the model usage log writer, tolerating either import layout
    (repo-root namespace package ``scripts`` or ``scripts/`` on sys.path)."""
    try:
        from scripts.model_usage_log import log_model_usage  # type: ignore
    except ImportError:  # scripts/ directly on sys.path
        from model_usage_log import log_model_usage  # type: ignore
    return log_model_usage


# Live stream name -> canonical cron_tracking subdirectory. Mirrors the
# established source of truth for each root: scripts/run_classification.py's
# STATE_ROOT (afternoon) and morning_dca.py's DEFAULT_MARKET_OPEN_TASK_ROOT /
# DEFAULT_MORNING_LIVE_STATE_ROOT / DEFAULT_INTRADAY_TASK_ROOT, plus
# midday_dca.py's DEFAULT_MIDDAY_LIVE_STATE_ROOT. Kept here (not in
# scripts/foreground_primary.py, which is standard-library-only by contract)
# so a foreground pointer's declared stream can be checked against the root
# it is actually supposed to live under.
_FOREGROUND_STREAM_SUBDIRS = {
    "market_open": "market_open_dca",
    "morning": "morning_dca",
    "midday": "midday_dca",
    "intraday": "intraday_dca",
    "afternoon": "ce16ba6b",
}


def _foreground_expected_roots() -> Dict[str, str]:
    """Expected artifact root per live stream, for pointer containment checks.

    Without this, ``detect_foreground_primary_context``'s ``expected_roots``
    stayed at its ``None`` default here, so a pointer's root-containment check
    was silently skipped and a pointer targeting a same-day run_state OUTSIDE
    the approved artifact tree could still activate foreground mode (Codex
    review: "Supply expected roots when validating foreground pointers").
    """
    from dca_paths import stream_root

    return {stream: stream_root(subdir) for stream, subdir in _FOREGROUND_STREAM_SUBDIRS.items()}


def _realign_close_window_summary(
    summary: Dict[str, Any],
    *,
    background_mode_active: bool,
    foreground_primary_mode_active: bool,
) -> Dict[str, Any]:
    """Return a copy of ``summary`` with its mode flags/per-row statuses
    corrected to the given, actually-determined background/foreground state.

    ``summary`` (built by ``scripts.close_window_signal.build_close_window_summary``)
    carries whatever ``background_mode_active``/``foreground_primary_mode_active``
    its builder assumed at build time, which will not generally match what this
    module determines afterward. Per-row ``*_mode_status`` is fully determined
    by ``reason_if_not_fired`` plus the two mode flags (see
    ``close_window_signal.build_close_window_row``), so it can be recomputed
    here without re-deriving price/ladder inputs. ``would_have_fired_count``/
    ``would_have_fired_tickers`` need no correction: they come from
    ``reason_if_not_fired`` alone and are mode-independent.
    """
    try:
        from scripts.close_window_signal import (  # type: ignore
            STATUS_SIGNAL_MODE_NOT_ACTIVE,
            STATUS_WOULD_HAVE_FIRED,
        )
    except ImportError:  # scripts/ directly on sys.path
        from close_window_signal import (  # type: ignore
            STATUS_SIGNAL_MODE_NOT_ACTIVE,
            STATUS_WOULD_HAVE_FIRED,
        )

    realigned = dict(summary)
    realigned["background_mode_active"] = background_mode_active
    realigned["foreground_primary_mode_active"] = foreground_primary_mode_active
    rows = realigned.get("rows")
    if isinstance(rows, list):
        realigned_rows = []
        for row in rows:
            if not isinstance(row, dict):
                realigned_rows.append(row)
                continue
            row = dict(row)
            if row.get("reason_if_not_fired") is None:
                row["background_mode_status"] = (
                    STATUS_WOULD_HAVE_FIRED if background_mode_active else STATUS_SIGNAL_MODE_NOT_ACTIVE
                )
                row["foreground_mode_status"] = (
                    STATUS_WOULD_HAVE_FIRED if foreground_primary_mode_active else STATUS_SIGNAL_MODE_NOT_ACTIVE
                )
            realigned_rows.append(row)
        realigned["rows"] = realigned_rows
    return realigned


def write_foreground_primary_report(
    trading_day: str,
    output_dir: str | Path,
    *,
    foreground_primary_artifacts: Optional[Iterable[str | Path | Mapping[str, Any]]],
    scheduled_evidence_reasons: Sequence[str] = (),
    blocking_findings: Sequence[str] = (),
    close_window_summary: Optional[Dict[str, Any]] = None,
    fills: Sequence[Dict[str, Any]] = (),
) -> Optional[Path]:
    """Write ``foreground_primary_context.json`` when the day ran foreground primary.

    Returns the report path when foreground primary mode is active and nothing
    else is escalating, and ``None`` otherwise, in which case the caller must
    escalate as it always did. See scripts/foreground_primary.py for the rule.

    ``fills`` is the caller-supplied broker fill/order list. It is reconciled
    against every foreground artifact's claimed order IDs
    (:func:`scripts.foreground_primary.reconcile_foreground_orders`) before the
    alert is evaluated, so a same-day foreground artifact that claims an order
    was placed with no matching broker fill drifts into
    ``FINDING_FOREGROUND_DRIFT`` and escalates rather than being suppressed.
    """
    detect, evaluate, reconcile_orders = _foreground_primary_helpers()
    # Materialized once and reused for both passes: a caller-supplied
    # generator (the advertised Iterable type permits one) would otherwise be
    # exhausted by detect(), leaving reconcile_orders() nothing to check and
    # silently dropping FINDING_FOREGROUND_DRIFT (Codex review: "Materialize
    # foreground artifact iterables before both passes").
    artifacts = list(foreground_primary_artifacts) if foreground_primary_artifacts is not None else None
    foreground = detect(artifacts, trading_day=trading_day, expected_roots=_foreground_expected_roots())
    # Reconciled against foreground.accepted_artifacts, NOT the raw artifacts
    # list: the raw list can carry a rejected candidate (prior-day, unmarked,
    # or an invalid pointer) that happens to claim an order_id absent from
    # fills, which produced a false FINDING_FOREGROUND_DRIFT and blocked an
    # otherwise-valid foreground report (Codex review: "Reconcile only
    # accepted foreground artifacts").
    drift_findings = reconcile_orders(foreground.accepted_artifacts, fills)
    alert = evaluate(
        scheduled_evidence_ok=False,
        scheduled_evidence_reasons=scheduled_evidence_reasons,
        foreground=foreground,
        blocking_findings=[*blocking_findings, *drift_findings],
    )
    if alert.escalate or not alert.foreground_primary_mode_active:
        return None

    payload: Dict[str, Any] = {
        "trading_day": trading_day,
        "patch": "foreground_primary_surrogate_context_2026_07_30",
        "foreground_primary_context": foreground.to_report(),
        "scheduled_evidence_alert": alert.to_report(),
    }
    if close_window_summary is not None:
        # This report is only ever written on the branch where the background
        # scheduled stream produced no valid evidence and foreground primary
        # mode IS active (the early return above already enforced
        # alert.foreground_primary_mode_active). The caller built
        # close_window_summary before that was known, so its own embedded
        # mode flags/per-row statuses can be stale -- e.g. still claiming
        # background_mode_active=true while this report simultaneously
        # declares foreground primary mode active, a self-contradictory
        # artifact (Codex review: "Align summary modes with the detected
        # foreground context"). Realign it to the state this function itself
        # just determined.
        payload["what_would_have_fired"] = _realign_close_window_summary(
            close_window_summary,
            background_mode_active=False,
            foreground_primary_mode_active=True,
        )
    path = Path(output_dir) / "foreground_primary_context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


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
    foreground_primary_artifacts: Optional[Iterable[str | Path | Mapping[str, Any]]] = None,
    close_window_summary: Optional[Dict[str, Any]] = None,
    model_usage_config: Optional[ModelUsageConfig] = None,
    blocking_findings: Sequence[str] = (),
) -> Dict[str, str]:
    """Task 67aa802b entry point: post-trade reports for the SCHEDULED run only.

    Routes the whole flow through the run-classification contract:
      * selects and validates the day's scheduled run_state (never infers a
        scheduled run from broker orders alone),
      * excludes every manual-run order ID from the broker ``fills``,
      * refuses to emit reports unless a valid scheduled state exists.

    ``fills`` is the same broker fill list ``generate_reports`` takes; ``base``
    overrides the artifact root (defaults to the contract's STATE_ROOT).

    ``model_usage_log_root`` is opt-in (see scripts/model_usage_log.py,
    config/scheduled_tasks.json -> model_usage_log): None (the default) skips
    the audit-log append entirely (appended even when report generation
    raises, so a failed run is still recorded). ``model_usage_config.log_root``
    is the new keyword-only alternative; any of the legacy
    ``model_usage_model_*``/``model_usage_reason_for_premium_model`` kwargs
    override the corresponding ``model_usage_config`` field, so old and new
    call sites can both be satisfied.

    The ``model_usage_log_root``/``model_usage_model_*``/
    ``model_usage_reason_for_premium_model`` parameters are the pre-existing
    interface documented at ``docs/cost_tracking.md:206-220`` and are kept
    in their original positional-or-keyword slots (unchanged) for the
    externally managed 67aa802b scheduler, which still calls this entry
    point with them -- including positionally. ``model_usage_config`` is
    the new keyword-only alternative; any of the legacy parameters that is
    passed (non-``None``) overrides the corresponding ``model_usage_config``
    field, so old and new call sites can both be satisfied.

    ``foreground_primary_artifacts`` opts into the Jul 30 2026 foreground primary
    surrogate context patch: same-day foreground ``run_state.json`` /
    ``run_summary.json`` / pointer paths (or already-loaded manifest entries) to
    check for a foreground primary marker when no valid scheduled state exists.
    When one is found this function writes ``foreground_primary_context.json``
    and returns its path instead of raising. Left as None (the default) the
    missing-scheduled-state case raises exactly as it always has.
    ``close_window_summary`` is the optional read-only what-would-have-fired
    analytics block (see scripts/close_window_signal.py) embedded in that report.

    ``blocking_findings`` are real failures the caller already detected
    (safety failures, broker/order mismatches, stale same-day pointers,
    missing required foreground artifacts -- see
    ``scripts.foreground_primary.BLOCKING_FINDING_CODES``) that must escalate
    even when foreground primary mode is active, forwarded verbatim to
    :func:`write_foreground_primary_report`. Previously this entry point
    exposed no such parameter, so a caller that detected one of these
    failures had to either bypass this function entirely or watch it return
    foreground success while suppressing the missing scheduled run (Codex
    review: "Thread validator blocking findings through the entry point").
    """
    collect_manual_order_ids, select_scheduled = _run_classification_helpers()
    base_path = Path(base) if base is not None else None
    selection = select_scheduled(trading_day, base=base_path)

    # This reporting task's own identity/artifact -- distinct from the live
    # run (selection.state_path) it is reporting ON, so the audit log points
    # an auditor at what THIS task produced (output_dir), not the afternoon
    # 15:30 live task's run_state.json. The live run's own run_id is kept in
    # `notes` as an explicit input reference.
    # Retimed 2026-08-02 from 3:35 PM ET to 5:00 PM ET daily (task 67aa802b,
    # Geoff: "move the 3:35 slippage sync + compact Notion update to 5 pm
    # daily") -- the audit run_id must match, or it claims the retired 3:35
    # PM slot while build_entry() derives scheduled_for_et='17:00' from the
    # manifest, producing internally contradictory audit data (Codex review:
    # "Retag reporting runs with the new 17:00 slot").
    report_run_id = f"reporting_{trading_day}_1700ET"
    log_model_usage = _model_usage_log_helper()
    # Merge model_usage_config with the legacy positional-or-keyword params:
    # a non-None legacy param overrides the corresponding config field, so
    # both the externally managed scheduler (which still calls with the
    # legacy params, including positionally) and a new call site using only
    # model_usage_config are satisfied, as documented above. Previously
    # model_usage_config was accepted but never read here, so a caller using
    # only the new alternative silently got root=None (skipping the audit-log
    # append) and lost every routing override it set (Codex review: "Apply
    # ModelUsageConfig before logging").
    config = model_usage_config or ModelUsageConfig()
    effective_log_root = model_usage_log_root if model_usage_log_root is not None else config.log_root
    routing_overrides = {
        "model_requested": model_usage_model_requested if model_usage_model_requested is not None else config.model_requested,
        "model_actual": model_usage_model_actual if model_usage_model_actual is not None else config.model_actual,
        "model_routing_mode": model_usage_model_routing_mode if model_usage_model_routing_mode is not None else config.model_routing_mode,
        "reason_for_premium_model": (
            model_usage_reason_for_premium_model
            if model_usage_reason_for_premium_model is not None
            else config.reason_for_premium_model
        ),
    }

    def _log_model_usage(notes: str) -> None:
        # Best-effort append even when report generation raises, so a failed
        # report run is still recorded per the one-entry-per-task-run
        # contract. Called on every path out of this function that reaches
        # report generation or the foreground-primary success return, so a
        # successful foreground-primary run appends a row exactly as a
        # successful scheduled run does (Codex review: "Preserve model-usage
        # logging on the foreground return").
        log_model_usage(
            root=effective_log_root,
            run_id=report_run_id,
            cron_id="67aa802b",
            run_origin="scheduled",
            artifact_path=str(output_dir),
            triggered_at_utc=datetime.now(timezone.utc),
            notes=notes,
            **{k: v for k, v in routing_overrides.items() if v is not None},
        )

    if not selection.ok:
        # JUL 30 2026 FOREGROUND PRIMARY SURROGATE CONTEXT PATCH. A day the
        # operator drove in the foreground legitimately has no background
        # scheduled run_state, so raising here reported an outage that did not
        # happen. Only a same-day, marker-carrying foreground artifact reaches
        # the non-raising path, and it is recorded as CONTEXT: the report it
        # writes states valid_scheduled_evidence=false. Callers that pass no
        # foreground artifacts (every existing one) still raise exactly as
        # before.
        #
        # selection.errors represents two different failure shapes:
        # selection.state_path is None only when no scheduled run_state file
        # exists at all -- a genuinely absent background run that foreground
        # primary context legitimately explains. Any other selection.errors
        # (selection.state_path is not None) means an artifact WAS found but
        # is unreadable or fails classification/schema validation, which stays
        # a blocking artifact-contract failure regardless of foreground mode
        # (Codex review: "Keep malformed scheduled artifacts blocking in
        # foreground mode") -- it is forwarded as blocking_findings, not as a
        # suppressible scheduled_evidence_reasons entry.
        if selection.state_path is None:
            scheduled_evidence_reasons: Sequence[str] = selection.errors
            malformed_scheduled_state_findings: Sequence[str] = ()
        else:
            scheduled_evidence_reasons = ()
            malformed_scheduled_state_findings = selection.errors

        report_path = None
        try:
            report_path = write_foreground_primary_report(
                trading_day,
                output_dir,
                foreground_primary_artifacts=foreground_primary_artifacts,
                scheduled_evidence_reasons=scheduled_evidence_reasons,
                blocking_findings=[*malformed_scheduled_state_findings, *blocking_findings],
                close_window_summary=close_window_summary,
                fills=fills,
            )
        finally:
            # Logged on every exit from this attempt -- success, a still-
            # escalating None result, or write_foreground_primary_report
            # raising (e.g. a JSON serialization or atomic-write failure) --
            # matching the scheduled branch's own try/finally below instead of
            # only logging after a successful return (Codex review: "Log
            # model usage when foreground report writing fails").
            _log_model_usage(
                f"foreground primary context for {trading_day}: {report_path}"
                if report_path is not None
                else f"foreground primary context attempt for {trading_day} did not produce a report"
            )
        if report_path is not None:
            return {"foreground_primary_context": str(report_path)}
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
                    raw_payload = json.loads(raw_orders_path.read_bytes())
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
                                # The dict KEY is not necessarily the real symbol: a
                                # recovered inverse-Martingale lease is stored under a
                                # synthetic "<ticker>#imart-recovery" key (and an
                                # unproven retry under "<ticker>:unproven-N") so it
                                # cannot collide with an ordinary same-run outcome for
                                # the same ticker -- the real ticker travels INSIDE the
                                # outcome instead (see _failure_candidate_row's
                                # identical outcome.get("ticker", ticker) fallback in
                                # near_close_dca.py). Keying this map on the raw dict
                                # key would map that order's id to the synthetic
                                # string, not the real ticker, breaking symbol
                                # resolution for fills_from_order_history_payload()
                                # (Codex review, PR #859/#911 round 43: "Prefer the
                                # outcome ticker over synthetic outcome keys").
                                ticker_by_order_id[str(oid).strip()] = str(oc.get("ticker") or tkr)
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

    source_run_id = (selection.state or {}).get("run_id")
    assert selection.state is not None
    try:
        report_paths = generate_reports(
            dict(selection.state),
            fills,
            output_dir,
            require_scheduled=True,
        )
        # JUL 30 2026 CLOSE-WINDOW SIGNAL COMPARISON PATCH. Previously this
        # summary was forwarded only on the foreground-primary path (inside
        # `if not selection.ok`), so the normal background scheduled run --
        # exactly the day the 3:35/5:00 validator most needs a
        # what_would_have_fired comparison -- never received or wrote it
        # (Codex review: "Emit the close-window summary on scheduled-run
        # days"). Written alongside the other report files so a scheduled run
        # gets the same comparison a foreground-primary run does.
        if close_window_summary is not None:
            close_window_path = Path(output_dir) / "close_window_summary.json"
            close_window_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                close_window_path,
                json.dumps({"what_would_have_fired": close_window_summary}, indent=2, sort_keys=True) + "\n",
            )
            report_paths = dict(report_paths, close_window_summary=str(close_window_path))
    finally:
        _log_model_usage(f"post-trade reports for live run {source_run_id}")
    return report_paths
