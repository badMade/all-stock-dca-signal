"""Phase 0 ingest driver: run artifacts + broker order rows -> ``fills`` and ``baseline_fills``.

Implements docs/adr-outcome-ledger-pit-harness.md Decisions 2, 3, 4 and 7 for the first two
tables. Spec: tests/ledger/test_run_ingest.py (operator-approved 2026-11-24, with its five
pinned assumptions). Read-only on the artifact tree; the only writes go through
``LedgerStore.ingest_fills`` / ``ingest_baseline_fills``.

Why a join and not "read each run directory": a run's own ``orders_pages_*.json`` is an order-history
snapshot taken during the run, which can predate some of that run's own orders (on the real scrubbed
fixtures in scripts/tests/fixtures/budget_exhaustion/ the 2026-11-20 09:35 run sees 1 of its 2
orders, the 15:30 run 6 of 7). So:

* ATTRIBUTION comes from each run directory (one holding ``submitted_orders.csv`` and/or
  ``finalize_orders.json``): order ids, run id, scheduled vs manual, slot, trading day, and the
  multiplier and base amount from ``candidates_*.json``.
* EXECUTION comes from broker order rows (the ``get_equity_orders`` shape), pooled from EVERY
  ``orders_pages*.json`` anywhere in the tree plus rows the caller passes (a later full pull).

Fail closed (Decision 7): an attributed order with no usable filled buy row is skipped with a
reason, never stored as a zero fill; a broker order no run claims is reported and never
ingested, because only a run directory can say an order was scheduled. Broker rows are reduced
to price, quantity, time, state, side and symbol -- nothing else (in particular no account
number) is ever copied out of an order-pages payload.

Pinned assumptions (see the spec's docstring): ``filled_at`` is the row's ``created_at``;
``notional`` is average price x filled quantity; one flat 1x baseline per ingested fill at the
fill's price and time; no candidate entry means no fill; a directory under ``manual_runs/`` is
always manual, and a manual run with no scheduled slot uses its trigger time in ET.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .store import LedgerStore

SOURCE = "run_artifact"
ORDER_LIST_FILES = ("submitted_orders.csv", "finalize_orders.json")
ET = ZoneInfo("America/New_York")
_SLOT = re.compile(r"\d{2}:\d{2}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class RunExtract:
    """What one pass over an artifact tree found. Every list is sorted, so reports are stable."""

    fills: list[dict[str, Any]] = field(default_factory=list)
    baselines: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)  # one per attributed order not ingested
    skipped_runs: list[dict[str, str]] = field(default_factory=list)  # a whole run directory not usable
    warnings: list[dict[str, str]] = field(default_factory=list)
    unattributed_order_ids: list[str] = field(default_factory=list)


@dataclass
class IngestReport(RunExtract):
    fills_inserted: int = 0
    baselines_inserted: int = 0


@dataclass(frozen=True)
class _Run:
    rel: str
    dca_run_id: str
    run_kind: str
    slot: str
    session: str
    orders: dict[str, str]  # order_id -> ticker
    candidates: dict[str, tuple[Any, Any]]  # ticker -> (multiplier, base)


class _SkipRun(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- reading the tree ----------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dirs(root: Path) -> list[Path]:
    dirs = {p.parent for name in ORDER_LIST_FILES for p in root.rglob(name) if p.is_file()}
    return sorted(dirs)


def _order_list(run_dir: Path) -> dict[str, str]:
    orders: dict[str, str] = {}
    csv_path = run_dir / "submitted_orders.csv"
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("order_id"):
                    orders.setdefault(row["order_id"], row.get("ticker") or "")
    json_path = run_dir / "finalize_orders.json"
    if json_path.is_file():
        data = _load_json(json_path)
        if not isinstance(data, list):
            raise ValueError("finalize_orders.json is not a list")
        for item in data:
            if isinstance(item, dict) and item.get("order_id"):
                orders.setdefault(str(item["order_id"]), str(item.get("ticker") or ""))
    return orders


def _candidates(run_dir: Path) -> dict[str, tuple[Any, Any]]:
    """ticker -> (multiplier, selected base). Executor files use ``cands``/``multiplier``; the
    3:30 PM file uses ``candidates``/``buy_multiplier``. Unreadable files contribute nothing, so
    their tickers fall through to ``no_candidate`` (fail closed)."""
    out: dict[str, tuple[Any, Any]] = {}
    for path in sorted(run_dir.glob("candidates_*.json")):
        try:
            body = _load_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        entries = body.get("cands") if isinstance(body.get("cands"), list) else body.get("candidates")
        for c in entries if isinstance(entries, list) else []:
            if isinstance(c, dict) and c.get("ticker"):
                mult = c.get("multiplier", c.get("buy_multiplier"))
                out.setdefault(str(c["ticker"]), (mult, c.get("selected_base_amount_usd")))
    return out


def _slot_from_trigger(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(ET).strftime("%H:%M")


def _read_run(run_dir: Path, root: Path, warnings: list[dict[str, str]]) -> _Run:
    rel = run_dir.relative_to(root).as_posix()
    summary_path = run_dir / "run_summary.json"
    if not summary_path.is_file():
        raise _SkipRun("no_run_summary")
    try:
        summary = _load_json(summary_path)
    except (OSError, ValueError):
        raise _SkipRun("unreadable_run_summary") from None
    if not isinstance(summary, dict) or not isinstance(summary.get("run_classification"), dict):
        raise _SkipRun("unreadable_run_summary")
    rc = summary["run_classification"]

    origin = rc.get("run_origin")
    if origin not in ("scheduled", "manual"):
        raise _SkipRun("unknown_run_origin")
    manual_path = "manual_runs" in Path(rel).parts
    if manual_path and origin == "scheduled":
        # Fail closed toward exclusion: a manual directory is never scheduled evidence.
        warnings.append({"run_dir": rel, "warning": "manual_path_overrides_scheduled_origin"})
    run_kind = "manual" if manual_path or origin == "manual" else "scheduled"

    dca_run_id = rc.get("run_id") or summary.get("run_id")
    if not isinstance(dca_run_id, str) or not dca_run_id.strip():
        raise _SkipRun("no_run_id")
    session = rc.get("trading_day") or summary.get("trading_day")
    if not isinstance(session, str) or not _DAY.fullmatch(session):
        raise _SkipRun("no_trading_day")
    slot = rc.get("scheduled_for_et")
    if not (isinstance(slot, str) and _SLOT.fullmatch(slot)):
        slot = _slot_from_trigger(rc.get("triggered_at_utc")) if run_kind == "manual" else None
    if slot is None:
        raise _SkipRun("no_slot")

    try:
        orders = _order_list(run_dir)
    except (OSError, ValueError):
        raise _SkipRun("unreadable_order_list") from None
    return _Run(rel, dca_run_id, run_kind, slot, session, orders, _candidates(run_dir))


def _page_rows(payload: Any) -> Iterator[dict[str, Any]]:
    if isinstance(payload, list):
        yield from (r for r in payload if isinstance(r, dict))
    elif isinstance(payload, dict):
        for page in payload.get("pages") or []:
            if isinstance(page, dict):
                yield from (r for r in page.get("orders") or [] if isinstance(r, dict))
        yield from (r for r in payload.get("results") or [] if isinstance(r, dict))


def _broker_rows(root: Path, extra: Iterable[dict[str, Any]], warnings: list[dict[str, str]]) -> dict[str, list]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(root.rglob("orders_pages*.json")):
        try:
            rows = list(_page_rows(_load_json(path)))
        except (OSError, ValueError):
            warnings.append({"file": path.relative_to(root).as_posix(), "warning": "unreadable_order_pages"})
            continue
        for row in rows:
            if row.get("id"):
                by_id.setdefault(str(row["id"]), []).append(row)
    for row in extra:
        if isinstance(row, dict) and row.get("id"):
            by_id.setdefault(str(row["id"]), []).append(row)
    return by_id


# --- the join ------------------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _execution(rows: list[dict[str, Any]], ticker: str) -> tuple[dict[str, Any] | None, str | None]:
    """The one usable filled buy among ``rows``, or a skip reason."""
    filled = [r for r in rows if r.get("state") == "filled"]
    pool = filled or rows

    def key(r: dict[str, Any]) -> tuple:
        return (r.get("side"), r.get("state"), r.get("symbol"), _number(r.get("average_price")),
                _number(r.get("cumulative_quantity")), r.get("created_at"))

    if filled and len({key(r) for r in filled}) > 1:
        return None, "conflicting_broker_rows"
    row = pool[0]
    if row.get("side") != "buy":
        return None, "not_a_buy"
    if row.get("state") != "filled":
        return None, "not_filled"
    if row.get("symbol") != ticker:
        return None, "symbol_mismatch"
    price, qty = _number(row.get("average_price")), _number(row.get("cumulative_quantity"))
    try:
        created = datetime.fromisoformat(str(row.get("created_at")))
    except ValueError:
        created = None
    if price is None or qty is None or price <= 0 or qty <= 0 or created is None or created.tzinfo is None:
        return None, "bad_broker_values"
    return {"price": price, "quantity": qty, "filled_at": created.astimezone(timezone.utc)}, None


def _candidate(run: _Run, ticker: str) -> tuple[float, float] | None:
    if ticker not in run.candidates:
        return None
    mult, base = (_number(v) for v in run.candidates[ticker])
    if mult is None or base is None or mult < 0 or base <= 0:
        return None
    return mult, base


def extract_tree(root: Path | str, *, broker_orders: Iterable[dict[str, Any]] = ()) -> RunExtract:
    """Read ``root`` (a cron-tracking tree) and return ledger-ready rows plus every skip.

    Rows carry ISO strings for dates and times so the result is JSON-printable;
    ``ingest_tree`` converts them for the store. Never writes."""
    root = Path(root)
    ex = RunExtract()
    runs: list[_Run] = []
    for run_dir in _run_dirs(root):
        try:
            runs.append(_read_run(run_dir, root, ex.warnings))
        except _SkipRun as skip:
            ex.skipped_runs.append({"run_dir": run_dir.relative_to(root).as_posix(), "reason": skip.reason})
    broker = _broker_rows(root, broker_orders, ex.warnings)

    claims: dict[str, list[_Run]] = {}
    for run in runs:
        for order_id in run.orders:
            claims.setdefault(order_id, []).append(run)

    for order_id, owners in sorted(claims.items()):
        run = owners[0]
        ticker = run.orders[order_id]

        def skip(reason: str, run: _Run = run, order_id: str = order_id) -> None:
            ex.skipped.append({"run_dir": run.rel, "order_id": order_id, "reason": reason})

        if len(owners) > 1:
            skip("claimed_by_multiple_runs")
            continue
        if order_id not in broker:
            skip("no_broker_row")
            continue
        execution, reason = _execution(broker[order_id], ticker)
        if execution is None:
            skip(reason or "bad_broker_values")
            continue
        cand = _candidate(run, ticker)
        if cand is None:
            skip("no_candidate")
            continue
        multiplier, base = cand
        filled_at = execution["filled_at"].isoformat()
        common = {"dca_run_id": run.dca_run_id, "run_kind": run.run_kind, "slot": run.slot,
                  "symbol": ticker, "session": run.session}
        ex.fills.append({
            "order_id": order_id, **common, "filled_at": filled_at,
            "price": execution["price"], "quantity": execution["quantity"],
            "notional": round(execution["price"] * execution["quantity"], 6),
            "multiplier": multiplier,
        })
        ex.baselines.append({
            "decision_id": f"{order_id}:baseline", **common, "decided_at": filled_at,
            "price": execution["price"], "base_notional": base,
        })

    ex.unattributed_order_ids = sorted(set(broker) - set(claims))
    ex.skipped.sort(key=lambda s: (s["run_dir"], s["order_id"]))
    return ex


# --- into the store ------------------------------------------------------------------------------


def _for_store(row: dict[str, Any], time_key: str) -> dict[str, Any]:
    return {**row, "session": date.fromisoformat(row["session"]), time_key: datetime.fromisoformat(row[time_key])}


def ingest_tree(
    store: LedgerStore,
    root: Path | str,
    *,
    run_id: str,
    as_of: datetime,
    broker_orders: Iterable[dict[str, Any]] = (),
) -> IngestReport:
    """Extract ``root`` and append its fills and baselines. Re-ingesting is a no-op (content hash)."""
    ex = extract_tree(root, broker_orders=broker_orders)
    fills = store.ingest_fills([_for_store(f, "filled_at") for f in ex.fills],
                               source=SOURCE, run_id=run_id, as_of=as_of)
    baselines = store.ingest_baseline_fills([_for_store(b, "decided_at") for b in ex.baselines],
                                            source=SOURCE, run_id=run_id, as_of=as_of)
    return IngestReport(**asdict(ex), fills_inserted=fills, baselines_inserted=baselines)


# --- CLI -----------------------------------------------------------------------------------------


def _load_extra(paths: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in paths:
        rows.extend(_page_rows(_load_json(Path(p))))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m market_data_ingest.ledger.run_ingest",
        description="Load scheduled and manual fills from a cron-tracking tree into the Phase 0 ledger.",
    )
    parser.add_argument("--root", required=True, help="cron-tracking root to read (never written)")
    parser.add_argument("--db", required=True, help="ledger DuckDB file (created if missing)")
    parser.add_argument("--orders-file", action="append", default=[],
                        help="extra saved get_equity_orders payload(s): a list of rows or {'pages': ...}")
    parser.add_argument("--run-id", default=None, help="ingest run id (default: run-ingest-<UTC timestamp>)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"run_ingest: root is not a directory: {root}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    run_id = args.run_id or f"run-ingest-{now:%Y%m%dT%H%M%SZ}"
    with LedgerStore(args.db) as store:
        report = ingest_tree(store, root, run_id=run_id, as_of=now, broker_orders=_load_extra(args.orders_file))
    if args.json:
        out = asdict(report)
        out.pop("fills")
        out.pop("baselines")
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"fills inserted: {report.fills_inserted}; baselines inserted: {report.baselines_inserted}; "
              f"orders skipped: {len(report.skipped)}; runs skipped: {len(report.skipped_runs)}; "
              f"unattributed broker orders: {len(report.unattributed_order_ids)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
