"""Ticker universe snapshot and data freshness inspector for the console (GUI expansion).

Inspects config/tickers.json, evaluates active, whole-share, and excluded symbols,
and checks data freshness in the local ticker archive without mutating state.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dca_console_reader import root_label
from dca_ticker_paths import configured_ticker_history_dir

TICKER_PATTERN = re.compile(r"^[A-Z0-9.\-]{1,12}$")
MAX_ROWS_SCAN = 100


def _modified_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def _inspect_csv(path: Path, today: date) -> dict[str, Any]:
    """Inspect a ticker CSV's last date, total line count, and freshness."""
    if not path.is_file():
        return {
            "exists": False,
            "last_date": None,
            "total_bars": 0,
            "size_bytes": 0,
            "freshness": "missing",
            "modified": None,
        }

    try:
        stat = path.stat()
        size_bytes = stat.st_size
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()

        last_line = ""
        line_count = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # For size efficiency, if file is large, read last block for date
            if size_bytes > 4096:
                # Approximate line count from file size and sample line
                f.seek(max(0, size_bytes - 2048))
                tail = f.read()
                lines = [line.strip() for line in tail.splitlines() if line.strip()]
                if lines:
                    last_line = lines[-1]
                # Rough bar estimation based on average line length ~65 bytes
                line_count = max(1, size_bytes // 65)
            else:
                lines = [line.strip() for line in f if line.strip()]
                line_count = max(0, len(lines) - 1)
                if len(lines) > 1:
                    last_line = lines[-1]

        last_date = None
        if last_line:
            parts = last_line.split(",")
            if parts and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]):
                last_date = parts[0]

        # Determine freshness: within 4 calendar days of today is considered up to date
        freshness = "unknown"
        if last_date:
            try:
                parsed_date = date.fromisoformat(last_date)
                delta_days = (today - parsed_date).days
                if delta_days <= 4:
                    freshness = "up_to_date"
                else:
                    freshness = "stale"
            except ValueError:
                freshness = "unknown"
        else:
            freshness = "missing_data"

        return {
            "exists": True,
            "last_date": last_date,
            "total_bars": line_count,
            "size_bytes": size_bytes,
            "freshness": freshness,
            "modified": modified,
        }
    except Exception:
        return {
            "exists": True,
            "last_date": None,
            "total_bars": 0,
            "size_bytes": 0,
            "freshness": "error",
            "modified": None,
        }


def build_tickers_snapshot(repo: Path, config_dir: Path, today: date | None = None) -> dict[str, Any]:
    """Compile a comprehensive snapshot of ticker configuration and data freshness."""
    if today is None:
        today = date.today()

    config_path = config_dir / "tickers.json"
    warnings: list[str] = []

    universe: dict[str, Any] = {
        "version": "",
        "description": "",
        "active": [],
        "excluded": [],
        "whole_share": [],
        "symbol_corrections": {},
        "exclusion_reasons": {},
        "whole_share_reasons": {},
        "special_handling": {},
        "notes": "",
    }

    if config_path.is_file():
        try:
            data = json.loads(config_path.read_bytes())
            if isinstance(data, dict):
                universe["version"] = str(data.get("version") or "")
                universe["description"] = str(data.get("description") or "")
                universe["active"] = [s for s in data.get("active", []) if isinstance(s, str)]
                universe["excluded"] = [s for s in data.get("excluded", []) if isinstance(s, str)]
                universe["whole_share"] = [s for s in data.get("whole_share", []) if isinstance(s, str)]
                universe["symbol_corrections"] = {
                    str(k): str(v) for k, v in data.get("symbol_corrections", {}).items()
                }
                universe["exclusion_reasons"] = {
                    str(k): str(v) for k, v in data.get("exclusion_reasons", {}).items()
                }
                universe["whole_share_reasons"] = {
                    str(k): str(v) for k, v in data.get("whole_share_reasons", {}).items()
                }
                universe["special_handling"] = data.get("special_handling", {})
                universe["notes"] = str(data.get("notes") or "")
        except Exception as err:
            warnings.append(f"Could not load tickers.json: {err}")
    else:
        warnings.append("config/tickers.json was not found.")

    # Locate ticker history directory
    try:
        history_dir = configured_ticker_history_dir()
        archive_available = history_dir.is_dir()
        archive_label = root_label(history_dir)
    except Exception as err:
        history_dir = repo / "ticker_data"
        archive_available = False
        archive_label = "ticker_data (unavailable)"
        warnings.append(f"Ticker history location check failed: {err}")

    # Inspect each symbol
    all_symbols = []
    seen = set()
    for s in universe["active"]:
        if s not in seen:
            all_symbols.append((s, "active"))
            seen.add(s)
    for s in universe["whole_share"]:
        if s not in seen:
            all_symbols.append((s, "whole_share"))
            seen.add(s)
    for s in universe["excluded"]:
        if s not in seen:
            all_symbols.append((s, "excluded"))
            seen.add(s)

    items: list[dict[str, Any]] = []
    up_to_date_count = 0
    stale_count = 0
    missing_count = 0

    for symbol, category in all_symbols:
        csv_path = history_dir / f"{symbol}.csv" if archive_available else Path("/nonexistent")
        info = _inspect_csv(csv_path, today) if archive_available else {
            "exists": False, "last_date": None, "total_bars": 0, "size_bytes": 0, "freshness": "missing", "modified": None
        }

        freshness = info["freshness"]
        if category in {"active", "whole_share"}:
            if freshness == "up_to_date":
                up_to_date_count += 1
            elif freshness == "stale":
                stale_count += 1
            elif freshness == "missing":
                missing_count += 1

        note = ""
        if category == "excluded":
            note = universe["exclusion_reasons"].get(symbol, "Excluded from live buying.")
        elif category == "whole_share":
            note = universe["whole_share_reasons"].get(symbol, "Whole-share accumulation.")
        elif symbol in universe["special_handling"]:
            note = "Special broker routing applied."

        items.append({
            "symbol": symbol,
            "category": category,
            "exists": info["exists"],
            "last_date": info["last_date"],
            "total_bars": info["total_bars"],
            "size_bytes": info["size_bytes"],
            "freshness": freshness,
            "modified": info["modified"],
            "note": note,
        })

    # Read latest daily update log if present
    log_path = history_dir / "_daily_update_log.txt" if archive_available else None
    recent_logs = []
    if log_path and log_path.is_file():
        try:
            lines = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            recent_logs = lines[-5:]
        except Exception:
            pass

    return {
        "universe": universe,
        "archive": {
            "available": archive_available,
            "label": archive_label,
            "recent_logs": recent_logs,
        },
        "summary": {
            "total_active": len(universe["active"]),
            "total_whole_share": len(universe["whole_share"]),
            "total_excluded": len(universe["excluded"]),
            "up_to_date_count": up_to_date_count,
            "stale_count": stale_count,
            "missing_count": missing_count,
        },
        "items": items,
        "warnings": warnings,
    }
