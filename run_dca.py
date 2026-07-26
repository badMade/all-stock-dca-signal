#!/usr/bin/env python3
"""Orchestrator and CLI for the near-close graded DCA strategy.

This is the single runnable entry point that ties the pieces together:

    config (dca_config) -> providers/broker -> run_daily_compact (near_close_dca)
        -> post-trade reports (dca_reporting) -> compact Notion sync (dca_notion_sync)

Two ways to run:

* **Live/scheduled** — the scheduler (which holds MCP/connector runtime access) builds
  the providers and broker from injected Robinhood MCP callables and calls
  :func:`run_pipeline` directly. Real placement stays gated behind ``config.dry_run`` and
  ``RobinhoodBroker.enable_live_orders``; the CLI never flips either.

* **Local/CI smoke test** — ``python run_dca.py --demo`` runs the whole pipeline against a
  deterministic in-memory data provider in dry-run mode, writing real artifacts and
  reports to a temp/output dir. Nothing contacts a broker or the network.

The CLI is intentionally dry-run only: placing real orders requires live MCP callables
that a bare Python process does not have, so live trading is driven from the scheduler
via :func:`run_pipeline`, not from this command line.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import logging
import os
import shutil
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from dca_alerts import guarded_run
from dca_config import DEFAULT_CONFIG_DIR, load_strategy_config
from dca_fileio import atomic_write_text
from midday_dca import DEFAULT_MIDDAY_LIVE_STATE_ROOT, load_midday_config
from morning_dca import DEFAULT_MORNING_LIVE_STATE_ROOT, MORNING_LIVE_MODE, load_morning_config
from near_close_dca import (
    EASTERN,
    Bar,
    BrokerAdapter,
    MarketDataProvider,
    PriorSameDayAllocation,
    SourceName,
    StrategyConfig,
    StrategyState,
    _earlier_selected_base,
    _write_and_validate_scheduled_contract,
    build_broker,
    load_prior_same_day_allocations,
    load_prior_same_day_allocations_multi,
    merge_manual_top_up_context,
    run_daily_compact,
)
from scripts.model_usage_log import log_model_usage
from scripts.run_classification import (
    CRON_ID as AFTERNOON_LIVE_CRON_ID,
)
from scripts.run_classification import (
    build_raw_persistence_state,
    load_same_day_top_up_context,
    raw_persistence_failure_run_state,
    save_raw_input,
    update_latest_scheduled_run_pointer,
)

logger = logging.getLogger("near_close_dca.run")

# Stream label for the scheduled 3:20 PM ET final live run, used for the
# failure-alert / heartbeat identity (DCA_HEARTBEAT_URL_AFTERNOON). Matches the
# "afternoon" stream name recorded by scripts/run_classification.py pointers.
AFTERNOON_STREAM = "afternoon"

# Sibling stream roots the scheduled afternoon top-up guard folds in as prior
# same-day allocation context (in addition to its own task root): the morning and
# midday live roots, the latter of which also holds manual override reruns under
# ``manual_reruns/``. Absent roots contribute nothing, so this is safe on days a
# stream did not run. These are prior-allocation context ONLY -- never scheduled
# afternoon evidence, slippage, reliability, fill counts, or cost basis.
#
# Used both as the sibling-of-afternoon-root fallback and as the default when a
# stream's own config can't be loaded (see
# _resolve_cross_stream_prior_allocation_roots).
_CROSS_STREAM_PRIOR_ALLOCATION_ROOTS: Tuple[str, ...] = (
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    DEFAULT_MIDDAY_LIVE_STATE_ROOT,
)


def _is_within(path: Path, ancestor: Path) -> bool:
    """True if resolved ``path`` is ``ancestor`` itself or nested under it."""
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _resolve_cross_stream_prior_allocation_roots(
    config_dir: Optional[str | Path],
    afternoon_root: Path,
) -> Tuple[Path, ...]:
    """Resolve the morning/midday live roots the afternoon top-up guard scans.

    Reads each stream's OWN configured artifact root (``MorningConfig``/
    ``MiddayConfig.morning_run_state_root``, sourced from
    ``artifact_paths.live_run_state_root`` in ``config/morning_dca.json`` /
    ``config/midday_dca.json``) rather than ONLY assuming the
    sibling-of-afternoon-root directory-naming convention, so a deployment that
    relocates a stream's artifact root (staging, CI, or a future config change)
    is scanned correctly instead of silently missing that stream's fills
    (Codex/Copilot review: "Honor the configured morning artifact root").

    Also still includes the sibling-of-``afternoon_root`` roots derived from
    ``afternoon_root.parent`` -- a caller that relocates the WHOLE
    cron-tracking tree via a non-production ``artifacts_dir`` (tests, demo,
    staging) without also pointing ``config_dir`` at a matching relocated
    config would otherwise have its real sibling morning/midday artifacts
    missed, since the configured root would still resolve to the bundled
    production default (Codex follow-up review: "Preserve the sibling
    stream-root fallback"). Both sets of roots are folded in and deduped by
    resolved path, so the common case where they coincide doesn't scan the
    same tree twice.

    Two further guards on top of that:

    - When no ``config_dir`` is given, a stream's configured root is only
      trusted if it actually lives under this run's cron base (i.e. matches
      the sibling-of-``afternoon_root`` convention). Without an explicit
      ``config_dir`` the loader falls back to the bundled production
      config, so its ``live_run_state_root`` would otherwise resolve to a
      real production path (e.g. ``/home/user/workspace/cron_tracking/...``)
      even when this run's whole tree has been relocated for tests, demos,
      or staging -- silently folding another environment's real same-day
      fills in as this run's prior-allocation context (Codex follow-up
      review: "Keep relocated runs from scanning production roots"). An
      explicit ``config_dir`` is trusted only once the corresponding stream
      config actually supplied a non-empty root -- the same requirement
      already applied to the ``DCA_CONFIG_DIR`` path below -- since a
      relocated/staging ``config_dir`` that lacks ``morning_dca.json`` or
      ``midday_dca.json`` (or whose stream config fails to load) still
      falls back to the bundled production default, which must not be
      admitted just because *some* config_dir was given (Codex review:
      "Keep fallback roots contained for incomplete configs").
    - Any resolved root that is a descendant of another resolved root is
      pruned, keeping only the ancestor. ``load_prior_same_day_allocations``/
      ``..._multi`` scan every given root recursively (``rglob``), so keeping
      both a root and its child would load the same ``run_state.json`` files
      twice and double the afternoon top-up guard's prior-allocation amount
      (Codex review: "Avoid double-counting nested stream roots").
    - Any resolved root that is ``afternoon_root`` itself or an ancestor of
      it is also dropped. The caller scans ``afternoon_root`` separately and
      sums that result with this function's cross-stream roots (see
      ``run_pipeline``), so keeping a root that contains -- or equals --
      ``afternoon_root`` (e.g. a shared ``cron_tracking`` root configured as
      a stream's ``morning_run_state_root``) would recursively re-scan the
      same ``afternoon_root`` tree a second time and double-count every
      manual afternoon allocation beneath it (Codex review: "Exclude the
      afternoon tree from cross-stream roots").
    """
    cron_base = afternoon_root.parent

    # DCA_CONFIG_DIR, when set and pointing to an existing directory, has the
    # same trust level as an explicit config_dir: deployment deliberately
    # selected that environment's config, so a root that stream's own config
    # actually set must be honoured even when the caller did not forward
    # config_dir to run_pipeline(). But trust is evaluated PER STREAM: if
    # DCA_CONFIG_DIR exists yet a stream's own JSON (morning_dca.json /
    # midday_dca.json) doesn't override live_run_state_root, that stream's
    # loader silently falls back to the bundled production default root --
    # exactly the untrusted case this containment check exists to catch, and
    # merely checking that the DCA_CONFIG_DIR directory exists would still
    # admit it. Only bypass containment for a stream once loading actually
    # produced an explicit (non-None) root for it.
    _env_config_dir = os.environ.get("DCA_CONFIG_DIR")
    _env_config_dir_valid = bool(_env_config_dir and Path(_env_config_dir).is_dir())

    def _configured_root(loader_root: Optional[str], default_root: str) -> Optional[Path]:
        root = Path(loader_root or default_root)
        # An empty string is treated as "not configured" everywhere else this
        # field is consumed (e.g. MorningDCAExecutor.state_root's own
        # `self.config.morning_run_state_root or self.profile["state_root"]`
        # fallback), so it must not count as this stream having an explicit
        # root either -- `loader_root is not None` alone would still trust an
        # empty override, and `root` above already silently fell back to
        # default_root (the production path) for it.
        trusted = bool(loader_root) and (config_dir is not None or _env_config_dir_valid)
        if not trusted and not _is_within(root.resolve(), cron_base.resolve()):
            # Neither an explicit config_dir argument nor a DCA_CONFIG_DIR
            # override for this specific stream was in play, and the resolved
            # root doesn't belong to this run's cron-tracking tree -- almost
            # certainly the bundled production default surviving a relocated
            # (test/demo/staging) tree. Drop it; the sibling-root fallback
            # below still covers this stream if it actually ran under the
            # relocated tree.
            return None
        return root

    try:
        morning_root = load_morning_config(config_dir, mode=MORNING_LIVE_MODE).morning_run_state_root
    except Exception:  # noqa: BLE001 - never let a bad morning config block the afternoon run
        morning_root = None
    try:
        midday_root = load_midday_config(config_dir).morning_run_state_root
    except Exception:  # noqa: BLE001 - never let a bad midday config block the afternoon run
        midday_root = None

    configured_roots = tuple(
        r
        for r in (
            _configured_root(morning_root, DEFAULT_MORNING_LIVE_STATE_ROOT),
            _configured_root(midday_root, DEFAULT_MIDDAY_LIVE_STATE_ROOT),
        )
        if r is not None
    )
    sibling_roots = tuple(
        cron_base / Path(r).name for r in _CROSS_STREAM_PRIOR_ALLOCATION_ROOTS
    )

    # Pass 1: dedupe exact-duplicate resolved paths, preserving first-seen order.
    seen: set = set()
    deduped: List[Tuple[Path, Path]] = []
    for root in (*configured_roots, *sibling_roots):
        key = root.resolve()
        if key not in seen:
            seen.add(key)
            deduped.append((root, key))

    # Pass 2: drop any root that is afternoon_root itself or an ancestor of it --
    # the caller scans afternoon_root separately and sums that result with this
    # function's roots (see run_pipeline), so keeping one that contains
    # afternoon_root would recursively re-scan (and double-count) that tree.
    # This MUST happen before Pass 3's descendant pruning below, not folded
    # into the same comparison: if a stream is configured to the shared
    # cron_base itself, cron_base contains afternoon_root (dropped here) AND
    # is the ancestor that would otherwise cause the sibling-root fallback
    # (a genuine descendant of cron_base) to be pruned as "redundant" in Pass
    # 3 -- leaving nothing scanned for that stream at all. Excluding
    # afternoon-containing roots first means Pass 3 never sees cron_base as
    # an ancestor candidate, so the sibling fallback survives.
    afternoon_key = afternoon_root.resolve()
    non_afternoon: List[Tuple[Path, Path]] = [
        (orig, key) for orig, key in deduped if not _is_within(afternoon_key, key)
    ]

    # Pass 3: prune any root that is a descendant of another surviving root.
    resolved: List[Path] = [
        orig
        for orig, key in non_afternoon
        if not any(key != other_key and _is_within(key, other_key) for _, other_key in non_afternoon)
    ]
    return tuple(resolved)


def _build_frozen_providers_from_raw(
    original_finance: "MarketDataProvider",
    original_robinhood: "MarketDataProvider",
    raw_dir: "Path",
    *,
    strict_finance: bool = False,
    strict_robinhood: bool = False,
) -> "Tuple[MarketDataProvider, MarketDataProvider]":
    """Return frozen (finance_data, robinhood_data) backed by the persisted raw files.

    Reads all batch files from ``raw_dir`` and constructs providers whose callables
    serve the exact archived data so ``run_daily_compact()`` computes from the same
    snapshot that was persisted, not from potentially-stale live MCP calls.

    Robinhood files (``historicals_batchN.json``, ``quotes_batchN.json``):
        Frozen ``RobinhoodDataProvider`` filters by ticker+interval so bars from
        different granularities are never mixed.

    Finance files (``finance_bars_batchN.json``, ``finance_close_batchN.json``):
        Frozen ``FinanceDataProvider`` keyed by ``(ticker, interval_minutes)`` for
        bars and by ticker for previous-close.  When no finance files are present,
        the original live provider is returned for that source and a warning is
        logged; the scheduler should persist finance connector responses under these
        names to satisfy the full persist-before-compute contract.

    Falls back to both original providers when ``raw_dir`` is absent or empty.
    """
    hist_results: List[Dict[str, Any]] = []
    quotes_results: List[Dict[str, Any]] = []
    # finance_bars keyed by (ticker, interval_minutes) → list of raw bar dicts
    finance_bars: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    finance_close: Dict[str, Any] = {}  # ticker → raw close value / dict
    # Track file presence separately from parsed content: a persisted batch file
    # with empty results is a valid archived snapshot (upstream returned no data)
    # and must satisfy the strict check. Only raise when NO matching files exist.
    hist_files_seen = False
    quotes_files_seen = False
    finance_files_seen = False

    if raw_dir.is_dir():
        for path in sorted(raw_dir.iterdir()):
            name = path.name
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # Unwrap the data envelope so both save paths read the same way:
            # (a) normalize_connector_payload() resolves output_path pointers and
            #     embeds the result under payload["data"] (normalized=true).
            # (b) Inline adapter responses may also use {"data": {"results": [...]}}
            #     as their carrier shape without an output_path; save_raw_input()
            #     preserves that envelope as-is since there is nothing to resolve.
            # In both cases, step into "data" when it is a dict and "results" is
            # not already at the top level of the outer envelope. This can nest
            # more than one layer deep -- e.g. an output_path pointing at a raw
            # response that is itself {"data": {"results": [...]}} normalizes to
            # {"data": {"data": {"results": [...]}}} on disk (the same doubly
            # wrapped pointer shape dca_reporting._order_list_from_payload
            # already handles) -- so keep stepping in, not just once.
            seen_ids: set = set()
            while (
                isinstance(payload, dict)
                and "results" not in payload
                and isinstance(payload.get("data"), dict)
            ):
                if id(payload) in seen_ids:
                    break  # defend against a cyclic/self-referential payload
                seen_ids.add(id(payload))
                payload = payload["data"]
            if not isinstance(payload, dict):
                continue
            if name.startswith("historicals_batch") and name.endswith(".json"):
                hist_files_seen = True
                hist_results.extend(payload.get("results") or [])
            elif name.startswith("quotes_batch") and name.endswith(".json"):
                quotes_files_seen = True
                quotes_results.extend(payload.get("results") or [])
            elif name.startswith("finance_bars_batch") and name.endswith(".json"):
                finance_files_seen = True
                for entry in payload.get("results") or []:
                    tkr = str(entry.get("ticker") or "").strip().upper()
                    ivl = int(entry.get("interval_minutes") or 0)
                    if tkr and ivl:
                        finance_bars.setdefault((tkr, ivl), []).extend(
                            entry.get("bars") or []
                        )
            elif name.startswith("finance_close_batch") and name.endswith(".json"):
                finance_files_seen = True
                for entry in payload.get("results") or []:
                    tkr = str(entry.get("ticker") or "").strip().upper()
                    if tkr:
                        finance_close[tkr] = entry.get("previous_close")

    # --- Robinhood frozen provider -------------------------------------------
    if hist_results or quotes_results or hist_files_seen or quotes_files_seen:
        _h = hist_results
        _q = quotes_results

        def _frozen_historicals(symbols=None, interval=None, **_kw):
            sym_set = set(symbols or [])
            return {
                "results": [
                    r for r in _h
                    if (not sym_set or r.get("symbol") in sym_set)
                    and (interval is None or r.get("interval") == interval)
                ]
            }

        def _frozen_quotes(symbols=None, **_kw):
            sym_set = set(symbols or [])
            return {
                "results": [
                    r for r in _q
                    if not sym_set or (r.get("quote") or {}).get("symbol") in sym_set
                ]
            }

        from dca_live_adapters import RobinhoodDataProvider as _RDP
        frozen_robinhood: "MarketDataProvider" = _RDP(
            historicals_fn=_frozen_historicals,
            quotes_fn=_frozen_quotes if quotes_results else None,
            bounds=getattr(original_robinhood, "_bounds", "regular"),
            adjustment_type=getattr(original_robinhood, "_adjustment_type", "none"),
        )
        # get_bid_ask backs the final pre-placement spread circuit breaker (see
        # near_close_dca.quote_spread_pct), a last-moment check meant to catch
        # price movement between signal computation and order placement.
        # Leaving it on the frozen provider would serve that breaker from this
        # archived quote snapshot instead, defeating its purpose -- delegate to
        # whatever live capability the originally-injected provider has, and
        # expose none at all (fail closed) when it has none either, rather than
        # silently falling back to frozen data.
        _live_get_bid_ask = getattr(original_robinhood, "get_bid_ask", None)
        frozen_robinhood.get_bid_ask = (
            _live_get_bid_ask if callable(_live_get_bid_ask) else (lambda ticker, as_of: None)
        )
    else:
        if strict_robinhood:
            raise RuntimeError(
                "Robinhood raw files (historicals_batchN.json / quotes_batchN.json) "
                f"absent from {raw_dir} — a scheduled live run requires a frozen Robinhood "
                "provider to satisfy the persist-before-compute contract; persist Robinhood "
                "connector responses under those filenames or add them to raw_connector_inputs"
            )
        logger.warning(
            "Robinhood raw files (historicals_batchN.json / quotes_batchN.json) "
            "absent from %s — robinhood_data will use the live provider; "
            "persist Robinhood connector responses to satisfy the full "
            "persist-before-compute contract",
            raw_dir,
        )
        frozen_robinhood = original_robinhood

    # --- Finance frozen provider ----------------------------------------------
    if finance_bars or finance_close or finance_files_seen:
        _fb = finance_bars
        _fc = finance_close

        def _frozen_finance_bars(ticker, _start, _end, interval_minutes):
            return _fb.get((str(ticker).strip().upper(), int(interval_minutes)), [])

        def _frozen_finance_close(ticker, _as_of):
            return _fc.get(str(ticker).strip().upper())

        from dca_live_adapters import FinanceDataProvider as _FDP
        frozen_finance: "MarketDataProvider" = _FDP(
            bars_fn=_frozen_finance_bars,
            previous_close_fn=_frozen_finance_close,
        )
    else:
        if strict_finance:
            raise RuntimeError(
                "finance raw files (finance_bars_batchN.json / finance_close_batchN.json) "
                f"absent from {raw_dir} — a scheduled live run requires a frozen finance "
                "provider to satisfy the persist-before-compute contract; persist finance "
                "connector responses under those filenames or add them to raw_connector_inputs"
            )
        logger.warning(
            "finance raw files (finance_bars_batchN.json / finance_close_batchN.json) "
            "absent from %s — finance_data will use the live provider; "
            "persist finance connector responses to satisfy the full "
            "persist-before-compute contract",
            raw_dir,
        )
        frozen_finance = original_finance

    return frozen_finance, frozen_robinhood


def _run_drift_check(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Invoke the artifact drift detector after a live run writes its artifacts.

    Writes ``drift_report.json`` and ``drift_report.md`` alongside the run's
    artifacts and logs a warning when drift is detected. Sends a Slack alert
    when ``DCA_DRIFT_SLACK_WEBHOOK`` is set and drift is detected. Returns the
    parsed drift report dict, or None when the directory is missing/unreadable.
    """
    try:
        from scripts.drift_detection import (  # lazy — stdlib only dep
            DEFAULT_SLACK_WEBHOOK_ENV,
            detect_drift,
            send_slack_alert,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("drift detection unavailable: %s", exc)
        return None
    try:
        report = detect_drift(run_dir)
        atomic_write_text(run_dir / "drift_report.json", report.to_json(compact=False))
        atomic_write_text(run_dir / "drift_report.md", report.to_markdown())
        if report.drift_detected:
            logger.warning(
                "DRIFT DETECTED in %s: %d symbol(s) expected but no order record — %s",
                run_dir,
                len(report.affected_symbols),
                ", ".join(s.symbol for s in report.affected_symbols),
            )
            try:
                send_slack_alert(report, env_var=DEFAULT_SLACK_WEBHOOK_ENV)
            except Exception as slack_exc:  # pragma: no cover
                logger.warning("drift Slack alert failed: %s", slack_exc)
        else:
            logger.info("drift check OK for %s", run_dir)
        return report.to_dict()
    except Exception as exc:  # pragma: no cover
        logger.warning("drift check failed for %s: %s", run_dir, exc)
        return None


def configure_logging(level: str = "INFO") -> None:
    """Configure a single stream handler for CLI/scheduler use (idempotent)."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _fills_from_orders_csv(orders_csv: Path) -> List[Dict[str, Any]]:
    """Read filled orders from a compact-run orders.csv into report-ready fill dicts."""
    if not orders_csv.exists():
        return []
    fills: List[Dict[str, Any]] = []
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("status") or "").lower() != "filled":
                continue
            fills.append(
                {
                    "ticker": row.get("ticker"),
                    "fill_price": row.get("avg_fill_price"),
                    "filled_qty": row.get("quantity_filled"),
                    "order_id": row.get("order_id"),
                }
            )
    return fills


def _execution_targets_from_orders_csv(orders_csv: Path) -> Dict[str, Optional[float]]:
    """Map ticker -> the execution-record target price (signal-time quote) from orders.csv.

    The durable orders.csv records ``decision.target_price`` (the signal-time quote),
    while run_state's ``buy_candidates`` store the decision-bar close. Reconciling to the
    execution target keeps the full slippage report/Notion summary consistent with the
    orders CSV instead of ranking against a different price.
    """
    targets: Dict[str, Optional[float]] = {}
    if not orders_csv.exists():
        return targets
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ticker = row.get("ticker")
            if not ticker:
                continue
            raw = row.get("target_price")
            try:
                targets[ticker] = float(raw) if raw not in (None, "", "-") else None
            except (TypeError, ValueError):
                targets[ticker] = None
    return targets


def _refresh_fills_from_broker(
    orders_csv: Path, broker: BrokerAdapter, fills: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Re-query the broker for submitted-but-unfilled orders and append any fresh fills.

    The immediate post-trade fill lookup can miss a fill that settles moments later; by the
    3:45 reporting step it is available. Only orders not already recorded as filled are
    re-queried, and lookup failures are swallowed so reporting never crashes.

    Lookups run concurrently only when ``broker.supports_concurrent_fill_lookup`` is
    ``True``. ``get_order_fill`` is commonly a thin wrapper around a scheduler-owned MCP
    callable that is not guaranteed thread-safe (see docs/live_wiring.md), so the default
    is a plain sequential loop; brokers must opt in once their wiring is verified safe for
    concurrent calls.
    """
    if not orders_csv.exists():
        return fills
    known = {f.get("order_id") for f in fills}
    refreshed = list(fills)
    queries = []

    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            order_id = row.get("order_id")
            if not order_id or order_id in known or (row.get("status") or "").lower() == "filled":
                continue
            # Deduplicate locally before fetching, regardless of lookup outcome, so a CSV
            # with repeated rows for the same order_id never queries it twice.
            known.add(order_id)
            queries.append((order_id, row.get("ticker")))

    if not queries:
        return refreshed

    def _fetch(query_tuple):
        oid, tkr = query_tuple
        try:
            return oid, tkr, broker.get_order_fill(oid)
        except Exception:  # noqa: BLE001 - a fill re-query must never crash reporting
            logger.exception("fill refresh failed for %s", oid)
            return oid, tkr, None

    if getattr(broker, "supports_concurrent_fill_lookup", False):
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Use map to preserve the order of elements from queries
            results = list(executor.map(_fetch, queries))
    else:
        results = [_fetch(query) for query in queries]

    for order_id, ticker, fill in results:
        if fill is not None and fill.quantity_filled > 0 and fill.avg_fill_price > 0:
            refreshed.append(
                {
                    "ticker": ticker,
                    "fill_price": fill.avg_fill_price,
                    "filled_qty": fill.quantity_filled,
                    "order_id": order_id,
                }
            )

    return refreshed


def _default_prior_allocation_state_root(artifacts_dir: Path) -> Path:
    """Derive the cron task root from a scheduled run's artifacts_dir.

    Per config/scheduled_tasks.json the live scheduled path writes into
    ``<task_root>/scheduled_runs/YYYYMMDD_1520ET/``, and prior same-day
    manual/morning allocations live under the sibling
    ``<task_root>/manual_runs/<timestamp>/``. A plain ``.parent`` would land on
    ``scheduled_runs/`` itself (which never contains ``manual_runs/``), so walk
    up looking for a ``scheduled_runs`` path segment and return its parent
    instead; fall back to the immediate parent when the layout doesn't match.
    """
    for candidate in (artifacts_dir, *artifacts_dir.parents):
        if candidate.name == "scheduled_runs":
            return candidate.parent
    return artifacts_dir.parent


def run_pipeline(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    *,
    artifacts_dir: str | Path,
    run_label: str = "daily",
    scheduled: bool = True,
    generate_full_reports: bool = False,
    ignore_trend_filter: bool = False,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None,
    prior_allocation_state_root: Optional[str | Path] = None,
    deployment_history_root: Optional[str | Path] = None,
    classification_state_root: Optional[str | Path] = None,
    config_dir: Optional[str | Path] = None,
    raw_connector_inputs: Optional[Mapping[str, Any]] = None,
    required_raw_filenames: Optional[frozenset] = None,
    model_usage_log_root: Optional[str | Path] = None,
    model_usage_model_requested: Optional[str] = None,
    model_usage_model_actual: Optional[str] = None,
    model_usage_model_routing_mode: Optional[str] = None,
    model_usage_reason_for_premium_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Thin wrapper around ``_run_pipeline_impl``: same signature plus the
    opt-in ``model_usage_log_root`` (see scripts/model_usage_log.py,
    config/scheduled_tasks.json -> model_usage_log). None (the default)
    skips the audit-log append entirely, matching every other opt-in root in
    this function's signature (e.g. ``classification_state_root``).

    Wrapping rather than inlining the append into ``_run_pipeline_impl``
    means the append happens exactly once per call regardless of which of
    that function's several early-return paths was taken, without touching
    any of its trading/safety logic.

    The ``model_usage_model_*``/``model_usage_reason_for_premium_model``
    kwargs override ``log_model_usage``'s cost-conscious defaults -- this
    function has no visibility into which model/agent actually drove the
    run, so they stay unset unless the scheduler invoking this call
    explicitly knows it used a permitted premium escalation and passes them.

    The whole call runs under ``dca_alerts.guarded_run``: on a scheduled
    non-dry-run invocation, any escaping exception is logged, alerted to the
    operator Slack webhook, and pinged as a failed heartbeat before
    re-raising, and a completed run pings the afternoon success heartbeat
    (dead-man's switch for the missed-fire failure class). Demo/dry-run and
    unscheduled calls only get a log handler ensured.
    """

    def _invoke() -> Dict[str, Any]:
        result = _run_pipeline_impl(
            now,
            config,
            state,
            finance_data,
            robinhood_data,
            broker,
            artifacts_dir=artifacts_dir,
            run_label=run_label,
            scheduled=scheduled,
            generate_full_reports=generate_full_reports,
            ignore_trend_filter=ignore_trend_filter,
            prior_same_day_allocations=prior_same_day_allocations,
            prior_allocation_state_root=prior_allocation_state_root,
            deployment_history_root=deployment_history_root,
            classification_state_root=classification_state_root,
            config_dir=config_dir,
            raw_connector_inputs=raw_connector_inputs,
            required_raw_filenames=required_raw_filenames,
        )
        run_state_path = result.get("run_state_path")
        run_id = None
        if run_state_path:
            try:
                run_id = json.loads(Path(run_state_path).read_text(encoding="utf-8")).get("run_id")
            except (OSError, json.JSONDecodeError):
                run_id = None
        routing_overrides = {
            "model_requested": model_usage_model_requested,
            "model_actual": model_usage_model_actual,
            "model_routing_mode": model_usage_model_routing_mode,
            "reason_for_premium_model": model_usage_reason_for_premium_model,
        }
        log_model_usage(
            root=model_usage_log_root,
            run_id=run_id,
            cron_id=AFTERNOON_LIVE_CRON_ID,
            run_origin="scheduled" if scheduled else "manual",
            artifact_path=run_state_path,
            triggered_at_utc=now.astimezone(timezone.utc),
            **{k: v for k, v in routing_overrides.items() if v is not None},
        )
        return result

    return guarded_run(
        AFTERNOON_STREAM,
        _invoke,
        enabled=scheduled and not config.dry_run,
        context=f"artifacts_dir={artifacts_dir}",
        # The raw-persistence failure path reports its failure as a returned
        # result (with its own targeted Slack alert) instead of raising; it must
        # ping a FAILED heartbeat, not clear the dead-man's switch.
        result_ok=lambda result: not (
            isinstance(result, dict) and result.get("raw_persistence_error")
        ),
    )


def _run_pipeline_impl(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    *,
    artifacts_dir: str | Path,
    run_label: str = "daily",
    scheduled: bool = True,
    generate_full_reports: bool = False,
    ignore_trend_filter: bool = False,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None,
    prior_allocation_state_root: Optional[str | Path] = None,
    deployment_history_root: Optional[str | Path] = None,
    classification_state_root: Optional[str | Path] = None,
    config_dir: Optional[str | Path] = None,
    raw_connector_inputs: Optional[Mapping[str, Any]] = None,
    required_raw_filenames: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """Run one compact DCA cycle and (optionally) the full post-trade report set.

    Returns the merged dict of artifact/report paths and summary metrics. The compact
    daily path always runs; ``generate_full_reports=True`` additionally renders the
    slippage/coverage reports and the compact Notion sync from the run artifacts
    (heavier work reserved for Friday/manual runs).

    The same-day prior-run offset/top-up guard is scheduled-run-only. For a
    scheduled run, ``prior_allocation_state_root`` defaults to the cron task root
    derived from ``artifacts_dir`` (see ``_default_prior_allocation_state_root``)
    so the guard activates without every caller having to wire it explicitly;
    pass ``prior_same_day_allocations`` directly to override, or an explicit
    ``prior_allocation_state_root`` to scan a different tree. Manual runs never
    auto-load prior allocations, so a manual invocation is never guard-active.

    ``deployment_history_root`` feeds the trailing-7-day kill-switch (see
    ``dca_risk.apply_risk_guard``): it should point at the directory containing
    prior days' ``run_state.json`` artifacts. Defaults to ``artifacts_dir``'s
    parent, since the live task's per-day directories are its siblings.

    ``raw_connector_inputs`` lets the scheduler hand in raw connector responses
    before signal computation. Scheduled runs persist these under the run's
    ``raw/`` directory and stamp the saved-file manifest into ``run_state.json``;
    if persistence or normalization fails, the pipeline writes the documented
    no-order error run_state and returns without computing or placing.

    ``required_raw_filenames`` declares the complete set of filenames the
    scheduler expects to be present in ``raw_connector_inputs`` for a live run.
    When provided and ``dry_run=False``, any missing filename is treated as a
    persistence failure and routes through the fail-closed error state rather
    than proceeding to compute with an incomplete raw payload set.

    ``classification_state_root`` is passed through to ``near_close_dca.run_once``
    for a scheduled run: it is where ``latest_scheduled_run.json`` is
    written/updated (see ``scripts.run_classification``). Opt-in only: when not
    given, no ``latest_scheduled_run.json`` write is attempted at all, so a
    caller must pass the real cron_tracking root explicitly to enable it.

    ``config_dir`` is forwarded to ``load_morning_config``/``load_midday_config``
    when resolving the sibling morning/midday roots below (see
    ``_resolve_cross_stream_prior_allocation_roots``); ``None`` uses their own
    default resolution (``DCA_CONFIG_DIR`` env var, then the bundled config/
    directory), matching how ``config`` itself is normally loaded.
    """
    # Drain any leftover raw placement responses from a prior run on this broker
    # instance (e.g. a manual run_pipeline() call that preceded this scheduled one).
    # Without this, a non-scheduled manual call would leave its responses in the
    # buffer and the next scheduled run would write them into its orders_after_place.json.
    getattr(broker, "get_raw_placement_responses", lambda: [])()
    if ignore_trend_filter and not config.ignore_trend_filter:
        import dataclasses as _dc
        config = _dc.replace(config, ignore_trend_filter=True)
    # Auto-apply the final-buy trend override for scheduled runs in the 3:15-3:35 PM ET
    # window (task 10ce362c, "scheduled_window_et": "15:15-15:35" in scheduled_tasks.json).
    # This is restricted to scheduled live runs so manual, demo, and morning/midday callers
    # are unaffected — they run outside this window and/or with scheduled=False.
    elif scheduled and not config.dry_run and not config.ignore_trend_filter:
        _now_et = now.astimezone(EASTERN).time()
        if time(15, 15) <= _now_et <= time(15, 35):
            import dataclasses as _dc
            config = _dc.replace(config, ignore_trend_filter=True)
            logger.info(
                "final-buy window (%s ET): applied ignore_trend_filter=True for scheduled run",
                _now_et.strftime("%H:%M"),
            )
    raw_persistence_state: Optional[Dict[str, object]] = None
    if scheduled:
        trading_day_et = now.astimezone(EASTERN).date()
        trading_day_raw = trading_day_et.isoformat()
        # Only persist raw inputs on approved trading days; unapproved days let
        # run_daily_compact write the normal blocked stub without side effects.
        if trading_day_et in config.approved_test_dates:
            # Always save raw inputs directly under artifacts_dir/raw so the raw
            # directory is co-located with run_state.json regardless of layout.
            raw_dir = Path(artifacts_dir) / "raw"
            # New raw inputs are staged here first and only swapped into raw_dir
            # once every file has saved and normalized cleanly. A same-day retry
            # that fails partway through must not destroy a prior successful
            # attempt's raw/ files -- those are the audit/replay record for any
            # live orders that attempt already placed.
            staging_dir = Path(artifacts_dir) / "raw.tmp"
            # Live runs (dry_run=False) require non-empty raw_connector_inputs.
            # Dry-run callers that have not yet been updated may omit entirely.
            # Both None and empty-dict on live runs go through the fail-closed path
            # (inside the try) so a durable no-order run_state is always written.
            _needs_raw = raw_connector_inputs is not None or not config.dry_run
            if _needs_raw:
                try:
                    if raw_connector_inputs is None or (not config.dry_run and not raw_connector_inputs):
                        raise RuntimeError(
                            "scheduled live run requires non-empty raw_connector_inputs; "
                            "omitting or passing an empty mapping on a live run allows orders "
                            "without persisting connector responses first"
                        )
                    if required_raw_filenames and not config.dry_run:
                        missing = required_raw_filenames - set(raw_connector_inputs.keys())
                        if missing:
                            raise RuntimeError(
                                f"scheduled live run: required raw inputs absent from "
                                f"raw_connector_inputs: {sorted(missing)}"
                            )
                    # Stage this attempt's raw saves in a separate directory so a
                    # failure below never touches the existing raw_dir.
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir)
                    for filename, payload in raw_connector_inputs.items():
                        path = save_raw_input(
                            trading_day_raw, filename, payload, raw_dir=staging_dir
                        )
                        written = json.loads(path.read_text(encoding="utf-8"))
                        if "normalize_error" in written:
                            raise RuntimeError(
                                f"normalization failed for {filename!r}: {written['normalize_error']}"
                            )
                    # Every file staged and normalized cleanly -- swap the staging
                    # directory into place as this run's raw/, replacing whatever
                    # a prior attempt left there. Preserve any post-placement
                    # order history from the prior attempt first: if that run
                    # placed an order and this retry is deduplicated (broker
                    # dedup means no new order), the fill snapshot must survive
                    # for slippage reporting and is not re-produced this run.
                    if raw_dir.exists():
                        _prior_oap = raw_dir / "orders_after_place.json"
                        _staged_oap = staging_dir / "orders_after_place.json"
                        if _prior_oap.exists() and not _staged_oap.exists():
                            shutil.copy2(_prior_oap, _staged_oap)
                    # Keep a backup until the rename succeeds so a filesystem error
                    # can't destroy the prior audit record: rename raw/ → raw.bak,
                    # rename staging/ → raw/, then remove raw.bak. On any failure,
                    # restore raw.bak → raw/.
                    #
                    # A stale raw.bak can be left by a prior interrupted swap:
                    # - If raw/ also exists the prior swap completed but cleanup did
                    #   not; raw.bak is stale -- remove it so the rename below can
                    #   proceed.
                    # - If raw/ is absent the staging rename failed last time; raw.bak
                    #   holds the real prior data -- restore it before proceeding.
                    raw_backup = raw_dir.with_name("raw.bak")
                    if raw_backup.exists():
                        if raw_dir.exists():
                            # Stale backup from an interrupted prior swap: raw/ was
                            # already restored so the backup is redundant. Remove it
                            # explicitly (no ignore_errors) so a failure surfaces here
                            # rather than silently causing the rename below to fail.
                            shutil.rmtree(raw_backup)
                        else:
                            raw_backup.rename(raw_dir)
                    if raw_dir.exists():
                        raw_dir.rename(raw_backup)
                        try:
                            staging_dir.rename(raw_dir)
                        except Exception:
                            raw_backup.rename(raw_dir)
                            raise
                        shutil.rmtree(raw_backup, ignore_errors=True)
                    else:
                        staging_dir.rename(raw_dir)
                    raw_persistence_state = build_raw_persistence_state(
                        trading_day_raw, raw_dir=raw_dir
                    )
                    # Rebuild both providers from the persisted, normalized files so
                    # run_daily_compact() computes from the exact archived snapshot and
                    # not potentially-stale live MCP calls made at a different time.
                    finance_data, robinhood_data = _build_frozen_providers_from_raw(
                        finance_data, robinhood_data, raw_dir,
                        strict_finance=not config.dry_run,
                        strict_robinhood=not config.dry_run,
                    )
                except Exception as exc:  # noqa: BLE001 - failure must still write durable scheduled state
                    # Remove only the staging directory; raw/ has been restored
                    # from raw.bak (if a prior attempt populated it) so its audit
                    # trail survives this failed retry.
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    out = Path(artifacts_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    # Remove stale derived/report artifacts from any prior
                    # attempt so they are not mistaken for this failed run's output.
                    # Include drift reports: a prior clean run may have written
                    # drift_report.json/md; leaving them next to a new raw-persistence-
                    # failure run_state would mislead operators about the current status.
                    # Do NOT delete orders.csv or buy_candidates.csv: a retry in the
                    # same directory may follow a previously-successful attempt whose
                    # real fill records must survive for reporting and cap accounting.
                    for _stale in (
                        "signals.csv",
                        "latest_run_summary.json", "slippage_and_coverage_report.md",
                        "slippage_ranked.csv", "missing_fills_report.csv",
                        "notion_latest_run_compact.md", "notion_latest_run_payload.json",
                        "drift_report.json", "drift_report.md",
                    ):
                        (out / _stale).unlink(missing_ok=True)
                    failure_state = raw_persistence_failure_run_state(
                        trading_day_raw,
                        str(exc),
                        raw_dir=raw_dir,
                        broker=config.broker.value,
                        triggered_at_utc=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        generated_at=now.astimezone(EASTERN).strftime("%Y-%m-%dT%H:%M:%S"),
                        budget={
                            "budget_tier": state.current_tier().name,
                            "daily_cap_usd": state.current_tier().daily_cap_usd,
                            "weekly_cap_usd": state.effective_weekly_cap(),
                        },
                    )
                    # Preserve orders.csv from a prior successful run so that
                    # load_prior_same_day_allocations() can still find the fills
                    # even when this retry's raw-persistence fails.
                    _prior_orders_csv = out / "orders.csv"
                    if _prior_orders_csv.exists():
                        failure_state["artifacts"] = {"orders_csv_path": str(_prior_orders_csv)}
                    run_state_path = out / "run_state.json"
                    atomic_write_text(run_state_path, json.dumps(failure_state, indent=2) + "\n")
                    # Route this fail-closed raw-persistence outcome through the same
                    # scheduled artifact contract as the normal finalizer: write the
                    # header-only submitted_orders.csv and the mirrored canonical
                    # run_summary.json (no signals were computed, so signals.csv is
                    # not required), overwriting any stale copies from a prior attempt.
                    # Without this the early return bypassed the contract entirely
                    # while still updating the scheduled pointer. A contract breach
                    # here (persisted as a blocked/invalid state) also blocks the
                    # pointer, so it is not recorded as valid scheduled evidence.
                    contract_ok = bool(
                        _write_and_validate_scheduled_contract(
                            failure_state, out, require_signals=False
                        )
                    )
                    if classification_state_root is not None and contract_ok:
                        update_latest_scheduled_run_pointer(
                            failure_state, run_state_path, base=Path(classification_state_root)
                        )
                    elif classification_state_root is not None:
                        logger.error(
                            "Not updating latest_scheduled_run.json: raw-persistence "
                            "failure-state artifact contract failed."
                        )
                    # signals.csv was already deleted above, so detect_drift()
                    # finds an empty expected set and returns drift_detected=False —
                    # _run_drift_check() would log nothing and send no Slack alert.
                    # Emit the raw-persistence alert directly instead so operators
                    # are paged on the documented missing-raw failure mode.
                    failure_result: Dict[str, Any] = {
                        "run_label": run_label,
                        "scheduled": scheduled,
                        "compact": True,
                        "blocked": False,
                        "raw_persistence_error": str(exc),
                        "artifacts_dir": str(out),
                        "run_state_path": str(run_state_path),
                    }
                    try:
                        from scripts.drift_detection import (
                            CAUSE_RAW_MISSING,
                            DEFAULT_SLACK_WEBHOOK_ENV,
                            DriftReport,
                            send_slack_alert,
                        )
                        _fail_report = DriftReport(
                            run_dir=str(out),
                            drift_detected=True,
                            affected_symbols=[],
                            run_suspected_cause=CAUSE_RAW_MISSING,
                            recommended_action=(
                                "Raw input persistence failed before signal computation; no orders "
                                "were placed. Re-run the live task after confirming connector "
                                "responses are available and raw/ can be written."
                            ),
                            signals_present=False,
                            orders_present=False,
                            submitted_orders_empty=True,
                            raw_dir_present=False,
                            expected_symbol_count=0,
                            submitted_symbol_count=0,
                            notes=[f"raw_persistence_error: {exc}"],
                        )
                        logger.warning(
                            "RAW-PERSISTENCE FAILURE in %s — no signals computed, no orders placed: %s",
                            out,
                            exc,
                        )
                        atomic_write_text(
                            out / "drift_report.json", _fail_report.to_json(compact=False)
                        )
                        atomic_write_text(out / "drift_report.md", _fail_report.to_markdown())
                        if scheduled and not config.dry_run:
                            send_slack_alert(_fail_report, env_var=DEFAULT_SLACK_WEBHOOK_ENV)
                        failure_result["drift_report"] = _fail_report.to_dict()
                    except Exception as _alert_exc:  # pragma: no cover
                        logger.warning("raw-persistence failure alert failed: %s", _alert_exc)
                    return failure_result

    if scheduled and prior_same_day_allocations is None:
        trading_day = now.astimezone(EASTERN).date()
        if prior_allocation_state_root is None:
            # Auto-derive the afternoon task root and fold in the morning/midday
            # stream roots (incl. manual override reruns), so morning, midday,
            # and manual override same-day fills all offset the afternoon buy.
            # Each stream's root is read from its OWN config rather than assumed
            # from directory-naming convention, so a relocated artifact root is
            # still found. An explicit prior_allocation_state_root overrides all
            # of this and scans only the tree the caller named.
            afternoon_root = _default_prior_allocation_state_root(Path(artifacts_dir))
            cross_stream_roots = _resolve_cross_stream_prior_allocation_roots(
                config_dir, afternoon_root
            )
            # All roots are capped at the actual run time so replayed/backfilled
            # runs never load fills created after the scheduled execution window.
            # The current run's own artifact is excluded by classification
            # (run_origin != "scheduled" or stream mismatch) not by time, so
            # passing `now` here is safe and captures late manual fills (e.g.
            # a 15:05 manual override under ce16ba6b/manual_runs).
            prior_same_day_allocations = load_prior_same_day_allocations_multi(
                (afternoon_root,), trading_day,
                boundary_et=now.astimezone(EASTERN).time(),
            )
            cross_stream_allocs = load_prior_same_day_allocations_multi(
                cross_stream_roots, trading_day,
                boundary_et=now.astimezone(EASTERN).time(),
            )
            for ticker, alloc in cross_stream_allocs.items():
                existing = prior_same_day_allocations.get(ticker)
                if existing is None:
                    prior_same_day_allocations[ticker] = alloc
                else:
                    ref = (
                        None
                        if existing.reference_price is None or alloc.reference_price is None
                        else min(existing.reference_price, alloc.reference_price)
                    )
                    # Pick the chronologically FIRST allocation's base rather than
                    # always keeping `existing` (the afternoon root, scanned first
                    # above) -- an earlier morning/midday allocation must win over a
                    # later same-day manual override recorded under the afternoon
                    # root, even though the afternoon root loads first (Codex
                    # review: "Select the base from the chronologically first
                    # allocation").
                    base, base_at = _earlier_selected_base(
                        existing.selected_base_amount_usd,
                        existing.selected_base_amount_usd_at,
                        alloc.selected_base_amount_usd,
                        alloc.selected_base_amount_usd_at,
                    )
                    prior_same_day_allocations[ticker] = PriorSameDayAllocation(
                        ticker=ticker,
                        amount_usd=existing.amount_usd + alloc.amount_usd,
                        reference_price=ref,
                        run_id=alloc.run_id or existing.run_id,
                        source=alloc.source or existing.source,
                        selected_base_amount_usd=base,
                        selected_base_amount_usd_at=base_at,
                    )
            # Manual-context discovery (scripts.run_classification.load_same_day_top_up_context)
            # only runs for the auto-derived root: it scans SIBLINGS of afternoon_root's
            # parent (the production cron_tracking layout), which would silently pull in
            # unrelated trees for a caller that passed an explicit prior_allocation_state_root
            # to scan ONLY that tree (Codex review: "Respect explicit prior-allocation root
            # isolation"). Capped at the run time for the same replay-safety reason as above
            # (Codex review: "Bound manual top-up context to the scheduled execution time").
            merge_manual_top_up_context(
                prior_same_day_allocations,
                load_same_day_top_up_context(
                    trading_day.isoformat(), base=afternoon_root,
                    boundary_et=now.astimezone(EASTERN).time(),
                ),
            )
        else:
            prior_same_day_allocations = load_prior_same_day_allocations(
                prior_allocation_state_root, trading_day,
                boundary_et=now.astimezone(EASTERN).time(),
            )
    if deployment_history_root is None:
        deployment_history_root = Path(artifacts_dir).parent
    result = run_daily_compact(
        now,
        config,
        state,
        finance_data,
        robinhood_data,
        broker,
        artifacts_dir=artifacts_dir,
        run_label=run_label,
        scheduled=scheduled,
        prior_same_day_allocations=prior_same_day_allocations,
        deployment_history_root=deployment_history_root,
        classification_state_root=classification_state_root,
        raw_persistence_state=raw_persistence_state,
    )
    if result.get("blocked"):
        logger.warning("run blocked by date gate for %s; skipping reports", result.get("run_state_path"))
        return result
    if scheduled and not config.dry_run:
        # Persist raw placement responses so fill data is replayable from
        # durable storage without a live broker re-query at reporting time.
        raw_responses = getattr(broker, "get_raw_placement_responses", lambda: [])()
        if raw_responses:
            raw_dir = Path(artifacts_dir) / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            oap_path = raw_dir / "orders_after_place.json"
            # Merge with any prior placement history so a partial retry (where some
            # orders are broker-deduplicated and others are newly placed) preserves
            # fill snapshots from both attempts. New responses take precedence on
            # order_id collision so the freshest broker state always wins.
            prior_orders: list = []
            if oap_path.exists():
                try:
                    prior_orders = json.loads(oap_path.read_text(encoding="utf-8")).get("orders", []) or []
                except (json.JSONDecodeError, OSError, AttributeError):
                    prior_orders = []
            new_ids = {r.get("order_id") or r.get("id") for r in raw_responses} - {None}
            merged = raw_responses + [o for o in prior_orders if (o.get("order_id") or o.get("id")) not in new_ids]
            atomic_write_text(oap_path, json.dumps({"orders": merged}, indent=2) + "\n")
            result["orders_after_place_path"] = str(oap_path)
        drift = _run_drift_check(Path(artifacts_dir))
        if drift is not None:
            result["drift_report"] = drift
    if not generate_full_reports:
        return result

    # Imported lazily so the compact daily path has no dependency on the report layer.
    from dca_notion_sync import write_compact_sync
    from dca_reporting import generate_reports

    out = Path(artifacts_dir)
    run_state = json.loads((out / "run_state.json").read_text(encoding="utf-8"))
    orders_csv = out / "orders.csv"
    fills = _fills_from_orders_csv(orders_csv)
    # When raw/orders_after_place.json was persisted at order time, use it for
    # fill data so the slippage report is replayable from the same durable file
    # rather than re-querying the broker at reporting time.
    orders_after_place = out / "raw" / "orders_after_place.json"
    if orders_after_place.exists():
        from dca_reporting import fills_from_order_history_payload
        order_id_to_ticker: Dict[str, str] = {}
        if orders_csv.exists():
            with orders_csv.open(newline="", encoding="utf-8") as _fh:
                for _row in csv.DictReader(_fh):
                    _oid = _row.get("order_id")
                    _tkr = _row.get("ticker")
                    if _oid and _tkr:
                        order_id_to_ticker[_oid] = _tkr
        persisted_fills = fills_from_order_history_payload(
            json.loads(orders_after_place.read_text(encoding="utf-8")),
            order_id_to_ticker=order_id_to_ticker,
        )
        persisted_ids = {f.get("order_id") for f in persisted_fills if f.get("order_id")}
        fills = [f for f in fills if f.get("order_id") not in persisted_ids] + persisted_fills
        # The persisted snapshot captures order state at placement time; orders that
        # were accepted/open then may have settled by now. Refresh any order ID that
        # appears in orders.csv but not in persisted_fills (i.e. not yet filled at
        # placement time) so they are not permanently reported as execution gaps.
        fills = _refresh_fills_from_broker(orders_csv, broker, fills)
    else:
        # Fallback: re-query the broker for any submitted-but-unfilled order so
        # it is not reported as a missing execution gap.
        fills = _refresh_fills_from_broker(orders_csv, broker, fills)
    # Align candidate target prices with the durable execution records so the slippage
    # report ranks against the same target the orders CSV uses (the signal-time quote).
    targets = _execution_targets_from_orders_csv(orders_csv)
    for candidate in run_state.get("buy_candidates") or []:
        target = targets.get(candidate.get("ticker"))
        if target is not None:
            candidate["target_price"] = target
            candidate["target_price_missing"] = False
    report_paths = generate_reports(run_state, fills, out)
    summary = json.loads(Path(report_paths["latest_run_summary"]).read_text(encoding="utf-8"))
    sync_paths = write_compact_sync(summary, out)
    result["reports"] = report_paths
    result["notion_sync"] = sync_paths
    logger.info("Generated full post-trade reports in %s", out)
    return result


class DemoDataProvider:
    """Deterministic in-memory :class:`MarketDataProvider` for ``--demo`` / CI.

    Generates a downward intraday path so some tickers trigger a graded buy. The drop
    depth is a stable function of the ticker so runs are reproducible without any random
    source. This is a self-test harness — it never represents real market data.
    """

    def __init__(self, source: SourceName, previous_close: float = 100.0) -> None:
        self.source = source
        self._previous_close = previous_close

    def _drop_fraction(self, ticker: str) -> float:
        # 0.0 .. ~4.5% depending on the ticker, stable across runs.
        return (sum(ord(ch) for ch in ticker) % 46) / 1000.0

    def get_intraday_bars(
        self, ticker: str, start: datetime, end: datetime, interval_minutes: int
    ) -> List[Bar]:
        drop = self._drop_fraction(ticker)
        decision_close = self._previous_close * (1.0 - drop)
        # Lookback (session open) sits above the decision close -> 60-minute trend down.
        open_price = self._previous_close * (1.0 + 0.002)
        bars: List[Bar] = []
        cursor = start
        steps = max(1, int((end - start).total_seconds() // (interval_minutes * 60)))
        for i in range(steps + 1):
            frac = i / steps
            price = open_price + (decision_close - open_price) * frac
            bars.append(
                Bar(
                    timestamp=cursor,
                    open=price,
                    high=price * 1.001,
                    low=price * 0.999,
                    close=price,
                    volume=1000.0,
                )
            )
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        timestamp = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        p = self._previous_close
        return Bar(timestamp=timestamp, open=p, high=p, low=p, close=p, volume=0.0)


def _resolve_now(now_arg: Optional[str], config: StrategyConfig) -> datetime:
    """Return the decision-time datetime for the run (parsed --now, or today's gate time)."""
    if now_arg:
        parsed = datetime.fromisoformat(now_arg)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=EASTERN)
    today = datetime.now(tz=EASTERN).date()
    return datetime.combine(today, config.decision_time_et, tzinfo=EASTERN)


def _demo_runtime(config: StrategyConfig) -> Tuple[MarketDataProvider, MarketDataProvider, BrokerAdapter]:
    finance = DemoDataProvider(SourceName.FINANCE)
    robinhood = DemoDataProvider(SourceName.ROBINHOOD)
    broker = build_broker(config)  # DryRunBroker for the demo
    return finance, robinhood, broker


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Near-close graded DCA orchestrator (dry-run CLI).")
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR), help="Directory with config/*.json (default: bundled config/).")
    parser.add_argument("--artifacts-dir", default=None, help="Output directory (default: artifacts/<trading-day>).")
    parser.add_argument("--now", default=None, help="Override decision time (ISO 8601, e.g. 2026-07-06T15:45). For testing.")
    parser.add_argument("--run-label", default="daily", help="Run label recorded in artifacts.")
    parser.add_argument("--report", action="store_true", help="Also render the full slippage/coverage reports and Notion sync.")
    parser.add_argument("--demo", action="store_true", help="Use the deterministic in-memory demo provider (no broker/network).")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG/INFO/WARNING/ERROR).")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    config = load_strategy_config(args.config_dir)
    if not config.dry_run:
        logger.error("Refusing to run: config.dry_run is False. The CLI is dry-run only; drive live runs via run_pipeline().")
        return 2

    now = _resolve_now(args.now, config)
    trading_day = now.astimezone(EASTERN).date()
    artifacts_dir = Path(args.artifacts_dir) if args.artifacts_dir else Path("artifacts") / trading_day.isoformat()

    if not args.demo:
        logger.error(
            "No live data providers available to the CLI. Run with --demo for a local dry-run, "
            "or drive a live run from the scheduler via run_dca.run_pipeline() with wired MCP callables."
        )
        return 2

    logger.info("Starting DEMO dry-run for %s -> %s", trading_day, artifacts_dir)
    finance, robinhood, broker = _demo_runtime(config)
    state = StrategyState()
    result = run_pipeline(
        now,
        config,
        state,
        finance,
        robinhood,
        broker,
        artifacts_dir=artifacts_dir,
        run_label=args.run_label,
        generate_full_reports=args.report,
    )
    logger.info("Done. Artifacts:\n%s", json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
