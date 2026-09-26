#!/usr/bin/env python3
"""Auto-resuming supervisor for Wondrous Octopus walk-forward training runs.

Monitors a walk-forward run and automatically restarts it with ``--resume --retrain``
if it halts due to transient filesystem locks, macOS inode sync shifts, or crashes,
preserving all checkpointed progress until the full history completes.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


def build_walkforward_cmd(run_dir: Path, python_bin: str = sys.executable) -> list[str]:
    """Construct the command line to resume walk-forward training."""
    return [
        python_bin,
        "wondrous-octopus/run_walkforward.py",
        "--resume",
        str(run_dir),
        "--retrain",
    ]


def build_export_cmd(run_dir: Path, python_bin: str = sys.executable) -> list[str]:
    """Construct the command line to export ledger predictions once finished."""
    return [
        python_bin,
        "scripts/export_octopus_ledger.py",
        "--run-dir",
        str(run_dir),
        "--all-scheduled",
        "--csv-out",
        str(run_dir / "predictions.csv"),
        "--report-md",
        "artifacts/octopus_evaluation_report.md",
        "--json-out",
        "artifacts/octopus_evaluation_metrics.json",
    ]


def run_supervisor(
    run_dir: Path,
    delay_seconds: float = 5.0,
    max_retries: int | None = None,
    auto_export: bool = True,
    dry_run: bool = False,
) -> int:
    """Supervise a walkforward run, restarting upon transient failures.

    Args:
        run_dir: Directory containing walkforward checkpoints and ledger.
        delay_seconds: Seconds to wait before restarting after a crash.
        max_retries: Maximum number of retries before failing (None for infinite).
        auto_export: Whether to run export_octopus_ledger.py on completion.
        dry_run: Print commands without executing.

    Returns:
        0 on successful completion, non-zero on failure or abort.
    """
    cmd = build_walkforward_cmd(run_dir)

    if dry_run:
        print(f"[Supervisor DRY RUN] Target directory: {run_dir}")
        print(f"[Supervisor DRY RUN] Resume command: {' '.join(cmd)}")
        if auto_export:
            export_cmd = build_export_cmd(run_dir)
            print(f"[Supervisor DRY RUN] Export command: {' '.join(export_cmd)}")
        return 0

    attempt = 0
    while True:
        attempt += 1
        print(f"\n[Supervisor] === Starting walk-forward run (Attempt #{attempt}) ===")
        print(f"[Supervisor] Command: {' '.join(cmd)}\n", flush=True)

        try:
            # Run the process directly attached to stdout/stderr so curses/bars display correctly
            proc = subprocess.Popen(cmd)
            exit_code = proc.wait()
        except KeyboardInterrupt:
            print("\n[Supervisor] Interrupted by operator (SIGINT). Terminating child...", file=sys.stderr)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            print("[Supervisor] Run aborted by user.", file=sys.stderr)
            return 130

        if exit_code == 0:
            print(f"\n[Supervisor] 🎉 Walk-forward run completed successfully at attempt #{attempt}!")
            if auto_export:
                print("[Supervisor] Running export_octopus_ledger.py to compile predictions.csv...")
                export_cmd = build_export_cmd(run_dir)
                export_res = subprocess.run(export_cmd)
                if export_res.returncode != 0:
                    print(f"[Supervisor] Warning: Export exited with code {export_res.returncode}", file=sys.stderr)
                    return export_res.returncode
            return 0

        print(
            f"\n[Supervisor] ⚠️ Process exited with error code {exit_code}.",
            file=sys.stderr,
        )

        if max_retries is not None and attempt >= max_retries:
            print(
                f"[Supervisor] Reached maximum retry limit ({max_retries}). Giving up.",
                file=sys.stderr,
            )
            return exit_code

        print(
            f"[Supervisor] Waiting {delay_seconds:.1f}s for filesystem locks/sync to settle before restarting...",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for auto_resume_walkforward."""
    parser = argparse.ArgumentParser(
        description="Auto-resuming supervisor for Wondrous Octopus walk-forward training.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path("artifacts/octopus_scheduler_deep_history"),
        help="Path to walkforward run directory (default: artifacts/octopus_scheduler_deep_history)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="Delay in seconds between retry attempts (default: 5.0)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Maximum retry attempts (default: unlimited)",
    )
    parser.add_argument(
        "--no-export",
        dest="auto_export",
        action="store_false",
        default=True,
        help="Skip automatic ledger export upon completion",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Display supervisor plan without executing",
    )

    args = parser.parse_args(argv)

    if not args.run_dir.exists() and not args.dry_run:
        print(f"Error: Run directory not found: {args.run_dir}", file=sys.stderr)
        return 1

    return run_supervisor(
        run_dir=args.run_dir,
        delay_seconds=args.delay,
        max_retries=args.max_retries,
        auto_export=args.auto_export,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
