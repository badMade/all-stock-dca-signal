#!/usr/bin/env python3
"""Run the configured resumable ticker-history updater to completion or a safe stop.

This is the program-facing implementation of the project Claude skill
``tickers-daily-update``.  It runs the maintained external updater in bounded
passes, records a concise archive log, and never invents a fallback downloader
when that updater is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from dca_ticker_paths import configured_ticker_history_dir
from scripts.file_lock import cross_process_lock

_UPDATED_RE = re.compile(r"DONE_THIS_RUN\s+updated=(\d+)\s+remaining=(\d+)")


def _append_log(history_dir: Path, line: str) -> None:
    """Append a durable, serialized one-line archive-update record."""
    log_path = history_dir / "_daily_update_log.txt"
    with cross_process_lock(log_path.with_suffix(".lock")):
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        atomic_write_text(log_path, existing + line + "\n")


def _history_mtimes(history_dir: Path) -> dict[str, int]:
    """Snapshot CSV modification times so an updated ticker can notify consumers."""
    return {
        path.stem.upper(): path.stat().st_mtime_ns
        for path in history_dir.glob("*.csv")
        if path.is_file()
    }


def _active_tickers(ticker_config: Path) -> set[str]:
    """Read the calibrated DCA symbols without making the archive depend on DCA code.

    Includes ``whole_share`` alongside ``active``: generate_per_ticker_sizing.py's
    build_sizing_payload() calibrates that SAME active-plus-whole-share universe
    (a whole-share ticker like BYDDY is evaluated by the identical graded ladder --
    see docs/whole_share_accumulation.md), but this function feeds
    changed_active_tickers, which gates whether a history-update event even
    triggers daily_per_ticker_calibration.py's rebuild at all. Without this, a
    BYDDY-only history update event could never trigger recalibration, and
    scripts/refresh_active_ticker_histories.py's active_symbols() (this
    function's own sibling for the OTHER maintenance path) would never even
    fetch its missing history in the first place -- leaving whole-share sizing
    permanently on the $1 fallback with no documented manual path to fix it
    (Codex review: "Include whole-share symbols in history-update events").
    """
    data = json.loads(ticker_config.read_bytes())
    active = data.get("active")
    if not isinstance(active, list) or not all(isinstance(ticker, str) for ticker in active):
        raise ValueError(f"{ticker_config} must contain an active ticker list")
    whole_share = data.get("whole_share", [])
    if not isinstance(whole_share, list) or not all(isinstance(ticker, str) for ticker in whole_share):
        raise ValueError(f"{ticker_config}'s whole_share list, if present, must be a list of strings")
    return {ticker.upper() for ticker in active} | {ticker.upper() for ticker in whole_share}


#: Default ceiling on one updater subprocess invocation. The external updater
#: fetches from Yahoo per ticker across the whole active universe; a hung
#: network request must not be able to wedge the nightly task forever.
DEFAULT_INVOCATION_TIMEOUT_SECONDS = 600.0


def run_incremental_update(
    *,
    updater_script: Path,
    history_dir: Path,
    max_invocations: int = 40,
    ticker_config: Path | None = None,
    artifacts_dir: Path | None = None,
    now: datetime | None = None,
    invocation_timeout_seconds: float = DEFAULT_INVOCATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the updater until completion, failure, or repeated no-progress.

    The external script remains the sole owner of Yahoo fetch/merge semantics.
    This wrapper only recognizes its documented progress lines and preserves a
    short run summary for analysis.
    """
    if not updater_script.is_file():
        raise FileNotFoundError(f"configured ticker updater does not exist: {updater_script}")
    if not history_dir.is_dir():
        raise FileNotFoundError(
            f"ticker history directory does not exist: {history_dir} -- set "
            "DCA_TICKER_HISTORY_DIR to the shared archive location (required "
            "when this package is not run from a repository checkout; see "
            "dca_ticker_paths.configured_ticker_history_dir)"
        )
    if max_invocations < 1:
        raise ValueError("max_invocations must be at least one")

    started_at = now or datetime.now().astimezone()
    before_mtimes = _history_mtimes(history_dir)
    # Resolved to an absolute path once, up front: a manifest-supplied
    # relative path (e.g. "scripts/update.py") resolves against the CURRENT
    # process's cwd here, but the subprocess below runs with its cwd set to
    # the script's own parent directory. Passing the still-relative path as
    # the subprocess argv would then have Python look for it a second time
    # under that parent (e.g. "scripts/scripts/update.py"), silently failing
    # every invocation (Codex review: "Resolve the updater path before
    # switching directories").
    resolved_script = updater_script.resolve()
    updated_total = 0
    remaining: int | None = None
    stagnant_runs = 0
    completed = False
    errors: list[str] = []
    updater_env = os.environ.copy()
    updater_env["TICKERS_DIR"] = str(history_dir.resolve())
    for invocation in range(1, max_invocations + 1):
        try:
            result = subprocess.run(  # nosec B603 - explicit Python executable and user-configured local script
                [sys.executable, str(resolved_script)],
                cwd=resolved_script.parent,
                capture_output=True,
                text=True,
                check=False,
                env=updater_env,
                timeout=invocation_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            # A hung invocation (e.g. the updater blocked in a Yahoo network
            # request) must degrade to a safe partial-update event rather
            # than leave the nightly task -- and anything gated on its
            # completion -- blocked indefinitely (Codex review: "Bound each
            # updater subprocess invocation").
            errors.append(
                f"invocation {invocation} exceeded the {invocation_timeout_seconds:.0f}s timeout"
            )
            break
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0:
            detail = output.strip().splitlines()[-1] if output.strip() else "no process output"
            errors.append(f"invocation {invocation} exited {result.returncode}: {detail}")
            break
        if "ALL_DONE" in output:
            completed = True
            remaining = 0
            break
        matches = _UPDATED_RE.findall(output)
        if not matches:
            errors.append(f"invocation {invocation} emitted no recognized progress line")
            break
        updated, next_remaining = (int(value) for value in matches[-1])
        updated_total += updated
        if remaining is not None and next_remaining >= remaining:
            stagnant_runs += 1
        else:
            stagnant_runs = 0
        remaining = next_remaining
        if stagnant_runs >= 3:
            errors.append("remaining count did not decrease across four consecutive invocations")
            break

    status = "completed" if completed else "partial"
    after_mtimes = _history_mtimes(history_dir)
    changed_tickers = sorted(
        ticker
        for ticker, mtime in after_mtimes.items()
        if before_mtimes.get(ticker) != mtime
    )
    active_tickers = _active_tickers(ticker_config) if ticker_config is not None else set()
    summary = {
        "event_type": "ticker_history_update",
        "status": status,
        "completed_at": started_at.isoformat(),
        "invocations": invocation,
        "updated_total": updated_total,
        "remaining": remaining,
        "errors": errors,
        "changed_tickers": changed_tickers,
        "changed_active_tickers": sorted(set(changed_tickers) & active_tickers),
        "execution_owner": "claude_only",
        "perplexity_involved": False,
    }
    _append_log(
        history_dir,
        "status={status} invocations={invocations} updated={updated_total} remaining={remaining} errors={errors}".format(
            **summary
        ),
    )
    event_dir = artifacts_dir or Path(
        stream_root("ticker_history_update", started_at.date().isoformat())
    )
    event_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(event_dir / "ticker_update_event.json", json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-dir", type=Path, default=configured_ticker_history_dir()
    )
    env_script = os.environ.get("DCA_TICKER_DAILY_UPDATE_SCRIPT")
    parser.add_argument(
        "--updater-script", type=Path, default=Path(env_script) if env_script else None
    )
    parser.add_argument("--max-invocations", type=int, default=40)
    parser.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument(
        "--invocation-timeout-seconds",
        type=float,
        default=DEFAULT_INVOCATION_TIMEOUT_SECONDS,
    )
    args = parser.parse_args()
    if args.updater_script is None:
        parser.error(
            "set DCA_TICKER_DAILY_UPDATE_SCRIPT or provide --updater-script; "
            "history defaults to the repository ticker_data archive"
        )
    summary = run_incremental_update(
        updater_script=args.updater_script,
        history_dir=args.history_dir,
        max_invocations=args.max_invocations,
        ticker_config=args.ticker_config,
        invocation_timeout_seconds=args.invocation_timeout_seconds,
        artifacts_dir=args.artifacts_dir,
    )
    print(summary)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
