#!/usr/bin/env python3
"""Run walk-forward replay across the entire ticker archive partitioned into shards.

Combines modern-window replay (--start 2024-01-01, weekly update cadence,
quarterly GBM refits) with clean ticker sharding so all 6,000+ archive tickers
can be trained and validated in balanced groups without memory bottlenecks.

Crash & Power Outage Safety:
- Atomic save points (checkpoints) are written and fsynced to disk every
  --checkpoint-every steps (default: 10 steps).
- If interrupted by a power outage, crash, or Ctrl+C, re-running automatically
  detects the checkpoint and resumes seamlessly from the last save point.
- Already-completed shards are automatically detected and skipped.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("DCA_LEDGER_MAX_LINKS", "2" if sys.platform == "darwin" else "1")

OCTOPUS_DIR = Path(__file__).resolve().parent
REPO_ROOT = OCTOPUS_DIR.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))
if str(OCTOPUS_DIR) not in sys.path:
    sys.path.insert(0, str(OCTOPUS_DIR))

from dca_bootstrap import ensure_venv  # noqa: E402

ensure_venv(
    packages=("numpy", "pandas", "sklearn", "torch"),
    requirements_files=(
        REPO_ROOT / "requirements-dev.txt",
        OCTOPUS_DIR / "requirements.txt",
    ),
)

from data import discover_available_tickers  # noqa: E402

from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402


def split_into_shards(tickers: Sequence[str], n_shards: int) -> list[list[str]]:
    """Divide a list of tickers into n_shards roughly equal chunks."""
    sorted_tickers = sorted(set(tickers))
    chunk_size = (len(sorted_tickers) + n_shards - 1) // n_shards
    shards = []
    for i in range(n_shards):
        chunk = sorted_tickers[i * chunk_size : (i + 1) * chunk_size]
        if chunk:
            shards.append(chunk)
    return shards


def get_shard_last_processed_date(shard_dir: Path) -> str | None:
    """Return the last processed date recorded in the shard's checkpoint, or None."""
    import json
    for name in ("checkpoint", "checkpoint.good", "checkpoint.prev"):
        state_path = shard_dir / name / "harness_state.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text())
                last_date = state.get("last_processed_date")
                if last_date:
                    return str(last_date)
            except Exception:
                continue
    return None


def get_archive_latest_date(history_dir: Path, sample_tickers: Sequence[str] = ("SPY", "AAPL", "MSFT")) -> str | None:
    """Infer the latest trading date present in the history directory."""
    candidates = []
    for ticker in sample_tickers:
        p = history_dir / f"{ticker}.csv"
        if p.is_file():
            candidates.append(p)
    if not candidates:
        candidates = list(history_dir.glob("*.csv"))[:5]

    latest_date = None
    for path in candidates:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 512), os.SEEK_SET)
                lines = f.read().decode("utf-8", errors="ignore").strip().splitlines()
                if lines:
                    last_line = lines[-1].strip()
                    parts = last_line.split(",")
                    if parts and len(parts[0]) == 10 and parts[0][4] == "-" and parts[0][7] == "-":
                        d = parts[0]
                        if latest_date is None or d > latest_date:
                            latest_date = d
        except Exception:
            continue
    return latest_date


def is_shard_completed(shard_dir: Path, target_end: str | None = None) -> bool:
    """Return True if the shard has already completed and published report.json up to target_end."""
    report_file = shard_dir / "report" / "report.json"
    if not report_file.is_file():
        return False
    if target_end is not None:
        last_date = get_shard_last_processed_date(shard_dir)
        if last_date is not None and last_date < target_end:
            return False
    return True


def has_checkpoint(shard_dir: Path) -> bool:
    """Return True if a durable checkpoint exists from a previous run."""
    return (
        (shard_dir / "checkpoint").is_dir()
        or (shard_dir / "checkpoint.prev").is_dir()
        or (shard_dir / "checkpoint.good").is_dir()
    )


def build_shard_cmd(
    shard_tickers: Sequence[str],
    shard_dir: Path,
    args: argparse.Namespace,
    target_end: str | None = None,
) -> tuple[list[str] | None, str]:
    """Assemble the CLI command for running run_walkforward.py on one shard.

    Returns (command_tokens_or_None, status_mode).
    """
    if is_shard_completed(shard_dir, target_end) and not args.force:
        return None, "completed"

    if has_checkpoint(shard_dir) and not args.force:
        cmd = [
            sys.executable,
            "-u",
            str(OCTOPUS_DIR / "run_walkforward.py"),
            "--resume",
            str(shard_dir),
        ]
        if args.end:
            cmd.extend(["--end", args.end])
        if args.device:
            cmd.extend(["--device", args.device])
        if args.dry_run:
            cmd.append("--dry-run")
        return cmd, "resume"

    if args.force and shard_dir.exists():
        if not args.dry_run:
            for sub in ("checkpoint", "checkpoint.prev", "checkpoint.tmp", "ledger", "report", "run_meta.json"):
                target = shard_dir / sub
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.is_file():
                    target.unlink()

    cmd = [
        sys.executable,
        "-u",
        str(OCTOPUS_DIR / "run_walkforward.py"),
        "--run-dir",
        str(shard_dir),
        "--start",
        args.start,
        "--window-len",
        str(args.window_len),
        "--update-every",
        str(args.update_every),
        "--refit-every",
        str(args.refit_every),
        "--checkpoint-every",
        str(args.checkpoint_every),
        "--model",
        args.model,
    ]
    if args.gbm_row_cap:
        cmd.extend(["--gbm-row-cap", str(args.gbm_row_cap)])
    if args.online_steps:
        cmd.extend(["--online-steps", str(args.online_steps)])
    if args.diagnostic_sample:
        cmd.extend(["--diagnostic-sample", str(args.diagnostic_sample)])
    if args.end:
        cmd.extend(["--end", args.end])
    if args.device:
        cmd.extend(["--device", args.device])
    if args.dry_run:
        cmd.append("--dry-run")
    cmd.append("--tickers")
    cmd.extend(shard_tickers)
    return cmd, "fresh"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Wondrous Octopus walk-forward replay across sharded ticker groups."
    )
    parser.add_argument(
        "--shards",
        type=int,
        default=4,
        help="Total number of shards to partition the archive into (default: 4)",
    )
    parser.add_argument(
        "--shard",
        type=int,
        default=None,
        help="Run only this single 1-indexed shard (e.g. 1, 2, 3, or 4)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all shards sequentially (default if neither --shard nor --parallel is passed)",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run all shards concurrently in background processes",
    )
    parser.add_argument(
        "--start",
        default="2000-01-01",
        help="First prediction date (default: 2000-01-01; full 2000-to-present walkforward)",
    )
    parser.add_argument(
        "--gbm-row-cap",
        type=int,
        default=100_000,
        help="Cap on training rows for GBM quantile fitting (default: 100000 = 5x faster fit)",
    )
    parser.add_argument(
        "--online-steps",
        type=int,
        default=2,
        help="Gradient descent steps per neural update (default: 2 = 2x faster update)",
    )
    parser.add_argument(
        "--diagnostic-sample",
        type=int,
        default=100,
        help="Tickers sampled for encoder health diagnostic (default: 100 = faster check)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Last simulation date (inclusive; default: latest in archive)",
    )
    parser.add_argument(
        "--window-len",
        type=int,
        default=64,
        help="Lookback window length (default: 64)",
    )
    parser.add_argument(
        "--update-every",
        type=int,
        default=5,
        help="Online neural update cadence in days (default: 5 = weekly)",
    )
    parser.add_argument(
        "--refit-every",
        type=int,
        default=63,
        help="GBM refit cadence in days (default: 63 = quarterly)",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=10,
        help="Save point cadence: write durable checkpoint every N steps (default: 10)",
    )
    parser.add_argument(
        "--model",
        choices=("persistence", "gbm", "octopus", "all"),
        default="all",
        help="Model set to train (default: all)",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "mps"),
        default="auto",
        help="Compute device for neural Octopus (default: auto -> mps on Mac)",
    )
    parser.add_argument(
        "--run-dir-base",
        type=Path,
        default=Path("analysis/real-octopus-run"),
        help="Base directory where shard subdirectories will be created (default: analysis/real-octopus-run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run from scratch, ignoring existing checkpoints or reports",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print shard distributions and command lines without fitting models",
    )

    args = parser.parse_args(argv)

    history_dir = configured_ticker_history_dir()
    all_tickers = discover_available_tickers(history_dir)
    if not all_tickers:
        print(f"REFUSED: no valid tickers found in {history_dir}", file=sys.stderr)
        return 1

    shards = split_into_shards(all_tickers, args.shards)
    total_tickers = sum(len(s) for s in shards)
    archive_latest = get_archive_latest_date(history_dir)
    target_end = args.end or archive_latest

    print(
        f"Archive: {history_dir} | Latest: {archive_latest or 'unknown'} | "
        f"Total valid tickers: {total_tickers} | Partitioned into {len(shards)} shards"
    )
    for i, s in enumerate(shards, start=1):
        s_dir = args.run_dir_base / f"shard_{i}"
        if is_shard_completed(s_dir, target_end):
            status = " [DONE]"
        elif (s_dir / "report" / "report.json").is_file():
            last_p = get_shard_last_processed_date(s_dir)
            status = f" [STALE: {last_p} < {target_end}]"
        elif has_checkpoint(s_dir):
            status = " [CHECKPOINT]"
        else:
            status = ""
        print(f"  Shard {i}/{len(shards)}: {len(s)} tickers ({s[0]} ... {s[-1]}){status}")

    # 1. Single shard mode
    if args.shard is not None:
        if not (1 <= args.shard <= len(shards)):
            print(f"Error: --shard must be between 1 and {len(shards)}", file=sys.stderr)
            return 1
        target_shard = shards[args.shard - 1]
        shard_dir = args.run_dir_base / f"shard_{args.shard}"
        cmd, mode = build_shard_cmd(target_shard, shard_dir, args, target_end=target_end)
        if mode == "completed":
            print(f"\nShard {args.shard} is already completed ({shard_dir}/report/report.json exists).")
            print("Use --force to re-run from scratch.")
            return 0
        if mode == "resume":
            last_p = get_shard_last_processed_date(shard_dir)
            is_stale = last_p and target_end and last_p < target_end
            stale_info = f" (stale: processed through {last_p}, target is {target_end})" if is_stale else ""
            print(
                f"\nFound existing save point! Resuming Shard {args.shard} "
                f"from {shard_dir}/checkpoint{stale_info} ..."
            )
        else:
            print(f"\nLaunching Shard {args.shard} ({len(target_shard)} tickers) -> {shard_dir} ...")
            print(f"Save points: writing checkpoints every {args.checkpoint_every} steps.")

        if args.dry_run:
            assert cmd is not None
            print("Dry run command:", " ".join(cmd[:12]), f"... [{len(target_shard)} tickers]")
            return 0
        assert cmd is not None
    if args.dry_run:
        print("\n[Dry Run Preview]")
        for i, s in enumerate(shards, start=1):
            shard_dir = args.run_dir_base / f"shard_{i}"
            cmd, mode = build_shard_cmd(s, shard_dir, args, target_end=target_end)
            if mode == "resume":
                last_p = get_shard_last_processed_date(shard_dir)
                has_report = (shard_dir / "report" / "report.json").is_file()
                stale_tag = (
                    f" (stale: processed through {last_p}, target is {target_end})" if has_report else ""
                )
                action = f"RESUME from {shard_dir}/checkpoint{stale_tag}"
            elif mode == "completed":
                action = "ALREADY COMPLETED"
            else:
                action = f"FRESH START (checkpoints every {args.checkpoint_every} steps)"
            print(f"  Shard {i}: {len(s)} tickers -> {action}")
        print("\nDry run finished. Pass without --dry-run to execute.")
        return 0

    # 2. Parallel mode
    if args.parallel:
        print(f"\nLaunching all {len(shards)} shards in parallel...")
        processes: list[tuple[int, subprocess.Popen, Path]] = []
        for i, s in enumerate(shards, start=1):
            shard_dir = args.run_dir_base / f"shard_{i}"
            cmd, mode = build_shard_cmd(s, shard_dir, args, target_end=target_end)
            if mode == "completed":
                print(f"  [Shard {i}] Already completed. Skipping.")
                continue
            assert cmd is not None
            log_file = shard_dir / "stdout.log"
            shard_dir.mkdir(parents=True, exist_ok=True)
            log_handle = log_file.open("a", encoding="utf-8")
            resume_tag = " [RESUMING FROM SAVE POINT]" if mode == "resume" else ""
            print(f"  [Shard {i}] {len(s)} tickers -> {shard_dir}{resume_tag} (logging to {log_file})")
            proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT)
            processes.append((i, proc, log_file))

        if not processes:
            print("\nAll shards are already completed!")
            return 0

        print("\nAll active shards launched. Waiting for completion...")
        exit_code = 0
        try:
            for i, proc, log_file in processes:
                code = proc.wait()
                if code == 0:
                    print(f"  [Shard {i}] Completed successfully.")
                else:
                    print(f"  [Shard {i}] Stopped with exit code {code}. Check {log_file}")
                    exit_code = code
        except KeyboardInterrupt:
            print("\nInterrupt received! Terminating parallel shard processes (checkpoints retained)...")
            for _, proc, _ in processes:
                proc.terminate()
            for _, proc, _ in processes:
                proc.wait()
            return 3
        return exit_code

    # 3. Sequential mode (default or --all)
    print(f"\nRunning shards sequentially (save points every {args.checkpoint_every} steps)...")
    for i, s in enumerate(shards, start=1):
        shard_dir = args.run_dir_base / f"shard_{i}"
        cmd, mode = build_shard_cmd(s, shard_dir, args, target_end=target_end)
        if mode == "completed":
            print(f"\nShard {i}/{len(shards)} is already completed. Skipping.")
            continue

        resume_title = "RESUMING FROM SAVE POINT" if mode == "resume" else "STARTING"
        print("\n=======================================================")
        print(f"  {resume_title}: SHARD {i}/{len(shards)} ({len(s)} tickers) -> {shard_dir}")
        print("=======================================================\n")
        assert cmd is not None
        res = subprocess.run(cmd)
        if res.returncode != 0:
            print(f"\nShard {i} stopped with exit code {res.returncode}. Last save point is retained.")
            print("Run again anytime to resume exactly where it left off!")
            return res.returncode

    print(f"\nAll {len(shards)} shards completed successfully!")
    print(f"Artifacts saved under {args.run_dir_base}/shard_1 ... shard_{len(shards)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
