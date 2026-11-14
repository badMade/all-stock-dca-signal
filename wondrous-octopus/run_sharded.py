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
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
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


#: Tentative sizing constants for ``--shards auto``. They bound the choice by resources and by a minimum shard
#: size; they are NOT yet backed by a measured accuracy comparison across shard counts (a shard trains its own
#: model on only its own tickers, so smaller shards may be less accurate). Re-tune from a measured 4-vs-8
#: comparison before trusting "auto" as the most accurate setting.
MIN_TICKERS_PER_SHARD = 750
GB_PER_SHARD = 8.0  # measured ~7.5 GB per ~1,500-ticker shard on 2026-11-11, rounded up
RAM_BUDGET_FRACTION = 0.6  # leave the rest for the OS, the GPU driver and the operator's other work
SHARD_PLAN = "shard_plan.json"


def _physical_ram_gb() -> float | None:
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
    except (ValueError, OSError, AttributeError):
        return None


def auto_shard_count(
    n_tickers: int, cpu_count: int | None = None, ram_gb: float | None = None
) -> tuple[int, str]:
    """Pick a shard count from the machine and the universe; returns (count, human-readable reason).

    The count is the smallest of three ceilings, never below 1: one shard per 2 cores (each shard is mostly a
    single busy thread plus GPU waits), what fits in RAM, and a floor on shard size so a shard keeps enough
    tickers to learn from.
    """
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    ram = ram_gb if ram_gb is not None else _physical_ram_gb()
    by_cpu = max(1, cpus // 2)
    by_size = max(1, n_tickers // MIN_TICKERS_PER_SHARD)
    ceilings = {"cores": by_cpu, "min-shard-size": by_size}
    if ram is not None:
        ceilings["ram"] = max(1, int(ram * RAM_BUDGET_FRACTION // GB_PER_SHARD))
    limiting = min(ceilings, key=lambda k: ceilings[k])
    count = ceilings[limiting]
    detail = ", ".join(f"{k}<={v}" for k, v in ceilings.items())
    return count, f"auto -> {count} shards ({detail}; limited by {limiting})"


def resolve_shard_count(requested: str | int, n_tickers: int, run_dir_base: Path) -> tuple[int, str]:
    """An explicit number is used as given. ``auto`` first reuses the partition already on disk: changing the count
    mid-run would re-deal the tickers and make every existing shard checkpoint unresumable."""
    if str(requested) != "auto":
        return int(requested), f"--shards {requested}"
    plan = run_dir_base / SHARD_PLAN
    if plan.is_file():
        try:
            return int(json.loads(plan.read_text(encoding="utf-8"))["shards"]), f"auto -> reusing {SHARD_PLAN}"
        except (OSError, ValueError, KeyError, TypeError):
            pass
    existing = [d for d in run_dir_base.glob("shard_*") if d.is_dir() and d.name.removeprefix("shard_").isdigit()]
    if existing:
        return len(existing), f"auto -> reusing the {len(existing)} shard directories already under {run_dir_base}"
    return auto_shard_count(n_tickers)


def _shards_arg(value: str) -> str | int:
    if value == "auto":
        return value
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--shards must be a positive integer or 'auto', got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("--shards must be at least 1")
    return n


_PROGRESS_RE = re.compile(r"\] (\d+)/(\d+) \(")


def parse_shard_progress(log_file: Path, tail_bytes: int = 16384) -> tuple[int, int] | None:
    """(steps_done, steps_total) from the last replay progress line in a shard log, else None.

    Reads only the file's tail so polling stays cheap on multi-GB logs; None while a shard is still
    loading data or in its first fit (no step line yet)."""
    try:
        with log_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    found = _PROGRESS_RE.findall(text)
    return (int(found[-1][0]), int(found[-1][1])) if found else None


def format_status(progress: dict[int, tuple[int, int] | None], elapsed_s: float, baseline: dict[int, int]) -> str:
    """One aggregate line: overall percent, steps/h since launch and an ETA set by the slowest shard.

    ``baseline`` holds each shard's step count at launch so a resumed run measures only this
    invocation's speed. The ETA is the slowest shard's remaining steps at its own rate, because the
    run is only finished when every shard is."""
    seen = {i: p for i, p in progress.items() if p}
    if not seen:
        return f"status: {len(progress)} shards started, no step lines yet (loading / first fit), elapsed {elapsed_s / 60:.0f}m"
    done = sum(p[0] for p in seen.values())
    total = sum(p[1] for p in seen.values())
    worst_eta = 0.0
    rate_total = 0.0
    for i, (cur, tot) in seen.items():
        rate = (cur - baseline.get(i, 0)) / elapsed_s if elapsed_s > 0 else 0.0
        rate_total += rate
        if rate > 0:
            worst_eta = max(worst_eta, (tot - cur) / rate)
    pct = 100.0 * done / total if total else 0.0
    line = f"status: {len(seen)}/{len(progress)} shards reporting | {pct:.1f}% ({done}/{total} steps)"
    if rate_total > 0 and worst_eta > 0:
        eta_at = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + worst_eta))
        line += f" | {rate_total * 3600:.0f} steps/h combined | ETA {eta_at} ({worst_eta / 3600:.1f}h left)"
    else:
        line += " | rate not measurable yet"
    return line + f" | elapsed {elapsed_s / 3600:.1f}h"


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
                # Bolt: Replace read_text with read_bytes to skip string allocation for ~15% faster JSON load
                state = json.loads(state_path.read_bytes())
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


class ShardExportError(RuntimeError):
    """One or more shards could not export their next-day forecasts, so no merged CSV may be published."""


def merge_shard_tomorrow_forecasts(run_dir_base: Path, shard_dirs: Sequence[Path]) -> Path:
    """Export and merge next-day predictions across all shards into all_shards_tomorrow.csv.

    All-or-nothing: ``all_shards_tomorrow.csv`` feeds ``publish_octopus_sizing.py``, so a partial merge (one shard
    silently missing) or a stale file from an earlier run must never be left behind. Any failed or missing shard
    export, or an export that yields no rows at all, raises :class:`ShardExportError` after removing the previous
    merged file.
    """
    merged_path = run_dir_base / "all_shards_tomorrow.csv"
    merged_path.unlink(missing_ok=True)  # never leave the previous run's forecasts looking current
    all_rows = []
    fieldnames = None
    failed: list[str] = []
    for shard_dir in shard_dirs:
        out_csv = run_dir_base / f"{shard_dir.name}_tomorrow.csv"
        out_csv.unlink(missing_ok=True)
        cmd = [
            sys.executable,
            str(OCTOPUS_DIR / "inspect_run.py"),
            "--run-dir",
            str(shard_dir),
            "--tomorrow",
            "--csv",
            str(out_csv),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not out_csv.is_file():
            failed.append(shard_dir.name)
            continue
        with open(out_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if fieldnames is None and reader.fieldnames:
                fieldnames = list(reader.fieldnames) + ["shard"]
            for row in reader:
                row["shard"] = shard_dir.name
                all_rows.append(row)
    if failed:
        raise ShardExportError(f"next-day export failed for {', '.join(failed)}; no merged forecast was written")
    if all_rows and fieldnames:
        with open(merged_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"Merged {len(all_rows)} next-day forecasts across {len(shard_dirs)} shards -> {merged_path}")
        return merged_path
    raise ShardExportError("no next-day forecasts were exported; no merged forecast was written")


def merge_or_exit_code(run_dir_base: Path, shard_dirs: Sequence[Path]) -> int:
    """Run the daily merge for a caller that must return an exit code: 0 on success, 4 when an export failed."""
    try:
        merge_shard_tomorrow_forecasts(run_dir_base, shard_dirs)
    except ShardExportError as error:
        print(f"\n{error}")
        return 4
    return 0


def build_shard_cmd(
    shard_tickers: Sequence[str],
    shard_dir: Path,
    args: argparse.Namespace,
    target_end: str | None = None,
) -> tuple[list[str] | None, str]:
    """Assemble the CLI command for running walk-forward replay or daily update on one shard.

    Returns (command_tokens_or_None, status_mode).
    """
    if getattr(args, "daily", False):
        if not has_checkpoint(shard_dir):
            return None, "no_checkpoint"
        cmd = [
            sys.executable,
            "-u",
            str(OCTOPUS_DIR / "daily_update.py"),
            "--run-dir",
            str(shard_dir),
        ]
        if getattr(args, "now", None):
            cmd.extend(["--now", str(args.now)])
        if getattr(args, "dry_run", False):
            cmd.append("--dry-run")
        return cmd, "daily"

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
        if getattr(args, "retrain", False):
            cmd.append("--retrain")
        if args.end:
            cmd.extend(["--end", args.end])
        if getattr(args, "now", None):
            cmd.extend(["--now", str(args.now)])  # as-of replay: rows on/after this day stay unseen
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
        "--checkpoint-every",
        str(args.checkpoint_every),
        "--model",
        args.model,
    ]
    preset = getattr(args, "preset", None)
    if preset:
        # A run_walkforward preset owns window/update/refit/diagnostic cadence, so those flags are NOT sent
        # (an explicit flag would silently override the preset's value).
        cmd.extend(["--preset", preset])
    else:
        cmd.extend(
            [
                "--window-len",
                str(args.window_len),
                "--update-every",
                str(args.update_every),
                "--refit-every",
                str(args.refit_every),
            ]
        )
        if args.diagnostic_sample:
            cmd.extend(["--diagnostic-sample", str(args.diagnostic_sample)])
    if getattr(args, "max_horizon", 1) > 1:
        cmd.extend(["--max-horizon", str(args.max_horizon)])
    if getattr(args, "eval_horizons", None):
        cmd.extend(["--eval-horizons", args.eval_horizons])
    if getattr(args, "history_dir", None):
        # Frozen snapshot: a resume refuses when archive rows change, and the live archive refreshes daily.
        cmd.extend(["--history-dir", str(args.history_dir)])
    if args.gbm_row_cap:
        cmd.extend(["--gbm-row-cap", str(args.gbm_row_cap)])
    if args.online_steps:
        cmd.extend(["--online-steps", str(args.online_steps)])
    if args.end:
        cmd.extend(["--end", args.end])
    if getattr(args, "now", None):
        cmd.extend(["--now", str(args.now)])  # as-of replay: rows on/after this day stay unseen
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
        type=_shards_arg,
        default="auto",
        help="Number of shards, or 'auto' (default): sized from cores, RAM and a minimum shard size, and an "
        "existing run directory's partition is always reused",
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
        "--status-every",
        type=float,
        default=10.0,
        help="Parallel mode: print one aggregate progress/ETA line to the launcher log every N minutes (0 disables)",
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
        "--preset",
        choices=("quick", "overnight", "full"),
        default=None,
        help="run_walkforward preset; owns window/update/refit/diagnostic cadence, so --window-len, "
        "--update-every, --refit-every and --diagnostic-sample are then NOT passed (default: none)",
    )
    parser.add_argument(
        "--max-horizon",
        type=int,
        default=1,
        help="Forecast every trading day t+1..t+N (default 1 = next-day only); frozen per shard",
    )
    parser.add_argument(
        "--eval-horizons",
        default=None,
        metavar="H,H,...",
        help="Horizons matured, scored and calibrated per shard (each in 2..max-horizon; default 5,10,15,20); "
        "frozen per shard, e.g. 2,3,...,90 to calibrate every horizon",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Ticker CSV archive (default: configured). Use a frozen snapshot for long replays",
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
        "--retrain",
        action="store_true",
        help="Pass --retrain to run_walkforward.py for a sanctioned recovery refit if archive changed",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Run daily_update.py on each shard to advance by one trading day",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="As-of cutoff date (passed to daily_update.py or run_walkforward.py)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print shard distributions and command lines without fitting models",
    )

    args = parser.parse_args(argv)

    if args.daily:
        # Every numbered shard directory counts, checkpoint or not: filtering on the checkpoint first would let a
        # lost shard_2/checkpoint drop out of the run silently, leaving its forecasts stale while the run exits 0.
        shard_dirs = sorted(
            [d for d in args.run_dir_base.glob("shard_*") if d.is_dir() and d.name.removeprefix("shard_").isdigit()],
            key=lambda p: int(p.name.removeprefix("shard_")),
        )
        if not shard_dirs:
            print(f"REFUSED: no shard checkpoints found in {args.run_dir_base}", file=sys.stderr)
            return 1
        no_checkpoint = [d.name for d in shard_dirs if not has_checkpoint(d)]
        present = {int(d.name.removeprefix("shard_")) for d in shard_dirs}
        missing = [f"shard_{n}" for n in range(1, max(present) + 1) if n not in present]
        if no_checkpoint or missing:
            problems = [f"{name} has no durable checkpoint" for name in no_checkpoint]
            problems += [f"{name} is missing" for name in missing]
            print(f"REFUSED: daily update would skip shards ({'; '.join(problems)})", file=sys.stderr)
            return 1
        shards = [[] for _ in shard_dirs]
        target_end = None

        print(f"Daily update across {len(shard_dirs)} shards under {args.run_dir_base}")
        for i, s_dir in enumerate(shard_dirs, start=1):
            last_p = get_shard_last_processed_date(s_dir)
            print(f"  Shard {i}/{len(shard_dirs)} ({s_dir.name}): last processed {last_p or 'unknown'}")
    else:
        history_dir = args.history_dir or configured_ticker_history_dir()
        all_tickers = discover_available_tickers(history_dir)
        if not all_tickers:
            print(f"REFUSED: no valid tickers found in {history_dir}", file=sys.stderr)
            return 1

        n_shards, shard_reason = resolve_shard_count(args.shards, len(all_tickers), args.run_dir_base)
        print(f"Shard count: {shard_reason}")
        shards = split_into_shards(all_tickers, n_shards)
        if not args.dry_run:
            args.run_dir_base.mkdir(parents=True, exist_ok=True)
            plan = {"shards": len(shards), "tickers": len(all_tickers), "reason": shard_reason}
            (args.run_dir_base / SHARD_PLAN).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        shard_dirs = [args.run_dir_base / f"shard_{i}" for i in range(1, len(shards) + 1)]
        total_tickers = sum(len(s) for s in shards)
        archive_latest = get_archive_latest_date(history_dir)
        target_end = args.end or archive_latest

        print(
            f"Archive: {history_dir} | Latest: {archive_latest or 'unknown'} | "
            f"Total valid tickers: {total_tickers} | Partitioned into {len(shards)} shards"
        )
        for i, s in enumerate(shards, start=1):
            s_dir = shard_dirs[i - 1]
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
        if not (1 <= args.shard <= len(shard_dirs)):
            print(f"Error: --shard must be between 1 and {len(shard_dirs)}", file=sys.stderr)
            return 1
        target_shard = shards[args.shard - 1]
        shard_dir = shard_dirs[args.shard - 1]
        cmd, mode = build_shard_cmd(target_shard, shard_dir, args, target_end=target_end)
        if mode == "completed":
            print(f"\nShard {args.shard} is already completed ({shard_dir}/report/report.json exists).")
            print("Use --force to re-run from scratch.")
            return 0
        if mode == "daily":
            last_p = get_shard_last_processed_date(shard_dir)
            print(f"\nRunning daily update on Shard {args.shard} ({shard_dir.name}, last: {last_p}) ...")
        elif mode == "resume":
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
        res = subprocess.run(cmd)
        if res.returncode == 0 and args.daily:
            return merge_or_exit_code(args.run_dir_base, shard_dirs)
        return res.returncode

    if args.dry_run:
        print("\n[Dry Run Preview]")
        for i, s_dir in enumerate(shard_dirs, start=1):
            cmd, mode = build_shard_cmd(shards[i - 1], s_dir, args, target_end=target_end)
            if mode == "daily":
                action = f"DAILY UPDATE on {s_dir.name}"
            elif mode == "resume":
                last_p = get_shard_last_processed_date(s_dir)
                has_report = (s_dir / "report" / "report.json").is_file()
                stale_tag = (
                    f" (stale: processed through {last_p}, target is {target_end})" if has_report else ""
                )
                action = f"RESUME from {s_dir}/checkpoint{stale_tag}"
            elif mode == "completed":
                action = "ALREADY COMPLETED"
            else:
                action = f"FRESH START (checkpoints every {args.checkpoint_every} steps)"
            print(f"  Shard {i}: {action}")
        print("\nDry run finished. Pass without --dry-run to execute.")
        return 0

    # 2. Parallel mode
    if args.parallel:
        title = "daily update on" if args.daily else "all"
        print(f"\nLaunching {title} {len(shard_dirs)} shards in parallel...")
        processes: list[tuple[int, subprocess.Popen, Path]] = []
        for i, s_dir in enumerate(shard_dirs, start=1):
            cmd, mode = build_shard_cmd(shards[i - 1], s_dir, args, target_end=target_end)
            if mode == "completed":
                print(f"  [Shard {i}] Already completed. Skipping.")
                continue
            assert cmd is not None
            log_file = s_dir / ("daily_update.log" if args.daily else "stdout.log")
            s_dir.mkdir(parents=True, exist_ok=True)
            log_handle = log_file.open("a", encoding="utf-8")
            if mode == "daily":
                tag = " [DAILY UPDATE]"
            elif mode == "resume":
                tag = " [RESUMING FROM SAVE POINT]"
            else:
                tag = ""
            print(f"  [Shard {i}] {s_dir.name} -> {s_dir}{tag} (logging to {log_file})")
            proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT)
            processes.append((i, proc, log_file))

        if not processes:
            print("\nAll shards are already completed!")
            return 0

        print("\nAll active shards launched. Waiting for completion...")
        exit_code = 0
        try:
            pending = list(processes)
            started = time.monotonic()
            baseline = {i: (parse_shard_progress(lf) or (0, 0))[0] for i, _, lf in processes}
            next_status = started + args.status_every * 60
            while pending:
                for item in list(pending):
                    i, proc, log_file = item
                    code = proc.poll()
                    if code is None:
                        continue
                    pending.remove(item)
                    if code == 0:
                        print(f"  [Shard {i}] Completed successfully.")
                    else:
                        print(f"  [Shard {i}] Stopped with exit code {code}. Check {log_file}")
                        exit_code = code
                if pending and args.status_every > 0 and time.monotonic() >= next_status:
                    next_status = time.monotonic() + args.status_every * 60
                    snap = {i: parse_shard_progress(lf) for i, _, lf in processes}
                    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] " + format_status(snap, time.monotonic() - started, baseline), flush=True)
                if pending:
                    time.sleep(2)
        except KeyboardInterrupt:
            print("\nInterrupt received! Terminating parallel shard processes (checkpoints retained)...")
            for _, proc, _ in processes:
                proc.terminate()
            for _, proc, _ in processes:
                proc.wait()
            return 3
        if exit_code == 0 and args.daily and not args.dry_run:
            exit_code = merge_or_exit_code(args.run_dir_base, shard_dirs)
        return exit_code

    # 3. Sequential mode (default or --all)
    mode_desc = "daily update on" if args.daily else "walk-forward on"
    print(f"\nRunning {mode_desc} shards sequentially...")
    for i, s_dir in enumerate(shard_dirs, start=1):
        cmd, mode = build_shard_cmd(shards[i - 1], s_dir, args, target_end=target_end)
        if mode == "completed":
            print(f"\nShard {i}/{len(shard_dirs)} is already completed. Skipping.")
            continue

        if mode == "daily":
            title = "DAILY UPDATE"
        elif mode == "resume":
            title = "RESUMING FROM SAVE POINT"
        else:
            title = "STARTING"
        print("\n=======================================================")
        print(f"  {title}: SHARD {i}/{len(shard_dirs)} -> {s_dir}")
        print("=======================================================\n")
        assert cmd is not None
        res = subprocess.run(cmd)
        if res.returncode != 0:
            print(f"\nShard {i} stopped with exit code {res.returncode}. Last save point is retained.")
            print("Run again anytime to resume exactly where it left off!")
            return res.returncode

    if args.daily and not args.dry_run:
        merge_code = merge_or_exit_code(args.run_dir_base, shard_dirs)
        if merge_code != 0:
            return merge_code

    print(f"\nAll {len(shard_dirs)} shards completed successfully!")
    print(f"Artifacts saved under {args.run_dir_base}/shard_1 ... shard_{len(shard_dirs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
