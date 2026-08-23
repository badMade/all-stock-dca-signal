#!/usr/bin/env python3
"""Execute ticker-history maintenance and per-ticker DCA recalculation.

The Claude skill and schedule manifest describe this workflow, but this module
is the executable implementation.  Its subcommands intentionally preserve the
two-job design:

* ``update`` refreshes the shared archive and writes its event.
* ``event-calibrate`` consumes that event and recalculates only after a fully
  completed update that changed an active DCA symbol.
* ``calibrate`` independently recalculates from the local archive.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dca_paths import stream_root
from dca_ticker_paths import configured_ticker_history_dir
from scripts.daily_per_ticker_calibration import rebuild_after_ticker_update
from scripts.run_tickers_daily_update import run_incremental_update


def run_update(
    *,
    updater_script: Path,
    history_dir: Path,
    ticker_config: Path,
    artifacts_dir: Path,
    max_invocations: int = 40,
) -> dict[str, Any]:
    """Run only the archive owner and publish its ticker-update event."""
    return run_incremental_update(
        updater_script=updater_script,
        history_dir=history_dir,
        ticker_config=ticker_config,
        artifacts_dir=artifacts_dir,
        max_invocations=max_invocations,
    )


def run_calibration(
    *,
    history_dir: Path,
    ticker_config: Path,
    output: Path,
    artifacts_dir: Path,
    ticker_update_event: Path,
    force: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the independent calibrator, optionally consuming an update event."""
    return rebuild_after_ticker_update(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=output,
        artifacts_dir=artifacts_dir,
        now=now or datetime.now().astimezone(),
        ticker_update_event=ticker_update_event,
        force=force,
    )


def _default_event_path(now: datetime) -> Path:
    return Path(stream_root("ticker_history_update", now.date().isoformat(), "ticker_update_event.json"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    update = subcommands.add_parser("update", help="Run only the ticker archive updater.")
    update.add_argument("--updater-script", type=Path, default=os.environ.get("DCA_TICKER_DAILY_UPDATE_SCRIPT"))
    update.add_argument("--history-dir", type=Path, default=configured_ticker_history_dir())
    update.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
    update.add_argument("--artifacts-dir", type=Path)
    update.add_argument("--max-invocations", type=int, default=40)

    for name, help_text, force in (
        ("event-calibrate", "Recalculate only when a completed update event changed an active ticker.", False),
        ("calibrate", "Independently recalculate from the shared local archive.", True),
    ):
        calibration = subcommands.add_parser(name, help=help_text)
        calibration.set_defaults(force=force)
        calibration.add_argument("--history-dir", type=Path, default=configured_ticker_history_dir())
        calibration.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
        calibration.add_argument("--output", type=Path, default=Path("config/per_ticker_sizing.json"))
        calibration.add_argument("--artifacts-dir", type=Path)
        calibration.add_argument("--ticker-update-event", type=Path)
    return parser


def main() -> int:
    """Dispatch one explicit maintenance action and print its JSON result."""
    args = _parser().parse_args()
    now = datetime.now().astimezone()
    if args.command == "update":
        if args.updater_script is None:
            raise SystemExit("set DCA_TICKER_DAILY_UPDATE_SCRIPT or provide --updater-script")
        artifact_dir = args.artifacts_dir or Path(stream_root("ticker_history_update", now.date().isoformat()))
        result = run_update(
            updater_script=args.updater_script,
            history_dir=args.history_dir,
            ticker_config=args.ticker_config,
            artifacts_dir=artifact_dir,
            max_invocations=args.max_invocations,
        )
        success = result["status"] == "completed"
    else:
        artifact_dir = args.artifacts_dir or Path(stream_root("per_ticker_calibration", now.date().isoformat()))
        event_path = args.ticker_update_event or _default_event_path(now)
        result = run_calibration(
            history_dir=args.history_dir,
            ticker_config=args.ticker_config,
            output=args.output,
            artifacts_dir=artifact_dir,
            ticker_update_event=event_path,
            force=args.force,
            now=now,
        )
        success = result["status"] in {"completed", "skipped_no_active_ticker_change"}
    print(json.dumps(result, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
