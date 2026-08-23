#!/usr/bin/env python3
"""Rebuild next-session per-ticker DCA triggers after a ticker-update event.

This is a separate Claude-only consumer of the nightly ticker-history update
skill.  It never updates Yahoo data itself: the upstream event tells it which
symbols changed, and an active symbol change triggers a new calibration.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from dca_ticker_paths import configured_ticker_history_dir
from scripts.generate_per_ticker_sizing import (
    archive_unavailable_reason,
    build_sizing_payload,
    session_count_regressions,
)

# Kept as private aliases: every call site below predates these guards'
# move into generate_per_ticker_sizing.py (Codex review: "Refuse default
# calibration when the archive is missing", "Check session regressions in
# the standalone publisher" -- share one implementation of each guard
# between this module's event-driven publish path and that module's own
# standalone CLI, so neither can drift out of sync with the other).
_archive_unavailable_reason = archive_unavailable_reason
_session_count_regressions = session_count_regressions


def _build_and_publish(
    history_dir: Path, ticker_config: Path, output: Path, now: datetime
) -> dict[str, Any]:
    """Build a calibration and publish it, or refuse to and say why.

    Shared by both the event-driven and standalone publish paths so neither
    can atomically replace the live model without first passing the same
    archive-availability and per-ticker regression checks.
    """
    unavailable = _archive_unavailable_reason(history_dir, ticker_config)
    if unavailable is not None:
        return {"status": "blocked_archive_unavailable", "archive_unavailable": unavailable}
    payload = build_sizing_payload(history_dir, ticker_config, as_of=now.date())
    regressions = _session_count_regressions(payload["tickers"], output)
    if regressions:
        return {"status": "blocked_session_count_regression", "session_count_regressions": regressions}
    atomic_write_text(output, json.dumps(payload, indent=2) + "\n")
    return {
        "status": "completed",
        "calibrated_tickers": len(payload["tickers"]),
        "fallback_tickers": payload["fallback_tickers"],
    }


def rebuild_after_ticker_update(
    *,
    history_dir: Path,
    ticker_config: Path,
    output: Path,
    artifacts_dir: Path,
    now: datetime,
    ticker_update_event: Path,
    force: bool = False,
) -> dict[str, Any]:
    """Publish a model from an event, or explicitly as a standalone run."""
    try:
        update = json.loads(ticker_update_event.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        update = {"status": "missing_or_invalid", "error": str(exc)}
    changed_active = update.get("changed_active_tickers", [])
    if not isinstance(changed_active, list) or not all(isinstance(ticker, str) for ticker in changed_active):
        changed_active = []

    result: dict[str, Any] = {
        "run_at": now.isoformat(),
        "history_dir": str(history_dir),
        "output": str(output),
        "upstream_ticker_update_event": str(ticker_update_event),
        "upstream_status": update.get("status"),
        "triggering_tickers": sorted(ticker.upper() for ticker in changed_active),
        "execution_owner": "claude_only",
        "perplexity_involved": False,
        "trigger_source": "standalone" if force else "ticker_update_event",
    }
    if force:
        result.update(_build_and_publish(history_dir, ticker_config, output, now))
    elif update.get("status") != "completed":
        result["status"] = "blocked_upstream_ticker_update"
    elif not changed_active:
        result["status"] = "skipped_no_active_ticker_change"
    else:
        missing = [ticker for ticker in changed_active if not (history_dir / f"{ticker}.csv").is_file()]
        if missing:
            result["status"] = "blocked_missing_updated_history"
            result["missing_histories"] = sorted(missing)
        else:
            result.update(_build_and_publish(history_dir, ticker_config, output, now))

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        artifacts_dir / "calibration_run.json", json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, default=configured_ticker_history_dir())
    parser.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
    parser.add_argument("--output", type=Path, default=Path("config/per_ticker_sizing.json"))
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--ticker-update-event", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run independently of the ticker-update event; intended for the scheduled standalone run.",
    )
    args = parser.parse_args()
    now = datetime.now().astimezone()
    artifacts_dir = args.artifacts_dir or Path(
        stream_root("per_ticker_calibration", now.date().isoformat())
    )
    ticker_update_event = args.ticker_update_event or Path(
        stream_root("ticker_history_update", now.date().isoformat(), "ticker_update_event.json")
    )
    result = rebuild_after_ticker_update(
        history_dir=args.history_dir,
        ticker_config=args.ticker_config,
        output=args.output,
        artifacts_dir=artifacts_dir,
        now=now,
        ticker_update_event=ticker_update_event,
        force=args.force,
    )
    print(f"{result['status'].upper()} triggers={len(result['triggering_tickers'])}")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
