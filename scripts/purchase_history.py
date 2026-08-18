#!/usr/bin/env python3
"""Consolidated buy-history ledger: every confirmed fill, at what price, ever.

Every stream writes its own confirmed-fill record (``filled_orders.csv`` /
``orders.csv``) into its own run directory under ``dca_paths.cron_tracking_root()``
-- there is no single place that answers "what have we ever bought, and at what
price?" across all streams and all days. This module builds that place.

WHAT THIS IS
------------
A read-only, offline reporting tool. It does not participate in any trading
decision, gate, or budget calculation -- it is pure historical record-keeping,
the same category as ``backup_strategy.py`` / ``drift_detection.py``. Run it
by hand, or wire it into a periodic (e.g. end-of-day) reporting job; it is
never on the live buy path.

HOW IT WORKS
------------
It walks every ``run_state*.json`` under the configured cron-tracking root
(scheduled and manual runs, every stream), and for each run directory calls
:func:`near_close_dca._orders_csv_fills` -- the SAME hardened fill-parsing
logic every same-day top-up guard and reporting path already relies on -- to
resolve confirmed fills. Reusing it (rather than re-parsing
``filled_orders.csv``/``orders.csv`` a second, differently-shaped way) avoids
adding a second, potentially-drifting reader for a file format that already
has many documented edge cases (recovery-shape columns, partial fills,
manual-override files with no ``status`` column, etc. -- see
``near_close_dca.py``'s own extensive comments on ``_orders_csv_fills``).

Records are deduplicated by broker ``order_id``, scoped to the executing
broker (the one identifier every copy of a fill -- original run, recovery
rerun, manual-exclusion scan -- agrees on, alongside which broker it was
placed with; an order_id is only unique WITHIN its issuing broker), so a
fill visible from more than one run directory (e.g. a recovery artifact
copied beside the original) is counted exactly once.

A second, final pass sweeps every scan root for a confirmed-fill CSV
(including the full-report writer's custom ``dca_<label>_<day>_<stamp>_orders.csv``
name) whose directory has no successfully-parsed ``run_state*.json`` of its
own -- a repair/reconciliation artifact, or a run interrupted before its own
state file was written -- and includes it with honestly-unknown provenance
(``stream``/``run_origin``/``run_id`` left ``None``) rather than letting it
stay invisible to the run_state-keyed scan above. ``broker`` is reconciled
into an existing known-broker row when the orphan's order_id uniquely
matches one, rather than surviving as a second, double-counted row.

Not every confirmed fill carries all three of price/quantity/amount --
``_orders_csv_fills`` already tolerates an "amount-only" row (a confirmed
dollar notional with no settled per-share price, or vice versa) rather than
discarding it, and this module preserves that: a field it cannot resolve is
recorded as an empty cell / ``null``, never a fabricated zero.

OUTPUT
------
Two files, written into the cron-tracking root by default:

* ``purchase_history.csv`` -- one row per confirmed fill: ``trading_day``,
  ``ticker``, ``quantity``, ``avg_fill_price``, ``amount_usd``, ``order_id``,
  ``stream``, ``run_origin``, ``run_id``, ``broker``, ``source_run_dir``.
  ``amount_usd``
  is ``avg_fill_price * quantity_filled`` (the executed notional, matching
  this repo's own ``total_filled_notional`` reporting convention) whenever
  both resolved, never the CSV's raw recorded amount as-is for a completed
  fill -- that raw figure is the REQUESTED notional for the standard
  full-report shape, not a recomputed executed one, and can disagree with
  what actually filled. Falls back to the CSV's resolved amount only when
  price or quantity is unavailable (a documented amount-only shape).
* ``purchase_history_by_ticker.json`` -- per-ticker rollup: total quantity,
  total invested (sum of ``amount_usd``), average cost basis, buy count,
  first/last buy date.

Standard library only, plus the one in-repo import.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date
from itertools import chain
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# This module is also a documented standalone entry point (see the module
# docstring and docs/purchase_history.md's Usage section), and direct
# invocation (``python scripts/purchase_history.py``, or via its shebang)
# puts only ``scripts/`` on sys.path rather than the repo root, so the
# imports below would raise ModuleNotFoundError -- the ``python -m`` form
# masks this by construction, and the test suite masks it too (it inserts
# the repo root itself). Mirrors the identical bootstrap
# scripts/drift_detection.py already carries for the same reason (Codex
# review: "Bootstrap the repository root for direct script execution").
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import dca_fileio  # noqa: E402 - needs the sys.path setup above
import dca_paths  # noqa: E402 - needs the sys.path setup above

__all__ = [
    "PurchaseRecord",
    "collect_purchase_records",
    "build_ticker_rollup",
    "write_purchase_history",
    "main",
]

PURCHASE_HISTORY_CSV_FILENAME = "purchase_history.csv"
PURCHASE_HISTORY_BY_TICKER_FILENAME = "purchase_history_by_ticker.json"

PURCHASE_HISTORY_FIELDS = (
    "trading_day",
    "ticker",
    "quantity",
    "avg_fill_price",
    "amount_usd",
    "order_id",
    "stream",
    "run_origin",
    "run_id",
    "broker",
    "source_run_dir",
)


@dataclass(frozen=True)
class PurchaseRecord:
    """One confirmed fill, enriched with the run identity it came from.

    ``quantity``/``avg_fill_price``/``amount_usd`` are ``Optional``: a
    genuinely amount-only or price-only confirmed fill (see module docstring)
    is preserved with the unresolved field left ``None`` rather than a
    fabricated ``0.0``, which would silently corrupt the per-ticker rollup's
    average-cost-basis math.
    """

    trading_day: str
    ticker: str
    quantity: Optional[float]
    avg_fill_price: Optional[float]
    amount_usd: Optional[float]
    order_id: Optional[str]
    stream: Optional[str]
    run_origin: Optional[str]
    run_id: Optional[str]
    broker: Optional[str]
    source_run_dir: str


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Best-effort JSON load. A malformed/unreadable run_state.json is skipped
    (reported to the caller via ``warnings``, not raised) -- this is a reporting
    tool over a large, organically-grown artifact tree, not a gate; failing the
    whole scan over one corrupt directory would hide every OTHER real fill.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _iter_run_state_paths(root: Path) -> Iterator[Path]:
    """Every ``run_state*.json`` artifact under ``root``, sorted for deterministic
    output. ``rglob`` walks manual/scheduled/recovery subtrees alike -- there is
    no single canonical location, by design (see docs/run_classification.md).

    Matches the glob every OTHER scanner of this tree uses (``dca_risk.py``,
    ``scripts/run_classification.py``, ``dca_weekly_dashboard.py``,
    ``load_prior_same_day_allocations``): an exact ``"run_state.json"`` name
    misses the documented archived/manual variants like
    ``run_state_scheduled.json`` / ``run_state_partial.json``, silently
    dropping every fill recorded under one (Codex review: "Scan all supported
    run-state filenames"). A directory carrying more than one matching file
    yields each one; any fill duplicated between them is still deduplicated
    downstream by order_id exactly as a fill duplicated across directories is.
    """
    if not root.is_dir():
        return
    yield from sorted(root.rglob("run_state*.json"))


def _dedup_rank(run_origin: Optional[str]) -> int:
    """Lower rank wins a dedup tie between two directories reporting the same order.

    A manual/recovery copy of a fill (``run_origin='manual'``) sorts before the
    scheduled directory that actually placed the order whenever its path is
    lexicographically earlier (e.g. ``manual_reruns/recovery_copy`` before
    ``original``) -- first-seen-wins then mislabeled a scheduled purchase as
    manual and attributed it to the copy's run_id (Codex review: "Prefer the
    originating run when deduplicating fills"). The run that actually placed
    the order is authoritative over any later copy, so non-manual origins
    always outrank ``manual`` regardless of directory scan order.

    ``scheduled`` outranks ``scheduled_recovery`` (and any other non-manual
    origin) rather than tying with it: with ``--additional-root`` scanning a
    second stream root, a ``scheduled_recovery`` copy of an order under the
    main root could otherwise tie with -- and, depending on scan order, win
    over -- the actual ``scheduled`` occurrence that placed it (Codex review:
    "Prefer scheduled origins over recovery copies").

    ``run_origin is None`` -- the orphan-CSV sweep's honestly-unknown
    provenance, not a real classification string -- ranks WORST of all
    (below even ``manual``), not the same as a genuine-but-unusual
    classification like ``scheduled_recovery``: the latter still came from a
    parsed ``run_state.json`` and deserves the middle rank documented above,
    while the former has no verified provenance at all and must never win
    against a directory whose run_state actually parsed, regardless of which
    one happened to be scanned first (Codex review: "Keep known manual
    attribution over an orphan copy").
    """
    if run_origin == "scheduled":
        return 0
    if run_origin == "manual":
        return 2
    if run_origin is None:
        return 3
    return 1


def _values_sort_key(values: Dict[str, Any]) -> tuple:
    """Comparison key for picking the more trustworthy of two fill-data candidates.

    ``rank`` (complete beats partial) always dominates, and is compared first
    regardless of branch below -- when it differs, the tuple comparison never
    reaches the remaining elements, so the two candidates being compared here
    always share the same ``rank`` in practice.

    Below that, PARTIAL (``rank == 0``) and COMPLETE (``rank == 1``) snapshots
    need opposite tie-break priority between price and magnitude:

    * Two PARTIAL snapshots are progressive measurements of the SAME
      still-filling order -- the one with more executed quantity/capital is
      strictly the more advanced, truer picture, whether or not it happens to
      carry a settled price yet. Ranking price first let a priced-but-smaller
      snapshot (0.01 shares) beat a larger, still-unpriced one (0.04 shares),
      discarding real executed quantity (Codex review: "Prefer the most
      advanced partial quantity").
    * Two COMPLETE snapshots should already agree on quantity (the order is
      done), so a resolved price is the more meaningful signal of which copy
      is the reconciled one (Codex review: "Prefer snapshots with a resolved
      fill price").
    """
    if values["rank"] == 0:
        return (values["rank"], values["has_quantity_rank"], values["magnitude"], values["has_price_rank"])
    return (values["rank"], values["has_quantity_rank"], values["has_price_rank"], values["magnitude"])


_TRADING_DAY_DASHED_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_TRADING_DAY_COMPACT_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
# generate_slippage_report (near_close_dca.py) builds the full-report orphan
# filename as f"dca_{run_label}_{trading_day.isoformat()}_{stamp}_orders.csv"
# with stamp = generated_at.strftime("%Y%m%d-%H%M%S") -- run_label is
# UNRESTRICTED free text a caller supplies (e.g. --run-label
# "replay-2026-08-01"), so it can itself embed a dashed date-like substring
# that sorts BEFORE the real trading-day segment in the filename. Anchored to
# this exact, fixed suffix shape rather than a generic first-match search, so
# the label's own text can never be mistaken for the definitive date (Codex
# review: "Parse the dedicated trading-day segment in report names").
_FULL_REPORT_FILENAME_DATE_RE = re.compile(r"_(20\d{2}-\d{2}-\d{2})_\d{8}-\d{6}_orders\.csv$")


def _infer_trading_day_from_path(path: Path) -> Optional[str]:
    """Best-effort ``YYYY-MM-DD`` recovered from a directory path with no
    run_state.json to read it from (the orphan-CSV sweep's only evidence).

    Every scheduled artifact root in this repo embeds its trading day in the
    path, either dashed (``YYYY-MM-DD``) or compact (``YYYYMMDDTHHMM``-style
    run directory names) -- the same two shapes
    ``generate_credit_usage_proxy_report._is_current_day_artifact`` already
    matches against for the analogous "date from path" problem.

    The full-report orphan filename shape is checked FIRST, against its
    exact known suffix, before any generic per-component search: a plain
    first-match search within that filename could otherwise return an
    unrestricted, caller-supplied run_label's own embedded date instead of
    the definitive trading-day segment that always follows it.

    Otherwise checked from the LEAF component outward, not as one search
    over the whole joined path: an orphan artifact scanned from a dated
    archive or backup root (e.g.
    ``/backups/2026-08-10/cron_tracking/manual/20260701_fix``) otherwise
    matched the OUTER backup date first and reported the purchase on the
    wrong day entirely -- the run's own directory name is always the
    closest, most specific evidence of when it actually ran (Codex review:
    "Prefer the nearest date when dating orphan fills").

    Returns ``None`` (never a guessed/fabricated date) when no path
    component carries a date pattern, or none of the matched digits form a
    real calendar date.
    """
    full_report_match = _FULL_REPORT_FILENAME_DATE_RE.search(path.name)
    if full_report_match is not None:
        try:
            return date.fromisoformat(full_report_match.group(1)).isoformat()
        except ValueError:
            pass
    for part in reversed(path.parts):
        match = _TRADING_DAY_DASHED_RE.search(part)
        if match is None:
            match = _TRADING_DAY_COMPACT_RE.search(part)
        if match is None:
            continue
        year, month, day = match.groups()
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
    return None


def collect_purchase_records(
    root: Optional[Path | str] = None,
    *,
    additional_roots: Sequence[Path | str] = (),
) -> tuple[List[PurchaseRecord], List[str]]:
    """Scan every run directory under ``root`` (default: the configured
    cron-tracking root) plus every root in ``additional_roots``, and return
    ``(records, warnings)``.

    A deployment can relocate a stream's live artifact root outside the
    cron-tracking tree entirely (``morning``/``midday`` via
    ``artifact_paths.live_run_state_root``, ``intraday``/``market-open`` via
    ``DCA_INTRADAY_LIVE_STATE_ROOT`` / ``DCA_MARKET_OPEN_LIVE_STATE_ROOT`` --
    see ``run_dca._resolve_cross_stream_prior_allocation_roots``, which
    resolves the same set for the afternoon top-up guard). A single scan root
    then silently omits that stream's whole history (Codex review: "Include
    configured stream roots outside the cron tree"). ``additional_roots`` is a
    generic escape hatch for a caller who knows their deployment's actual
    roots -- this module deliberately does not import ``run_dca``'s
    config-loading/trust-rule machinery (built for a narrower afternoon-root-
    relative purpose) to auto-discover them, keeping this reporting tool's
    only in-repo dependency the fill parser it already reuses.

    Deduplicates by ``order_id``: when the same order_id is seen from more than
    one directory, the occurrence from the more authoritative run wins (see
    :func:`_dedup_rank`), and every other occurrence is recorded as a warning
    rather than a second row -- a real fill has exactly one broker order_id no
    matter how many run directories reference it. This dedup applies across
    ``root`` and every ``additional_roots`` entry alike, so overlapping roots
    are merged rather than double-counted.
    """
    # Deferred: near_close_dca is a ~450k-line module; importing it only when
    # this function actually runs (not at module import time) keeps `--help`
    # and other lightweight invocations of this script cheap, matching the
    # deferred-import pattern scripts/generate_buy_candidates.py already uses
    # for dca_runner.
    from near_close_dca import _orders_csv_fills, _recovery_root_for, _resolve_recorded_orders_csv

    resolved_root = Path(root) if root is not None else Path(dca_paths.cron_tracking_root())
    # Resolved (absolute, no symlinks, no ".." segments) and deduplicated: the
    # same physical directory reachable through two different path spellings
    # (a relative --cron-tracking-root that happens to coincide with an
    # absolute or symlinked --additional-root) would otherwise be walked
    # TWICE. order_id-bearing rows happen to still dedup (their key doesn't
    # depend on the scan root's own spelling), but a legacy no-order-id
    # row's key is scoped by the lexical fill_source_dir string, so two
    # aliases of the same root produced two distinct keys and doubled
    # quantity/dollars/buy_count for it (Codex review: "Normalize scan roots
    # before deduplicating artifacts").
    scan_roots_list: List[Path] = []
    seen_scan_roots: set = set()
    for candidate in (resolved_root, *(Path(r) for r in additional_roots)):
        normalized = candidate.resolve()
        if normalized in seen_scan_roots:
            continue
        seen_scan_roots.add(normalized)
        scan_roots_list.append(normalized)
    scan_roots: Tuple[Path, ...] = tuple(scan_roots_list)
    records: List[PurchaseRecord] = []
    warnings: List[str] = []
    seen: Dict[str, Dict[str, Any]] = {}
    # Every directory a run_state*.json under it was actually parsed for,
    # successfully -- including a dry-run directory that was then
    # deliberately skipped, and a recovery root whose fills were pulled in
    # (or already read once via run_dir) below. The orphan sweep at the end
    # of this function treats anything NOT in this set as undiscovered
    # evidence; a dry-run/recovery directory being "covered" here is what
    # stops that sweep from re-surfacing a simulated fill, or double-counting
    # a recovery-root fill already folded in above, as if it were new.
    covered_dirs: set = set()
    # Specific FILES already consumed by an externally-referenced
    # artifacts.orders_csv_path (see below) -- narrower than covered_dirs,
    # since a full-report output directory can legitimately hold more than
    # one run's evidence.
    covered_files: set = set()

    def _record_fills(
        fill_sources: Iterator[tuple[Path, str, Dict[str, Any]]],
        *,
        run_dir: Path,
        trading_day: Optional[str],
        stream: Optional[str],
        run_origin: Optional[str],
        run_id: Optional[str],
        broker: Optional[str],
    ) -> None:
        """Resolve and dedup one run's fills into ``records``/``seen``.

        Shared by the main run_state-keyed scan below and the orphan-CSV
        sweep at the end of this function, so a fill discovered either way
        goes through the exact same order_id-normalization, executed-notional,
        and attribution-vs-values dedup logic -- see the module docstring and
        the comments below for why each step exists.
        """
        for fill_source_dir, ticker, fill in fill_sources:
            # "-" is the documented placeholder some CSV shapes use for "no
            # order id" (mirrors near_close_dca._fill_order_id's own
            # normalization). Treating it as a literal order_id gave every
            # such row the SAME dedup key across unrelated tickers/runs, so a
            # later placeholder row could silently replace or merge with an
            # earlier real fill (Codex review: "Normalize placeholder order
            # IDs before deduplication").
            raw_order_id = fill.get("order_id")
            order_id = raw_order_id.strip() if isinstance(raw_order_id, str) else raw_order_id
            order_id = order_id if order_id and order_id != "-" else None

            def _optional_float(value: Any) -> Optional[float]:
                if value is None:
                    return None
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            price = _optional_float(fill.get("avg_fill_price"))
            quantity = _optional_float(fill.get("quantity_filled"))
            amount = _optional_float(fill.get("amount_usd"))
            # For the standard full-report artifact shape, _orders_csv_fills's
            # amount_usd comes from the CSV's notional_usd column, which
            # near_close_dca._log_execution populates with the REQUESTED
            # amount (decision.suggested_amount_usd or an override) -- not a
            # recomputed executed value. _orders_csv_fills only recomputes
            # price*quantity itself for a PARTIAL execution; a fully filled
            # row keeps the raw requested notional as-is. This repo's own
            # reporting already treats avg_fill_price*quantity_filled as the
            # true executed notional (see the total_filled_notional summary
            # metric), so prefer that product whenever both are resolved --
            # not only when amount_usd happens to be absent -- rather than
            # publishing the requested (pre-slippage/pre-rounding) dollar
            # figure as if it were the confirmed executed one (Codex review:
            # "Use executed notional for completed fills"). Rounded to 6
            # decimals -- matching near_close_dca.build_report's own
            # total_filled_notional precision -- not 2: build_ticker_rollup
            # sums per-record amount_usd first and only rounds the ticker
            # total at the very end, so rounding each row to cents here first
            # would compound per-fill rounding error into the published
            # total_invested_usd/average_cost_basis_usd instead of rounding
            # once, late (Codex review: "Preserve executed-notional
            # precision").
            if price is not None and quantity is not None:
                amount = round(price * quantity, 6)
            elif fill.get("_partial_execution") and (price is None or quantity is None):
                # near_close_dca._orders_csv_fills only recomputes
                # price*quantity for a PARTIAL execution when BOTH price and
                # quantity are valid (near_close_dca.py's own
                # `if partial_execution and price_valid and qty_valid:`
                # branch); an unpriced partial row's amount_usd is therefore
                # still the RAW REQUESTED notional the row's own comment
                # documents ("only price * executed-quantity is the cash
                # that actually moved"), never executed cash. Publishing it
                # as amount_usd here would overstate invested dollars for a
                # fill that only partially executed (Codex review: "Avoid
                # treating unpriced partial requests as executed cash").
                # Quantity (a real, confirmed executed count) is preserved;
                # only the unreliable dollar figure is nulled.
                amount = None
            if price is None and quantity is None and amount is None:
                warnings.append(f"confirmed fill for {ticker!r} under {fill_source_dir} has no usable price/quantity/amount; skipped")
                continue

            # A fill with no order_id (a documented shape: some manual/recovery
            # filled_orders.csv records predate that column, e.g. the Jul 27
            # 2026 12:30 recovery pass) cannot be deduplicated against another
            # directory by identity, so it is kept rather than silently
            # dropped -- the alternative (requiring order_id) would erase real,
            # confirmed historical fills from the ledger. Its dedup key is
            # scoped to the ACTUAL directory the fill was read from
            # (fill_source_dir), not the outer run_dir being processed: the
            # nested artifacts/ directory and its enclosing recovery root can
            # each contribute a distinct no-order-id fill for the same ticker,
            # and keying both by run_dir alone collided them into one entry
            # (Codex review: "Scope no-ID recovery fills to their actual
            # artifact roots").
            # Broker-scoped by construction ("{order_id}::{broker_key}"), but
            # NOT reconciled against a matching known-broker occurrence here:
            # an orphan copy (broker=None) and its broker-attributed twin can
            # be scanned in EITHER order (a legacy/full-report run_state
            # omits ``broker`` entirely, so the "unknown" side isn't only
            # ever an orphan), and a lookback here only catches one of the
            # two orderings (Codex review: "Reconcile a brokerless copy when
            # the brokered copy arrives later"). ``_reconcile_unknown_broker_entries``
            # merges every such pair in one final pass after scanning
            # completes, which is correct regardless of scan order.
            broker_key = broker or "unknown-broker"
            dedup_key = (
                f"{order_id}::{broker_key}" if order_id else f"{fill_source_dir}::{ticker}::{broker_key}::no-order-id"
            )
            # Attribution (which run gets credit) and fill data (the actual
            # quantity/price/amount) are reconciled on SEPARATE axes: the same
            # order can appear as a stale partial snapshot beside the
            # scheduled run that placed it and as a later completed snapshot
            # in a manual/recovery copy. Ranking purely by run_origin (as a
            # single combined decision) kept the scheduled row's provenance
            # but threw away the completed quantity/amount, permanently
            # understating totals and cost basis (Codex review: "Prefer the
            # final outcome when deduplicating an order"). ``origin_rank``
            # picks which occurrence's identity metadata to credit;
            # ``completeness_rank`` (mirroring near_close_dca._fill_rank)
            # separately picks whose quantity/price/amount to trust.
            attribution: Dict[str, Any] = {
                "run_dir": run_dir,
                "rank": _dedup_rank(run_origin),
                "trading_day": trading_day if trading_day else "",
                "stream": stream,
                "run_origin": run_origin,
                "run_id": run_id if run_id else None,
                "broker": broker,
                "source_run_dir": str(run_dir),
            }
            values: Dict[str, Any] = {
                "run_dir": fill_source_dir,
                "rank": 0 if fill.get("_partial_execution") else 1,
                # A row with a resolved quantity is strictly more useful than
                # an amount-only row (it drives both total_quantity and a real
                # average-cost-basis contribution), so it must win regardless
                # of the two numbers' raw magnitude -- comparing a $5 amount-
                # only row against a 0.05-share priced row as "5 > 0.05" would
                # discard the more complete data (Codex review: "Prefer field
                # completeness before comparing magnitudes").
                "has_quantity_rank": 1 if quantity is not None else 0,
                # A resolved avg_fill_price is likewise strictly more useful
                # than a price-less row once quantity presence already ties
                # for two COMPLETE snapshots (e.g. a later reconciled snapshot
                # supplies the settled price a same-quantity row still lacks).
                # For two PARTIAL snapshots this must NOT outrank execution
                # progress, though: a priced-but-smaller partial fill (0.01
                # shares) must not beat a larger, still-unpriced one (0.04
                # shares), or the more advanced quantity is discarded (Codex
                # review: "Prefer the most advanced partial quantity"). See
                # :func:`_values_sort_key`, which orders price completeness
                # before magnitude only when both candidates are complete.
                "has_price_rank": 1 if price is not None else 0,
                # Tie-break WITHIN the same completeness tier only (e.g. two
                # progressive partially_filled snapshots, both with a known
                # quantity, or two amount-only rows): the larger executed
                # size/capital is the more advanced evidence. A strict
                # rank-only comparison kept whichever directory sorted first
                # regardless of which snapshot had actually executed more
                # (Codex review: "Preserve the latest partial snapshot during
                # deduplication").
                "magnitude": quantity if quantity is not None else (amount or 0.0),
                "quantity": quantity,
                "avg_fill_price": price,
                "amount_usd": amount,
                "order_id": str(order_id) if order_id else None,
            }
            incumbent = seen.get(dedup_key)
            if incumbent is None:
                seen[dedup_key] = {"index": len(records), "attribution": attribution, "values": values}
                records.append(
                    PurchaseRecord(
                        trading_day=attribution["trading_day"],
                        ticker=ticker,
                        quantity=values["quantity"],
                        avg_fill_price=values["avg_fill_price"],
                        amount_usd=values["amount_usd"],
                        order_id=values["order_id"],
                        stream=attribution["stream"],
                        run_origin=attribution["run_origin"],
                        run_id=attribution["run_id"],
                        broker=attribution["broker"],
                        source_run_dir=attribution["source_run_dir"],
                    )
                )
                continue

            best_attribution: Dict[str, Any] = (
                attribution if attribution["rank"] < incumbent["attribution"]["rank"] else incumbent["attribution"]
            )
            best_values: Dict[str, Any] = (
                values if _values_sort_key(values) > _values_sort_key(incumbent["values"]) else incumbent["values"]
            )
            # A directory carrying more than one run_state*.json (e.g.
            # run_state.json AND run_state_scheduled.json) reads the SAME
            # underlying CSV twice, so the "incumbent" and this occurrence
            # share a run_dir -- not a real cross-directory duplicate, just
            # this scan visiting the same evidence twice. Only warn when the
            # two occurrences actually came from different directories.
            if incumbent["attribution"]["run_dir"] != run_dir:
                warnings.append(
                    f"duplicate order_id {order_id!r} for {ticker!r} also seen under "
                    f"{incumbent['attribution']['run_dir']}; attributed to {best_attribution['run_dir']}, "
                    f"fill data from {best_values['run_dir']}"
                )
            seen[dedup_key] = {"index": incumbent["index"], "attribution": best_attribution, "values": best_values}
            records[incumbent["index"]] = PurchaseRecord(
                trading_day=best_attribution["trading_day"],
                ticker=ticker,
                quantity=best_values["quantity"],
                avg_fill_price=best_values["avg_fill_price"],
                amount_usd=best_values["amount_usd"],
                order_id=best_values["order_id"],
                stream=best_attribution["stream"],
                run_origin=best_attribution["run_origin"],
                run_id=best_attribution["run_id"],
                broker=best_attribution["broker"],
                source_run_dir=best_attribution["source_run_dir"],
            )

    for state_path in chain.from_iterable(_iter_run_state_paths(r) for r in scan_roots):
        run_dir = state_path.parent
        state = _load_json(state_path)
        if state is None:
            warnings.append(f"unreadable or malformed {state_path.name} under {run_dir}")
            continue
        covered_dirs.add(run_dir)
        # A full-report or compact demo dry run (DryRunBroker) writes simulated
        # fill rows with status="filled" and no real capital deployed, the same
        # shape as a genuine confirmed fill. The historical-allocation scanner
        # already excludes these (near_close_dca.py's own dry_run check); this
        # ledger must too, or it records purchases that never happened (Codex
        # review: "Skip simulated dry-run fills").
        if state.get("dry_run"):
            continue
        trading_day = state.get("trading_day")
        classification = state.get("run_classification")
        stream = classification.get("stream") if isinstance(classification, dict) else None
        run_origin = classification.get("run_origin") if isinstance(classification, dict) else None
        # run_classification.run_id is the origin-scoped identifier reporting
        # elsewhere in this repo treats as authoritative; the top-level run_id
        # is only the artifact's own build id and can differ from it. Falling
        # back to the top-level value only when the classification is silent
        # keeps older/legacy states (with no run_classification block at all)
        # working (Codex review: "Report the authoritative classification run
        # ID").
        run_id = (classification.get("run_id") if isinstance(classification, dict) else None) or state.get("run_id")
        # near_close_dca.write_run_state's full-report shape has NO
        # run_classification block and no top-level run_id at all -- only
        # top-level "scheduled" (bool) and "run_label". Leaving run_origin/
        # run_id null for every one of those runs threw away the only
        # provenance available for them (Codex review: "Read full-report
        # provenance from top-level fields"). Used only as a fallback so a
        # run WITH a real run_classification block is never overridden by it.
        if run_origin is None and "scheduled" in state:
            run_origin = "scheduled" if state.get("scheduled") else "manual"
        if run_id is None:
            run_id = state.get("run_label")
        # Multi-broker support is real (near_close_dca.AlpacaBroker/AlpacaConfig
        # alongside RobinhoodBroker, both selectable via config.broker), and a
        # broker's own order_id is only guaranteed unique WITHIN that broker --
        # nothing prevents two different brokers from generating the same
        # order_id string independently. Deduplicating purely by order_id could
        # then collapse two genuinely distinct fills from two different brokers
        # into one row (Codex review: "Scope deduplication to the execution
        # broker"). A non-string/missing value (older states predate this
        # field, or record it as an empty stub) falls back to None rather than
        # a fabricated broker name, so legacy states with no real broker
        # identity still dedup against each other exactly as before this
        # field existed.
        raw_broker = state.get("broker")
        broker = raw_broker.strip() if isinstance(raw_broker, str) and raw_broker.strip() else None

        # Follow the same order-artifact resolution every other reader of this
        # tree uses: a full-report run records a custom-named CSV under
        # artifacts.orders_csv_path (run_and_report's dca_<label>_..._orders.csv),
        # and the market-open/intraday streams -- which have no in-repo Python
        # entry point -- write only a bare sibling submitted_orders.csv, never
        # an artifacts block. Checking only filled_orders.csv/orders.csv (the
        # default _orders_csv_fills sibling search) silently missed both shapes
        # (Codex review: "Follow every run state's actual order artifact").
        skip_sink: List[Path] = []
        orders_csv_file = _resolve_recorded_orders_csv(state, state_path, skip_sink=skip_sink)
        if orders_csv_file is not None and not orders_csv_file.exists():
            # The recorded path can go stale (artifacts relocated/archived
            # after the state was written): following it verbatim made
            # _orders_csv_fills return an empty mapping -- indistinguishable
            # from a genuine zero-fill run -- even when a real sibling CSV
            # exists right next to it (Codex review: "Fall back when the
            # recorded order path is stale").
            warnings.append(
                f"recorded order artifact {orders_csv_file} under {run_dir} no longer exists; "
                "falling back to sibling order files"
            )
            # An archived full-report run can keep its custom-named CSV
            # sitting right beside its (also archived) run_state.json, with
            # the recorded path -- written before the archive move -- still
            # naming the OLD location. Discarding the basename entirely and
            # falling through to only the three canonical sibling names
            # below missed it, and the orphan sweep can never recover it
            # afterward since this directory is already marked covered
            # (Codex review: "Fall back to the recorded basename for stale
            # report paths"). _orders_csv_fills fails soft on a still-
            # missing path, so trying this unconditionally is safe.
            stale_basename_candidate = run_dir / orders_csv_file.name
            orders_csv_file = stale_basename_candidate if stale_basename_candidate.exists() else None
        if (
            orders_csv_file is None
            and not (run_dir / "filled_orders.csv").exists()
            and not (run_dir / "orders.csv").exists()
        ):
            # Only fall back to the bare market-open/intraday-only name when
            # NEITHER canonical sibling exists. Falling back unconditionally
            # forced this exact file on _orders_csv_fills even when a
            # confirmed filled_orders.csv sat right beside it, which bypassed
            # that function's own preference for the confirmed file over a
            # merely-submitted one -- losing the confirmed price/quantity, or
            # seeing no fill at all for a row still reading status=submitted
            # (Codex review: "Prefer confirmed sibling fills before submitted
            # orders"). Leaving orders_csv_file as None here lets
            # _orders_csv_fills apply its own filled_orders.csv-over-orders.csv
            # default resolution untouched.
            bare_submitted = run_dir / "submitted_orders.csv"
            if bare_submitted.exists():
                orders_csv_file = bare_submitted

        try:
            fills = _orders_csv_fills(run_dir, orders_csv_file=orders_csv_file, skip_sink=skip_sink)
        except Exception as exc:  # pragma: no cover - defensive; _orders_csv_fills already fails soft
            warnings.append(f"failed to read fills under {run_dir}: {exc}")
            continue
        # _resolve_recorded_orders_csv explicitly permits a non-recovery
        # full-report state to reference an order CSV in a DIFFERENT
        # directory than run_dir (the full-report writer's own directory
        # layout doesn't guarantee co-location). Labeling every fill from it
        # with run_dir let the orphan sweep read that same external file a
        # second time: a legacy no-order-id row's dedup key is scoped per
        # source directory and can't tell "same file, seen under two
        # different labels" from "two distinct files" (Codex review: "Mark
        # externally recorded CSVs as covered"). Track the CSV's real parent
        # as the fill source. ``.resolve()`` matters: _resolve_recorded_orders_csv
        # builds the path via simple concatenation (``run_state_path.parent /
        # orders_csv_path``), never normalizing a ``../`` segment, so its
        # ``.parent`` can be a DIFFERENT Path object (unresolved) than the
        # same real directory the orphan sweep's own ``rglob`` walk yields.
        resolved_fill_source_dir = orders_csv_file.parent.resolve() if orders_csv_file is not None else run_dir
        # Mark only the SPECIFIC resolved file as covered here, not its
        # whole parent directory: a full-report output directory is
        # EXPLICITLY shared across runs (each gets its own uniquely
        # timestamped filename), so a DIFFERENT, genuinely orphaned
        # dca_*_orders.csv can legitimately sit beside this externally-
        # referenced one in that same directory. Covering the whole
        # directory made the full-report orphan pass below skip that
        # second, unrelated file too (Codex review: "Track covered order
        # files instead of whole directories"). When orders_csv_file is
        # None the fills instead came from run_dir's own default
        # resolution, and run_dir itself is already covered above -- no
        # separate action needed for that case.
        if orders_csv_file is not None:
            covered_files.add(orders_csv_file.resolve())
        # A recovery run can write artifacts/run_state.json beneath a
        # recovery directory while its confirmed filled_orders.csv sits at
        # that ENCLOSING directory's own root, with no artifacts.orders_csv_path
        # recorded to point at it. Checking only run_dir (the directory
        # containing run_state.json) missed it entirely (Codex review:
        # "Search the enclosing recovery root for fill artifacts"). _recovery_root_for
        # is a pure directory-name-pattern check, safe to call unconditionally:
        # it returns run_dir unchanged for any non-recovery layout.
        #
        # Deliberately NOT merged via near_close_dca._merge_fill: that helper
        # is built for the allocation guard's ticker-keyed aggregate, and
        # combines two DISTINCT orders for the same ticker into one blended
        # entry with order_id=None -- exactly wrong for a per-order ledger,
        # since it would silently collapse two real purchases into one row
        # and clear the order_id dedup key needed to catch a later duplicate
        # of either original order (Codex review: "Preserve distinct recovery
        # orders as separate ledger rows"). Instead, recovery-root fills are
        # fed through the SAME per-order loop below as run_dir's own fills:
        # a genuine cross-directory duplicate (same order_id) is reconciled
        # by this function's own order_id dedup further down; two distinct
        # orders for one ticker are kept as two separate rows.
        recovery_root = _recovery_root_for(run_dir)
        # Covered regardless of whether recovery_fills ends up populated below:
        # either its fills were just folded in via recovery_fills, or the
        # already_covered check found they were the SAME physical file run_dir
        # itself already read. Either way the orphan sweep below must not
        # treat this directory as undiscovered.
        covered_dirs.add(recovery_root)
        recovery_fills: Dict[str, Dict[str, Any]] = {}
        if recovery_root != run_dir:
            # A recorded artifacts.orders_csv_path like "../filled_orders.csv"
            # already resolves INTO recovery_root, and recovery_root's own
            # default sibling search (below) would then find that SAME
            # physical file a second time -- double-reading one CSV double-
            # counts every legacy no-order-id row in it (order_id-bearing
            # rows are merely deduped-with-warnings, not double-counted, but
            # a no-order-id row's dedup key is scoped per source directory
            # and can't tell "same file, two directories" from "two distinct
            # files") (Codex review: "Avoid reading a recorded recovery CSV
            # twice"). Resolve what file THIS run_dir's own read actually
            # used and skip the recovery-root read when it's the same file.
            effective_run_csv = orders_csv_file
            if effective_run_csv is None:
                candidate = run_dir / "filled_orders.csv"
                effective_run_csv = candidate if candidate.exists() else run_dir / "orders.csv"
            recovery_default_csv = recovery_root / "filled_orders.csv"
            if not recovery_default_csv.exists():
                recovery_default_csv = recovery_root / "orders.csv"
            already_covered = effective_run_csv.exists() and effective_run_csv.resolve() == recovery_default_csv.resolve()
            if not already_covered:
                # A supported market-open/intraday recovery can store
                # confirmed evidence at the recovery root ONLY as
                # submitted_orders.csv (with a broker-native state=filled
                # row) -- the same bare-sibling shape run_dir's own read
                # above already falls back to. Without applying the same
                # fallback HERE, this read silently returns no fills even
                # though real evidence sits right there, and the orphan
                # sweep can't rescue it either since recovery_root is
                # already marked covered above regardless of outcome (Codex
                # review: "Read submitted-only files from enclosing recovery
                # roots").
                recovery_orders_csv_file = None
                if not (recovery_root / "filled_orders.csv").exists() and not (recovery_root / "orders.csv").exists():
                    recovery_bare_submitted = recovery_root / "submitted_orders.csv"
                    if recovery_bare_submitted.exists():
                        recovery_orders_csv_file = recovery_bare_submitted
                try:
                    recovery_fills = _orders_csv_fills(
                        recovery_root, orders_csv_file=recovery_orders_csv_file, skip_sink=skip_sink
                    )
                except Exception as exc:  # pragma: no cover - defensive; _orders_csv_fills already fails soft
                    warnings.append(f"failed to read recovery-root fills under {recovery_root}: {exc}")
        # _orders_csv_fills fails soft: an unreadable/malformed/truncated CSV
        # returns an empty mapping rather than raising, which is indistinguishable
        # from a legitimate zero-fill day unless the caller passes skip_sink and
        # reports what landed in it (Codex review: "Report order-file parse
        # failures in ledger warnings").
        for bad_path in skip_sink:
            warnings.append(f"failed to parse order artifact {bad_path} under {run_dir}; its fills may be missing from this ledger")

        fill_sources = chain(
            ((resolved_fill_source_dir, ticker, fill) for ticker, fill in fills.items()),
            ((recovery_root, ticker, fill) for ticker, fill in recovery_fills.items()),
        )
        _record_fills(
            fill_sources,
            run_dir=run_dir,
            trading_day=trading_day,
            stream=stream,
            run_origin=run_origin,
            run_id=run_id,
            broker=broker,
        )

    # recovery_artifact_types_rule's equivalent here: a run_state-keyed scan
    # only visits a directory that HAS a run_state*.json. An order-artifact
    # persisted with no run_state at all -- a repair/reconciliation pass, or
    # a run interrupted after writing its CSV but before its own state file --
    # is invisible to every branch above (Codex review: "Include orphan
    # confirmed-fill artifacts"). Mirrors the orphan sweep
    # scripts/generate_credit_usage_proxy_report.py already runs over
    # MANUAL_SCAN_SUBROOTS, generalized across every scan root (not just the
    # cron-tracking root's known manual subroots) since --additional-root can
    # point at an arbitrary relocated stream layout this module has no
    # subroot list for.
    order_csv_names = ("filled_orders.csv", "orders.csv", "submitted_orders.csv")
    # run_and_report (the full-report/3:30pm-shape writer) records its
    # confirmed-fill CSV under a custom name -- dca_<label>_<day>_<stamp>_orders.csv
    # -- via generate_slippage_report, which runs strictly BEFORE
    # write_run_state persists run_state.json. An interruption between those
    # two steps leaves exactly this file on disk with no run_state.json at
    # all, and the fixed three-name whitelist above never discovers it
    # (Codex review: "Discover orphan full-report order CSVs").
    _FULL_REPORT_ORPHAN_GLOB = "dca_*_orders.csv"

    def _process_orphan(run_dir: Path, orders_csv_file: Optional[Path], date_source: Path) -> None:
        # skip_sink: without it, an unreadable/empty/truncated orphan CSV
        # fails soft to an empty mapping and the scan publishes with no
        # warning at all -- the run-state and recovery readers above
        # already report into warnings this way; this reader must too
        # (Codex review: "Report parse failures from orphan order
        # artifacts").
        orphan_skip_sink: List[Path] = []
        try:
            fills = _orders_csv_fills(run_dir, orders_csv_file=orders_csv_file, skip_sink=orphan_skip_sink)
        except Exception as exc:  # pragma: no cover - defensive; _orders_csv_fills already fails soft
            warnings.append(f"failed to read fills from orphan artifact under {run_dir}: {exc}")
            return
        for bad_path in orphan_skip_sink:
            warnings.append(
                f"failed to parse orphan order artifact {bad_path} under {run_dir}; its fills may be "
                "missing from this ledger"
            )
        # No run_state.json survives here to say whether the run this CSV
        # came from was a dry run -- the main scan's own dry_run check
        # (state.get("dry_run")) never runs for an orphan. But
        # near_close_dca.DryRunBroker gives every simulated fill a
        # synthetic order_id starting with "DRY-RUN" (both the ordinary and
        # "-SIM-" variants), the same tell every other DRY-RUN consumer in
        # this repo checks (near_close_dca.py's own
        # `order_id.startswith("DRY-RUN")`). Reject those rows here rather
        # than publishing fake quantity/invested dollars for a purchase
        # that never happened (Codex review: "Exclude simulated fills from
        # the orphan sweep").
        fills = {
            ticker: fill
            for ticker, fill in fills.items()
            if not (isinstance(fill.get("order_id"), str) and fill["order_id"].startswith("DRY-RUN"))
        }
        if not fills:
            return
        # No run_state.json survives to say which stream/run/broker this
        # was -- the CSV itself is the only evidence. Recorded with
        # conservative, honestly-unknown provenance (None fields sort below
        # any successfully parsed run_state's attribution via _dedup_rank)
        # rather than guessing a stream or run_origin this ledger cannot
        # actually verify.
        warnings.append(
            f"orphan order artifact(s) under {run_dir} with no run_state*.json; included with unknown "
            "provenance (stream/run_origin/run_id/broker could not be determined)"
        )
        _record_fills(
            # date_source (the specific discovered CSV path), not run_dir, as
            # the fill source: the full-report orphan pass deliberately lets
            # more than one dca_*_orders.csv share a single directory, and a
            # no-order-id row's dedup key is scoped by this value -- keying
            # it by run_dir alone collided two DISTINCT legacy no-ID fills
            # for the same ticker from two different reports in that
            # directory into one row, discarding one purchase and mixing the
            # other's trading_day with a different report's quantity (Codex
            # review: "Scope no-ID orphan fills to their source file"). For
            # the canonical pass (always exactly one artifact per directory)
            # this is the same identity run_dir would have given anyway.
            ((date_source, ticker, fill) for ticker, fill in fills.items()),
            run_dir=run_dir,
            # The definitive date can live in the CSV's own FILENAME (the
            # full-report shape always stamps one in) even when its
            # enclosing directory carries none at all (e.g. a shared,
            # undated manual_reports/ root) -- date_source is the file path
            # itself, not just run_dir, so the filename's date is checked
            # too (Codex review: "Infer orphan full-report dates from the
            # CSV filename").
            trading_day=_infer_trading_day_from_path(date_source),
            stream=None,
            run_origin=None,
            run_id=None,
            broker=None,
        )

    orphan_dirs_seen: set = set()
    orphan_full_report_seen: set = set()
    for scan_root in scan_roots:
        if not scan_root.is_dir():
            continue

        # Canonical per-directory artifacts: exactly one confirmed-fill
        # record per run directory by convention, so directory-level dedup
        # (orphan_dirs_seen) is correct here.
        canonical_paths = sorted({p for name in order_csv_names for p in scan_root.rglob(name)})
        for csv_path in canonical_paths:
            run_dir = csv_path.parent
            if run_dir in covered_dirs or run_dir in orphan_dirs_seen or csv_path.resolve() in covered_files:
                continue
            orphan_dirs_seen.add(run_dir)
            # _orders_csv_fills's own default sibling search only tries
            # filled_orders.csv then orders.csv -- never submitted_orders.csv,
            # which it has no way to guess. Matches the main scan's own
            # bare_submitted fallback above.
            orphan_orders_csv_file = None
            if not (run_dir / "filled_orders.csv").exists() and not (run_dir / "orders.csv").exists():
                bare_submitted = run_dir / "submitted_orders.csv"
                if bare_submitted.exists():
                    orphan_orders_csv_file = bare_submitted
            _process_orphan(run_dir, orphan_orders_csv_file, csv_path)

        # Full-report artifacts (dca_<label>_<day>_<stamp>_orders.csv):
        # unlike the three canonical names above, run_and_report
        # deliberately gives EACH run a unique timestamped filename while
        # accepting output_dir and state_path independently -- more than one
        # run's evidence can legitimately coexist in a single directory,
        # including a directory that ALSO holds a DIFFERENT, successfully-
        # parsed run_state*.json for an unrelated completed run. Collapsing
        # discovery to "one per directory" the way the canonical pass above
        # does for canonical names silently dropped every run but the
        # lexicographically first one (Codex review: "Process every orphan
        # report in a shared directory"); skipping the whole directory
        # whenever ANY run_state parsed there is the same mistake in a
        # different guise (Codex review: "Scan custom reports beside covered
        # runs"), and so is skipping it whenever a canonical orphan artifact
        # (filled_orders.csv etc.) was found there -- orphan_dirs_seen marks
        # directory-level coverage for the canonical pass's OWN one-artifact-
        # per-directory assumption, which says nothing about a full-report
        # CSV, an entirely different filename set, also present (Codex
        # review: "Do not suppress custom reports beside canonical
        # orphans"). Discovered and processed per FILE instead: the two
        # filename sets never overlap, so the only real duplicate-read risk
        # is re-processing the exact same file twice, which
        # orphan_full_report_seen and covered_files (the specific file an
        # externally-referenced run_state already consumed) already prevent.
        for csv_path in sorted(scan_root.rglob(_FULL_REPORT_ORPHAN_GLOB)):
            if csv_path in orphan_full_report_seen:
                continue
            orphan_full_report_seen.add(csv_path)
            run_dir = csv_path.parent
            if csv_path.resolve() in covered_files:
                continue
            _process_orphan(run_dir, csv_path, csv_path)

    # Reconcile every "unknown-broker" order_id against a UNIQUE known-broker
    # occurrence of the same order_id, in one final pass independent of scan
    # order -- the dedup_key construction above deliberately does NOT
    # attempt this inline, since a legacy/full-report run_state that omits
    # `broker` entirely (near_close_dca.write_run_state's shape, not only an
    # orphan) can be scanned either BEFORE or after a later occurrence that
    # does carry a real broker name, and an inline lookback only catches one
    # of the two orderings (Codex review: "Reconcile a brokerless copy when
    # the brokered copy arrives later").
    by_order_id: Dict[str, Dict[str, str]] = {}
    for key in seen:
        if key.endswith("::no-order-id"):
            continue
        order_id_part, _, broker_part = key.rpartition("::")
        by_order_id.setdefault(order_id_part, {})[broker_part] = key
    indices_to_drop: set = set()
    for order_id, broker_map in by_order_id.items():
        unknown_key = broker_map.get("unknown-broker")
        if unknown_key is None:
            continue
        known_matches = {b: k for b, k in broker_map.items() if b != "unknown-broker"}
        if len(known_matches) != 1:
            if len(known_matches) > 1:
                warnings.append(
                    f"order_id {order_id!r} matches known broker orders under more than one broker "
                    f"({sorted(known_matches)}); kept as a separate, unattributed row rather than "
                    "guessing which one it belongs to"
                )
            continue
        target_broker, target_key = next(iter(known_matches.items()))
        unknown_entry = seen.pop(unknown_key)
        target_entry = seen[target_key]
        best_attribution = (
            target_entry["attribution"]
            if target_entry["attribution"]["rank"] <= unknown_entry["attribution"]["rank"]
            else unknown_entry["attribution"]
        )
        best_values = (
            unknown_entry["values"]
            if _values_sort_key(unknown_entry["values"]) > _values_sort_key(target_entry["values"])
            else target_entry["values"]
        )
        target_entry["attribution"] = best_attribution
        target_entry["values"] = best_values
        ticker = records[target_entry["index"]].ticker
        records[target_entry["index"]] = PurchaseRecord(
            trading_day=best_attribution["trading_day"],
            ticker=ticker,
            quantity=best_values["quantity"],
            avg_fill_price=best_values["avg_fill_price"],
            amount_usd=best_values["amount_usd"],
            order_id=best_values["order_id"],
            stream=best_attribution["stream"],
            run_origin=best_attribution["run_origin"],
            run_id=best_attribution["run_id"],
            # The KNOWN broker, not best_attribution["broker"]: the winning
            # attribution is chosen purely by _dedup_rank (which run's
            # identity metadata to credit), and the brokerless occurrence
            # can legitimately win that comparison (e.g. a "scheduled"
            # full-report row beats a "scheduled_recovery" one) while still
            # carrying broker=None -- using it here would silently discard
            # the one known broker this merge exists specifically because it
            # uniquely proved (Codex review: "Preserve the known broker when
            # merging brokerless attribution").
            broker=target_broker,
            source_run_dir=best_attribution["source_run_dir"],
        )
        indices_to_drop.add(unknown_entry["index"])
    if indices_to_drop:
        records = [record for index, record in enumerate(records) if index not in indices_to_drop]

    records.sort(key=lambda r: (r.trading_day, r.ticker, r.order_id or ""))
    return records, warnings


def build_ticker_rollup(records: List[PurchaseRecord]) -> Dict[str, Dict[str, Any]]:
    """Per-ticker summary: total shares bought, total invested, average cost
    basis, buy count, first/last buy date.

    This is bought-quantity accounting only -- it does not know about
    dividends, splits, or sells (the strategy never sells; see
    docs/strategy.md), and it is not a substitute for the broker's own
    tax-lot/cost-basis records.
    """
    rollup: Dict[str, Dict[str, Any]] = {}
    # "_priced_*" accumulate only records where BOTH quantity and amount_usd
    # resolved: an amount-only fill (quantity None) added its dollars to
    # total_invested_usd with nothing added to total_quantity, inflating the
    # average cost basis with capital no known share count backs; the reverse
    # (a quantity-only fill) would deflate it the same way. total_invested_usd
    # itself still reports the true total dollars spent -- only the average-
    # cost-basis numerator/denominator pair is restricted to matched fills
    # (Codex review: "Exclude unpaired amounts from average cost basis").
    for record in records:
        entry = rollup.setdefault(
            record.ticker,
            {
                "total_quantity": 0.0,
                "total_invested_usd": 0.0,
                "_priced_quantity": 0.0,
                "_priced_invested_usd": 0.0,
                "buy_count": 0,
                "first_buy_date": record.trading_day,
                "last_buy_date": record.trading_day,
            },
        )
        if record.quantity is not None:
            entry["total_quantity"] += record.quantity
        if record.amount_usd is not None:
            entry["total_invested_usd"] += record.amount_usd
        if record.quantity is not None and record.amount_usd is not None:
            entry["_priced_quantity"] += record.quantity
            entry["_priced_invested_usd"] += record.amount_usd
        entry["buy_count"] += 1
        if record.trading_day and (not entry["first_buy_date"] or record.trading_day < entry["first_buy_date"]):
            entry["first_buy_date"] = record.trading_day
        if record.trading_day and (not entry["last_buy_date"] or record.trading_day > entry["last_buy_date"]):
            entry["last_buy_date"] = record.trading_day

    for entry in rollup.values():
        entry["total_invested_usd"] = round(entry["total_invested_usd"], 2)
        # Repeated float addition of fractional share quantities can leave
        # representation noise (e.g. 0.06364200000000001) in the published
        # JSON; round to a precision well beyond any real share-size
        # resolution rather than publishing raw float accumulation error.
        entry["total_quantity"] = round(entry["total_quantity"], 8)
        priced_quantity = entry.pop("_priced_quantity")
        priced_invested_usd = entry.pop("_priced_invested_usd")
        entry["average_cost_basis_usd"] = (
            round(priced_invested_usd / priced_quantity, 6) if priced_quantity else None
        )
    return rollup


def write_purchase_history(
    records: List[PurchaseRecord],
    rollup: Dict[str, Dict[str, Any]],
    *,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write both output files. Full rebuild every call (not an incremental
    append) -- re-scanning the whole tree is cheap relative to a daily
    reporting cadence, and a full rebuild can never drift from a partially-
    applied incremental update.

    Both files are replaced atomically (temp file + ``os.replace`` via
    ``dca_fileio.atomic_open_w``) rather than truncated in place: a plain
    ``open("w")`` is visible to a concurrent reader (human or a periodic job
    overlapping this one) as empty or partially written for the duration of
    the write, and an interruption mid-write can leave the CSV and JSON views
    reflecting different rebuilds -- exactly the drift this function's own
    full-rebuild guarantee claims cannot happen (Codex review: "Replace ledger
    outputs atomically").
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / PURCHASE_HISTORY_CSV_FILENAME
    with dca_fileio.atomic_open_w(csv_path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PURCHASE_HISTORY_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))

    by_ticker_path = output_dir / PURCHASE_HISTORY_BY_TICKER_FILENAME
    with dca_fileio.atomic_open_w(by_ticker_path) as handle:
        json.dump(rollup, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return csv_path, by_ticker_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cron-tracking-root",
        type=Path,
        default=None,
        help="Root to scan for run_state.json files (default: dca_paths.cron_tracking_root()).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write purchase_history.csv/.json (default: the scanned root).",
    )
    parser.add_argument(
        "--additional-root",
        type=Path,
        action="append",
        default=[],
        help=(
            "Extra root to scan for run_state*.json files, in addition to the "
            "main cron-tracking root. Repeatable. Use this when a stream's live "
            "artifact root has been relocated outside the cron-tracking tree "
            "(e.g. via artifact_paths.live_run_state_root, "
            "DCA_INTRADAY_LIVE_STATE_ROOT, or DCA_MARKET_OPEN_LIVE_STATE_ROOT)."
        ),
    )
    args = parser.parse_args(argv)

    root = args.cron_tracking_root or Path(dca_paths.cron_tracking_root())
    output_dir = args.output_dir or root

    # Refuse to publish over a possibly-good existing ledger when a requested
    # scan root can't actually be scanned (misspelled path, unmounted volume,
    # not a directory): collect_purchase_records()/_iter_run_state_paths()
    # silently treat a missing root as an empty tree, so without this check
    # this command would overwrite the published ledger with an empty or
    # partial result while still exiting 0 (Codex review: "Refuse to publish
    # when a requested scan root is unavailable").
    missing_roots = [r for r in (root, *args.additional_root) if not r.is_dir()]
    if missing_roots:
        for missing in missing_roots:
            print(f"scan root does not exist or is not a directory: {missing}", file=sys.stderr)
        return 1

    records, warnings = collect_purchase_records(root, additional_roots=args.additional_root)
    rollup = build_ticker_rollup(records)
    csv_path, json_path = write_purchase_history(records, rollup, output_dir=output_dir)

    print(f"Scanned {root}")
    print(f"Wrote {len(records)} purchase record(s) to {csv_path}")
    print(f"Wrote per-ticker rollup ({len(rollup)} ticker(s)) to {json_path}")
    if warnings:
        print(f"{len(warnings)} warning(s):", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
