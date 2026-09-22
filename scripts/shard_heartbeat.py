"""Periodic heartbeat daemon for multi-shard walkforward training runs.

Monitors active run_walkforward processes and appends a status/telemetry heartbeat
line to each active shard's stdout.log at a configurable interval (default: 10 minutes),
keeping log modification timestamps fresh and monitoring dashboards updated.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def find_active_shard_processes(run_dir_base: Path) -> dict[int, tuple[int, Path]]:
    """Scan the process table for active run_walkforward processes matching shard directories.

    Returns:
        Mapping of shard_id -> (pid, shard_dir).
    """
    active_shards: dict[int, tuple[int, Path]] = {}
    try:
        ps_output = subprocess.check_output(
            ["ps", "-eo", "pid,command"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return active_shards

    # Match: python ... run_walkforward.py ... shard_X
    pattern = re.compile(r"^\s*(\d+)\s+.*run_walkforward\.py.*?(shard_(\d+))")
    for line in ps_output.splitlines():
        match = pattern.search(line)
        if match:
            pid = int(match.group(1))
            shard_name = match.group(2)
            shard_id = int(match.group(3))
            shard_dir = run_dir_base / shard_name
            if shard_dir.is_dir():
                active_shards[shard_id] = (pid, shard_dir)

    return active_shards


def get_process_telemetry(pid: int) -> tuple[str, str]:
    """Retrieve accumulated CPU time and current CPU percentage for a PID."""
    try:
        output = subprocess.check_output(
            ["ps", "-o", "cputime,pcpu", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip().splitlines()
        if len(output) > 1:
            parts = output[1].split()
            if len(parts) >= 2:
                return parts[0], parts[1]
    except (subprocess.SubprocessError, OSError):
        pass
    return "unknown", "unknown"


def emit_heartbeats(run_dir_base: Path) -> int:
    """Emit a single heartbeat line to stdout.log for all active shards.

    Returns:
        Number of active shards updated.
    """
    active_shards = find_active_shard_processes(run_dir_base)
    if not active_shards:
        return 0

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    for shard_id, (pid, shard_dir) in sorted(active_shards.items()):
        cputime, pcpu = get_process_telemetry(pid)
        log_path = shard_dir / "stdout.log"
        line = (
            f"[{utc_now}] [heartbeat] shard_{shard_id} (pid {pid}) active: "
            f"{cputime} CPU time ({pcpu}% CPU) | training in progress\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
            print(f"Updated {log_path.name} for shard_{shard_id}: {cputime} CPU ({pcpu}%)", flush=True)
        except OSError as exc:
            print(f"Failed to write heartbeat to {log_path}: {exc}", file=sys.stderr, flush=True)

    return len(active_shards)


def run_heartbeat_daemon(run_dir_base: Path, interval_seconds: int = 600, once: bool = False) -> int:
    """Run the heartbeat monitoring loop until all shard processes exit."""
    print(f"Starting shard heartbeat monitor (interval: {interval_seconds}s, base: {run_dir_base})", flush=True)
    while True:
        count = emit_heartbeats(run_dir_base)
        if once:
            return 0
        if count == 0:
            print("No active shard training processes detected. Heartbeat monitor exiting.", flush=True)
            return 0
        time.sleep(interval_seconds)


def main() -> int:
    """CLI entry point for shard heartbeat monitor."""
    parser = argparse.ArgumentParser(description="Emit periodic heartbeat logs for active training shards.")
    parser.add_argument(
        "--run-dir-base",
        type=Path,
        default=Path("analysis/real-octopus-run"),
        help="Base directory containing shard subdirectories (default: analysis/real-octopus-run)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=600,
        help="Interval between heartbeat writes in seconds (default: 600 seconds / 10 minutes)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Emit a single heartbeat and exit immediately",
    )
    args = parser.parse_args()
    return run_heartbeat_daemon(args.run_dir_base, interval_seconds=args.interval, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
