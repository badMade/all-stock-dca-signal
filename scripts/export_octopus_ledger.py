#!/usr/bin/env python3
"""Export Octopus ledger predictions and scores for the scheduled watchlist.

This script extracts historical predictions and scores from a Wondrous Octopus
run ledger, filters to the requested tickers (e.g. the 61 active scheduled stocks),
and exports clean tabular CSVs and summary reports.

Usage:
    python3 scripts/export_octopus_ledger.py --run-dir <run_dir> --all-scheduled --csv-out <out.csv>
    python3 scripts/export_octopus_ledger.py --run-dir <run_dir> --tickers AAPL MSFT --csv-out <out.csv>
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Insert wondrous-octopus path to access ledger and formatting
OCTOPUS_DIR = REPO_ROOT / "wondrous-octopus"
if str(OCTOPUS_DIR) not in sys.path:
    sys.path.insert(1, str(OCTOPUS_DIR))

from scripts.sequestered_truth_comparator import (  # noqa: E402
    SequesteredComparator,
    render_markdown_report,
)


def load_scheduled_tickers() -> list[str]:
    """Load the canonical active scheduler tickers from config/tickers.json."""
    cfg_path = REPO_ROOT / "config" / "tickers.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    active = data.get("active") or []
    return sorted(list({t.upper() for t in active}))


def _inspect_run_helpers() -> tuple[Any, Any]:
    # inspect_run.py bootstraps the full Wondrous Octopus environment (including
    # torch) on import. Keep the core-testable config/CLI paths import-safe and
    # defer that setup until we actually need ledger access.
    from inspect_run import _dollar, _open_ledger  # noqa: E402

    return _dollar, _open_ledger


def export_ledger_records(
    run_dir: Path,
    target_tickers: set[str],
    model: str = "octopus",
    csv_out: Path | None = None,
    json_out: Path | None = None,
    report_md: Path | None = None,
) -> list[dict[str, Any]]:
    """Stream records from the ledger, filter to target tickers, and export."""
    _dollar, _open_ledger = _inspect_run_helpers()
    ledger = _open_ledger(run_dir)
    available_models = ledger.models()
    if model not in available_models:
        raise ValueError(f"Model {model!r} not found in run ledger (available: {available_models})")

    exported_rows: list[dict[str, Any]] = []

    # Stream through scored records
    for row in ledger.iter_records(model=model, kind="score"):
        ticker = row["ticker"].upper()
        if target_tickers and ticker not in target_tickers:
            continue

        ref_close = row["ref_close"]
        target_date = row["target_date"]
        origin_date = row.get("origin_date")

        pred_close = _dollar(row["point"]["close"], ref_close)
        act_close = _dollar(row["actual"]["close"], ref_close)

        pred_open = _dollar(row["point"]["open"], ref_close)
        act_open = _dollar(row["actual"]["open"], ref_close)

        pred_high = _dollar(row["point"]["high"], ref_close)
        act_high = _dollar(row["actual"]["high"], ref_close)

        pred_low = _dollar(row["point"]["low"], ref_close)
        act_low = _dollar(row["actual"]["low"], ref_close)

        p10 = _dollar(row["quantiles"]["close"]["p10"], ref_close)
        p90 = _dollar(row["quantiles"]["close"]["p90"], ref_close)

        abs_err = abs(pred_close - act_close)
        err_pct = (abs_err / act_close * 100.0) if act_close > 0 else 0.0

        in_band = bool(row["score"]["in_band"]["close"])
        in_range = bool(row["score"].get("close_in_range", False))

        record = {
            "date": target_date,
            "origin_date": origin_date or "",
            "ticker": ticker,
            "pred_close": round(pred_close, 4),
            "actual_close": round(act_close, 4),
            "abs_error": round(abs_err, 4),
            "error_pct": round(err_pct, 4),
            "pred_close_p10": round(p10, 4),
            "pred_close_p90": round(p90, 4),
            "in_band": in_band,
            "in_range": in_range,
            "pred_open": round(pred_open, 4),
            "actual_open": round(act_open, 4),
            "pred_high": round(pred_high, 4),
            "actual_high": round(act_high, 4),
            "pred_low": round(pred_low, 4),
            "actual_low": round(act_low, 4),
            "ref_close": round(ref_close, 4),
        }
        exported_rows.append(record)

    # Sort chronologically by date then ticker
    exported_rows.sort(key=lambda r: (r["date"], r["ticker"]))

    # Write CSV if requested
    if csv_out:
        csv_out.parent.mkdir(parents=True, exist_ok=True)
        if exported_rows:
            fieldnames = list(exported_rows[0].keys())
            with csv_out.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(exported_rows)
            print(f"Exported {len(exported_rows)} records to CSV: {csv_out}")

    # Evaluate via SequesteredComparator if report requested
    if (report_md or json_out) and exported_rows:
        comparator = SequesteredComparator(ground_truth_dir=REPO_ROOT / "ticker_data")
        summary, records = comparator.evaluate_all(exported_rows)

        if report_md:
            report_md.parent.mkdir(parents=True, exist_ok=True)
            report_md.write_text(render_markdown_report(summary, records), encoding="utf-8")
            print(f"Markdown evaluation report written to: {report_md}")

        if json_out:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
            print(f"JSON metrics written to: {json_out}")

    return exported_rows


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for exporting ledger records."""
    parser = argparse.ArgumentParser(
        prog="export_octopus_ledger.py",
        description="Export Octopus run ledger predictions for selected stocks.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Path to the Wondrous Octopus run directory containing checkpoint and ledger.",
    )
    parser.add_argument(
        "--all-scheduled",
        action="store_true",
        help="Export all active tickers in the DCA scheduler (config/tickers.json).",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Specific tickers to export.",
    )
    parser.add_argument(
        "--model",
        default="octopus",
        help="Model name in ledger (default: octopus).",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Destination path for exported CSV.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Destination path for structured summary JSON metrics.",
    )
    parser.add_argument(
        "--report-md",
        type=Path,
        default=None,
        help="Destination path for evaluation Markdown report.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for exporting ledger."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.run_dir.is_dir():
        print(f"REFUSED: Run directory not found: {args.run_dir}", file=sys.stderr)
        return 1

    if args.all_scheduled or (args.tickers is None):
        target_tickers = set(load_scheduled_tickers())
        print(f"Filtering to {len(target_tickers)} scheduled watchlist tickers.")
    else:
        target_tickers = {t.upper() for t in args.tickers}
        print(f"Filtering to {len(target_tickers)} requested tickers.")

    try:
        rows = export_ledger_records(
            run_dir=args.run_dir,
            target_tickers=target_tickers,
            model=args.model,
            csv_out=args.csv_out,
            json_out=args.json_out,
            report_md=args.report_md,
        )
        print(f"Successfully processed {len(rows)} matching ledger records.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
