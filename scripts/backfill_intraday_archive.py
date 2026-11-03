#!/usr/bin/env python3
"""Seed the intraday observation archive from run artifacts already on disk.

Every scheduled poll this repo has ever run wrote its prices into a
``*signals.csv`` beside a ``run_state.json`` under ``cron_tracking/``. Going
forward the executors write those observations into the shared per-ticker
archive themselves (see :mod:`dca_intraday_archive`); this script recovers the
history that predates that wiring, so the archive does not start empty.

It reads ``run_state.json`` -- not the CSV -- because the run state carries both
the ``signal_rows`` and the ``run_classification`` (stream, slot, run id,
scheduled-vs-manual) needed to key an observation. Historical rows will not have
the price-provenance columns the executors now emit; those land empty, and the
price itself falls back to ``decision_bar_close`` for near-close-shaped rows.

Idempotent: the archive replaces on ``(trading_day, stream, slot, ticker,
source)``, so re-running only refreshes what it already wrote.

    python -m scripts.backfill_intraday_archive --dry-run
    python -m scripts.backfill_intraday_archive --since 2026-07-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dca_intraday_archive import (  # noqa: E402
    IntradayObservation,
    append_observations,
    configured_intraday_archive_dir,
    observations_from_signal_rows,
)
from dca_paths import cron_tracking_root  # noqa: E402

logger = logging.getLogger("backfill_intraday_archive")

# Manual-override trees are NOT skipped. Their runs are excluded from every
# scheduled-evidence metric in this repo, but they still observed real prices at
# a real time, and this archive is analytics rather than evidence. Their rows
# stay distinguishable by the `manual_run` and `stream` columns.


def iter_run_states(root: Path) -> Iterator[Path]:
    """Yield every ``run_state.json`` under ``root``, deepest paths last."""
    yield from sorted(root.rglob("run_state.json"))


def load_run_state(path: Path) -> Optional[Dict[str, Any]]:
    try:
        loaded = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("skipping unreadable %s: %s", path, exc)
        return None
    if not isinstance(loaded, dict):
        logger.warning("skipping %s: run_state is not an object", path)
        return None
    return loaded


def observations_for_run_state(
    run_state: Dict[str, Any], path: Path
) -> List[IntradayObservation]:
    """Build observations for one run_state, or ``[]`` when it carries none."""
    signal_rows = run_state.get("signal_rows")
    if not isinstance(signal_rows, list) or not signal_rows:
        return []
    classification = run_state.get("run_classification")
    if not isinstance(classification, dict):
        # Pre-classification artifacts still identify their slot by folder name
        # (``YYYYMMDD_1030ET``); recover just enough to key the observation
        # rather than dropping a real day of prices on a schema technicality.
        classification = _classification_from_path(path)
    return observations_from_signal_rows(
        signal_rows,
        run_classification=classification,
        generated_at=run_state.get("generated_at") or run_state.get("as_of"),
        data_source=str(run_state.get("data_source") or ""),
        trading_day=run_state.get("trading_day"),
    )


def _classification_from_path(path: Path) -> Dict[str, Any]:
    """Derive stream/slot from a ``.../<stream>/YYYYMMDD_HHMMET/`` layout."""
    folder = path.parent.name
    stream = path.parent.parent.name
    slot = ""
    if "_" in folder and folder.endswith("ET"):
        slot = folder.split("_", 1)[1][:-2]
    return {"stream": stream, "scheduled_for_et": slot, "run_id": folder}


def _trading_day_from_path(path: Path) -> str:
    """Derive an ISO trading day from a legacy ``.../YYYYMMDD_HHMMET/`` folder."""
    date_part = path.parent.name.split("_", 1)[0]
    if len(date_part) == 8 and date_part.isdigit():
        return f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}"
    return ""


def _effective_trading_day(run_state: Dict[str, Any], path: Path) -> str:
    """Best-effort trading day for ``--since`` filtering, even for legacy runs.

    A legacy run_state.json predating the top-level ``trading_day`` field
    still names its own folder ``YYYYMMDD_HHMMET`` (or carries a
    ``run_classification.trading_day``), and observations_for_run_state
    already recovers a classification from that same path when building
    observations. ``--since`` must use that SAME recovered day, not an
    empty string, or a legacy artifact newer than the cutoff is silently
    skipped before that recovery ever runs (Codex review: "Derive the
    legacy day before applying --since").
    """
    trading_day = run_state.get("trading_day")
    if trading_day:
        return str(trading_day)
    classification = run_state.get("run_classification")
    if isinstance(classification, dict) and classification.get("trading_day"):
        return str(classification["trading_day"])
    return _trading_day_from_path(path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cron-tracking-root",
        default=None,
        help="Artifact root to scan (default: $DCA_CRON_TRACKING_ROOT or ./cron_tracking).",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="Archive directory to write (default: <ticker archive>/_intraday).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only backfill runs whose trading_day is on or after this ISO date.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without touching the archive.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    root = Path(args.cron_tracking_root or cron_tracking_root())
    if not root.exists():
        logger.error("artifact root %s does not exist", root)
        return 2
    # Pass None through to append_observations() when --archive-dir was not
    # given, rather than resolving configured_intraday_archive_dir() here
    # and passing that resolved Path in explicitly: append_observations()
    # treats ANY non-None archive_dir as an operator override and skips its
    # own guard against materializing a local stand-in when the managed
    # ticker_data archive is missing, so pre-resolving the default here
    # bypassed that guard for every default-path backfill run (Codex
    # review: "Let the default archive guard run for backfills"). The
    # resolved default is still used for the dry-run/log display below.
    archive_dir_override = Path(args.archive_dir) if args.archive_dir else None
    archive_dir_for_display = archive_dir_override or configured_intraday_archive_dir()

    collected: List[IntradayObservation] = []
    runs_seen = 0
    runs_used = 0
    for state_path in iter_run_states(root):
        run_state = load_run_state(state_path)
        if run_state is None:
            continue
        runs_seen += 1
        if args.since and _effective_trading_day(run_state, state_path) < args.since:
            continue
        observations = observations_for_run_state(run_state, state_path)
        if not observations:
            continue
        runs_used += 1
        collected.extend(observations)

    logger.info(
        "scanned %d run states under %s; %d carried signal rows -> %d observations",
        runs_seen,
        root,
        runs_used,
        len(collected),
    )
    if args.dry_run:
        tickers = sorted({obs.ticker for obs in collected})
        logger.info("dry run: would write %d tickers to %s", len(tickers), archive_dir_for_display)
        logger.info("tickers: %s", ", ".join(tickers) if tickers else "(none)")
        return 0
    if not collected:
        logger.info("nothing to backfill")
        return 0
    try:
        summary = append_observations(collected, archive_dir=archive_dir_override)
    except FileNotFoundError as exc:
        # append_observations() itself refuses the default archive location
        # when the managed ticker_data archive is missing/unmounted (see
        # archive_dir_override above); report that cleanly instead of a raw
        # traceback.
        logger.error("refusing to backfill: %s", exc)
        return 2
    logger.info("backfill complete: %s", json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
