"""Audit that every placed order has a complete per-order check record.

Why this exists (2026-10-28 and 2026-10-29, 09:35 ET): the live run skipped per-order checks
the prompt makes mandatory (the weekly-history refetch, a per-ticker tradability check, a
full-history rescan) and said so only in its closing summary, giving "budget headroom is
wide" as the reason. A prompt rule alone did not stop it twice, so the rule now has a
deterministic check. For every order the run placed it must have appended one line to
``$ROOT/per_order_checks.jsonl``; this script fails the slot when a line is missing or
incomplete, and the Gate 9 alert must carry its failures.

The record is SELF-REPORTED. It cannot prove a check ran; it turns a silent omission into a
named failure. Contract:

* one record per placed order, matched on ``order_id``; no duplicates; none for an order that
  was not placed;
* every key present and non-empty, timestamps offset-aware ISO-8601;
* ``history_mode`` is ``full`` (the first order, after the baseline fetch) or ``incremental``
  (every later order), and an incremental record carries ``merged_history_sha256`` (the
  ``payload_sha256`` that ``scripts.merge_order_pages`` printed for that order);
* batched tradability is allowed, but ``tradability_checked_at`` may not be more than 300
  seconds older than the order's own ``clock_checked_at``.

Usage::

    python3 -m scripts.check_per_order_record --run-dir "$ROOT"

Reads ``finalize_orders.json`` and ``per_order_checks.jsonl`` in the run directory. Exit 0
prints ``{"ok": true, "failures": []}``; exit 1 prints the failures. It never contacts a
broker and never writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

MAX_TRADABILITY_AGE_SECONDS = 300
HISTORY_MODES = ("full", "incremental")
REQUIRED_KEYS = (
    "ticker",
    "order_id",
    "history_mode",
    "clock_checked_at",
    "tradability_checked_at",
    "spread_checked_at",
    "price_checked_at",
)
TIMESTAMP_KEYS = ("clock_checked_at", "tradability_checked_at", "spread_checked_at", "price_checked_at")


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _aware(value: Any) -> Optional[datetime]:
    """An offset-aware instant, or ``None`` when the value is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _record_failures(label: str, record: dict) -> list[str]:
    failures = []
    for key in REQUIRED_KEYS:
        if _blank(record.get(key)):
            failures.append(f"{label}: {key} is missing or empty")
    mode = record.get("history_mode")
    if not _blank(mode) and mode not in HISTORY_MODES:
        failures.append(f"{label}: history_mode {mode!r} is not one of {', '.join(HISTORY_MODES)}")
    if mode == "incremental" and _blank(record.get("merged_history_sha256")):
        failures.append(f"{label}: an incremental record needs merged_history_sha256")
    parsed = {}
    for key in TIMESTAMP_KEYS:
        if _blank(record.get(key)):
            continue
        parsed[key] = _aware(record[key])
        if parsed[key] is None:
            failures.append(f"{label}: {key} is not an offset-aware ISO-8601 timestamp")
    clock, tradability = parsed.get("clock_checked_at"), parsed.get("tradability_checked_at")
    if clock and tradability and (clock - tradability).total_seconds() > MAX_TRADABILITY_AGE_SECONDS:
        failures.append(
            f"{label}: tradability evidence is older than {MAX_TRADABILITY_AGE_SECONDS} seconds "
            "at this order's clock check"
        )
    return failures


def evaluate(orders: Sequence[dict], records: Sequence[dict]) -> list[str]:
    """Return sorted failures for the placed ``orders`` against the run's per-order ``records``."""
    failures: list[str] = []
    placed = {str(order.get("order_id")): order for order in orders}
    seen: dict[str, dict] = {}
    for record in records:
        order_id = str(record.get("order_id"))
        label = f"{record.get('ticker', '?')} ({order_id})"
        if order_id in seen:
            failures.append(f"{label}: duplicate per-order record")
            continue
        seen[order_id] = record
        if order_id not in placed:
            failures.append(f"{label}: record for an order that was not placed")
            continue
        failures.extend(_record_failures(label, record))
    for order_id, order in placed.items():
        if order_id not in seen:
            failures.append(f"{order.get('ticker', '?')} ({order_id}): no per-order record")
    return sorted(set(failures))


def _load_records(path: Path, failures: list[str]) -> list[dict]:
    if not path.exists():
        return []
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        failures.append("per_order_checks.jsonl is unreadable")
        return []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            failures.append(f"per_order_checks.jsonl line {number} is not valid JSON")
            continue
        if not isinstance(record, dict):
            failures.append(f"per_order_checks.jsonl line {number} is not an object")
            continue
        records.append(record)
    return records


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True, help="The run's $ROOT directory.")
    args = parser.parse_args(argv)

    failures: list[str] = []
    orders: list[dict] = []
    orders_path = args.run_dir / "finalize_orders.json"
    try:
        loaded = json.loads(orders_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list) or not all(isinstance(order, dict) for order in loaded):
            raise ValueError("not a list of objects")
        orders = loaded
    except (OSError, UnicodeDecodeError, ValueError):
        failures.append("finalize_orders.json is missing or malformed")

    records = _load_records(args.run_dir / "per_order_checks.jsonl", failures)
    if not failures or orders:
        failures.extend(evaluate(orders, records))
    failures = sorted(set(failures))
    print(json.dumps({"ok": not failures, "failures": failures}, sort_keys=True))
    return int(bool(failures))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
