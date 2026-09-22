"""Merge sharded walk-forward predictions into a unified dataset.

This module consolidates the ledgered walk-forward prediction scores from
analysis/real-octopus-run/shard_{1..4} into a single unified archive for
Phase 4 backtest integration.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path


def merge_shard_predictions(
    run_dir: Path,
    output_file: Path,
    year: str = "2026",
    verbose: bool = True,
) -> int:
    """Merge score predictions from all shards in run_dir for a given year.

    Args:
        run_dir: Root directory containing shard subdirectories.
        output_file: Destination path for merged compressed jsonl file.
        year: Year string to merge (e.g., "2026").
        verbose: Whether to log progress to stdout.

    Returns:
        Total number of prediction records merged.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)
    shard_dirs = sorted([d for d in run_dir.glob("shard_*") if d.is_dir()])
    if not shard_dirs:
        raise FileNotFoundError(f"No shard directories found under {run_dir}")

    total_records = 0
    with gzip.open(output_file, "wt", encoding="utf-8") as out_f:
        for shard_dir in shard_dirs:
            score_file = shard_dir / "ledger" / "octopus" / f"score_{year}.jsonl.gz"
            if not score_file.exists():
                if verbose:
                    print(f"[{shard_dir.name}] Notice: {score_file.name} not found, skipping", file=sys.stderr)
                continue

            shard_count = 0
            with gzip.open(score_file, "rt", encoding="utf-8") as in_f:
                for line in in_f:
                    if line.strip():
                        out_f.write(line)
                        shard_count += 1
            total_records += shard_count
            if verbose:
                print(f"[{shard_dir.name}] Merged {shard_count:,} records from {score_file.name}")

    if verbose:
        print(f"Successfully merged {total_records:,} total records into {output_file}")
    return total_records


def main() -> int:
    """Command-line entry point for merging shard predictions."""
    parser = argparse.ArgumentParser(description="Merge sharded walk-forward predictions.")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path("analysis/real-octopus-run"),
        help="Path to real-octopus-run directory containing shard_* folders.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("analysis/real-octopus-run/merged_predictions.jsonl.gz"),
        help="Destination path for merged predictions.",
    )
    parser.add_argument(
        "--year",
        type=str,
        default="2026",
        help="Year to merge (default: 2026).",
    )
    args = parser.parse_args()

    merge_shard_predictions(args.run_dir, args.output, year=args.year)
    return 0


if __name__ == "__main__":
    sys.exit(main())
