#!/usr/bin/env python3
"""Build a derived, rebuildable SQLite index of the run-state files under the artifact tree.

``PRODUCTION_READINESS.md`` gap 2 notes that "the file tree is the database": prior-allocation and cost scans
``rglob`` the whole history on every run, and the per-run artifact corpus index is still open. The long-term answer
is a real store; moving *canonical* run and budget state into SQLite changes the live path and needs its own design
and acceptance run, so this script does the safe first step instead:

* The JSON files stay the **only source of truth**. The database is a *derived cache*: delete it any time and
  rebuild it. Nothing in the live path reads it, and nothing here writes under the artifact tree.
* **Read-only on the source.** The tree is walked and each ``run_state*.json`` is read; the only file written is the
  SQLite database, which must live outside the artifact tree.
* **Incremental and idempotent.** A file whose size and mtime are unchanged is not re-read. Re-running yields the
  same rows; removed files are dropped from the index.
* **Honest about bad data.** A file that is unreadable or not a JSON object is *recorded* with its error rather than
  skipped, so an index consumer can tell "no history" from "history I could not parse" (the same evidence-vs-zero
  distinction ``dca_risk`` documents).
* Standard library only (``sqlite3``), no network.

It answers questions such as "how many scheduled runs per stream in the last 30 days" or "which run states did not
parse" in milliseconds instead of rescanning the tree.

Usage::

    python scripts/index_run_states_sqlite.py --db ~/dca-index.sqlite3                 # index $DCA_CRON_TRACKING_ROOT
    python scripts/index_run_states_sqlite.py --source /path/to/cron_tracking --db ~/dca-index.sqlite3
    python scripts/index_run_states_sqlite.py --db ~/dca-index.sqlite3 --summary       # print counts, no indexing
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_states (
    path TEXT PRIMARY KEY,          -- relative to the source root, posix separators
    stream_dir TEXT NOT NULL,       -- first path component (e.g. morning_dca)
    folder TEXT NOT NULL,           -- the run folder name (e.g. 20260713_1030ET)
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    run_id TEXT,
    run_origin TEXT,
    manual_run INTEGER,             -- 1 / 0 / NULL when absent
    state_schema_version TEXT,
    parse_error TEXT                -- NULL when the file parsed to a JSON object
);
CREATE INDEX IF NOT EXISTS idx_run_states_stream ON run_states(stream_dir);
CREATE INDEX IF NOT EXISTS idx_run_states_origin ON run_states(run_origin, manual_run);
PRAGMA user_version = 1;
"""


def iter_run_state_files(source: Path) -> Iterator[Path]:
    """``run_state*.json`` regular files; symlinks are never followed (they could point outside the tree)."""
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if name.startswith("run_state") and name.endswith(".json"):
                path = Path(dirpath) / name
                if path.is_file() and not path.is_symlink():
                    yield path


def _scalar(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    return str(value) if isinstance(value, (str, int, float)) else None


def _parse(path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"[:200]
    if not isinstance(data, dict):
        return {}, f"not a JSON object (got {type(data).__name__})"
    return data, None


def _row(source: Path, path: Path) -> Dict[str, Any]:
    rel = path.relative_to(source).as_posix()
    parts = rel.split("/")
    stat = path.stat()
    data, error = _parse(path)
    manual = data.get("manual_run")
    return {
        "path": rel,
        "stream_dir": parts[0] if len(parts) > 1 else "",
        "folder": parts[-2] if len(parts) > 1 else "",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "run_id": _scalar(data.get("run_id")),
        "run_origin": _scalar(data.get("run_origin")),
        "manual_run": int(manual) if isinstance(manual, bool) else None,
        "state_schema_version": _scalar(data.get("state_schema_version")),
        "parse_error": error,
    }


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def build_index(source: Path, db_path: Path) -> Dict[str, int]:
    """Index ``source`` into ``db_path``. Returns counts: seen, added_or_updated, unchanged, removed, unparseable."""
    if not source.is_dir():
        raise SystemExit(f"source root does not exist or is not a directory: {source}")
    if _inside(db_path, source):
        raise SystemExit("--db is inside the source root; the index must live outside the artifact tree")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"seen": 0, "added_or_updated": 0, "unchanged": 0, "removed": 0, "unparseable": 0}
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        known = {
            path: (size, mtime)
            for path, size, mtime in conn.execute("SELECT path, size, mtime_ns FROM run_states")
        }
        seen_paths = set()
        for path in iter_run_state_files(source):
            rel = path.relative_to(source).as_posix()
            seen_paths.add(rel)
            counts["seen"] += 1
            try:
                stat = path.stat()
            except OSError:
                continue
            if known.get(rel) == (stat.st_size, stat.st_mtime_ns):
                counts["unchanged"] += 1
                continue
            row = _row(source, path)
            conn.execute(
                "INSERT OR REPLACE INTO run_states (path, stream_dir, folder, size, mtime_ns, run_id, run_origin, "
                "manual_run, state_schema_version, parse_error) VALUES (:path, :stream_dir, :folder, :size, "
                ":mtime_ns, :run_id, :run_origin, :manual_run, :state_schema_version, :parse_error)",
                row,
            )
            counts["added_or_updated"] += 1
        stale = [p for p in known if p not in seen_paths]
        conn.executemany("DELETE FROM run_states WHERE path = ?", [(p,) for p in stale])
        counts["removed"] = len(stale)
        counts["unparseable"] = conn.execute("SELECT COUNT(*) FROM run_states WHERE parse_error IS NOT NULL").fetchone()[0]
    return counts


def summarize(db_path: Path) -> Dict[str, Any]:
    if not db_path.is_file():
        raise SystemExit(f"index not found: {db_path}")
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        by_stream = conn.execute(
            "SELECT stream_dir, COUNT(*), SUM(CASE WHEN manual_run = 1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN parse_error IS NOT NULL THEN 1 ELSE 0 END) FROM run_states GROUP BY stream_dir ORDER BY stream_dir"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM run_states").fetchone()[0]
    return {
        "total": total,
        "streams": [
            {"stream_dir": s, "run_states": n, "manual": int(m or 0), "unparseable": int(u or 0)}
            for s, n, m, u in by_stream
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Derived SQLite index of run_state*.json files (read-only on source).")
    parser.add_argument("--source", help="artifact root to index (default: $DCA_CRON_TRACKING_ROOT)")
    parser.add_argument("--db", required=True, help="SQLite file to create/update (must be outside the artifact tree)")
    parser.add_argument("--summary", action="store_true", help="print counts from an existing index and exit")
    return parser


def _default_source() -> Optional[str]:
    try:
        from dca_paths import cron_tracking_root
    except ImportError:
        return None
    return cron_tracking_root()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if args.summary:
        print(json.dumps(summarize(db_path), indent=2, sort_keys=True))
        return 0
    source_arg = args.source or _default_source()
    if not source_arg:
        print("error: no --source given and dca_paths could not be imported", file=sys.stderr)
        return 2
    print(json.dumps(build_index(Path(source_arg), db_path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
