#!/usr/bin/env python3
"""Workspace persistence probe: does a scheduled cloud run keep its files between fires?

The cloud-clone plan hinges on one unknown: whether the Perplexity workspace keeps
``cron_tracking`` from one scheduled fire to the next. Each run of this probe
writes one timestamped marker under ``<root>/workspace_probe/`` and lists the
markers earlier runs left behind. Schedule it twice: if the second run lists the
first run's marker, the workspace persists.

Usage::

    python -m scripts.workspace_probe [--root DIR] [--now YYYY-MM-DDTHH:MM:SSZ]

Exit 0 = ok, exit 2 = refused (unusable root). Stdout is one JSON object:
``root``, ``marker``, ``written_at_utc``, ``prior_count`` and ``prior_markers``
(``name``, ``written_at_utc``, ``age_seconds``, ``readable``), oldest first.

Standard library only (plus the repo's own ``dca_paths`` for the default root).
No network, broker, order path or account number; the only side effect is one
small file written under the probe directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from dca_paths import cron_tracking_root

PROBE_DIR = "workspace_probe"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# <stamp>[-N].json: N orders same-second markers in the order they were written.
_MARKER_NAME = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<n>\d+))?\.json$")


def _parse_time(text: str) -> datetime:
    if not text.endswith("Z"):
        raise ValueError(text)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _argparse_time(text: str) -> datetime:
    try:
        return _parse_time(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DDTHH:MM:SSZ, got {text!r}") from exc


def _order_key(path: Path) -> tuple:
    # Sort by stamp then suffix number rather than by raw name: "-" sorts before
    # "." so plain string order would put "<stamp>-1.json" ahead of "<stamp>.json".
    match = _MARKER_NAME.match(path.name)
    if match:
        return (0, match["stamp"], int(match["n"] or 0), path.name)
    return (1, "", 0, path.name)


def _describe_prior(path: Path, now: datetime) -> Dict[str, Any]:
    """Report one earlier marker; anything unreadable is reported, never fatal."""
    unreadable: Dict[str, Any] = {"name": path.name, "written_at_utc": None, "age_seconds": None, "readable": False}
    try:
        written_at = json.loads(path.read_text(encoding="utf-8"))["written_at_utc"]
        # Age comes from the time recorded inside the marker, not file mtime,
        # which a copy or restore would reset.
        age_seconds = int((now - _parse_time(written_at)).total_seconds())
    except (OSError, ValueError, KeyError, TypeError):
        return unreadable
    return {"name": path.name, "written_at_utc": written_at, "age_seconds": age_seconds, "readable": True}


def _write_marker(probe_dir: Path, now: datetime) -> Path:
    """Create the new marker, never overwriting one written in the same second."""
    stamp = now.strftime(_STAMP_FORMAT)
    body = json.dumps({"written_at_utc": now.strftime(_TIME_FORMAT)}) + "\n"
    attempt = 0
    while True:
        path = probe_dir / (f"{stamp}.json" if attempt == 0 else f"{stamp}-{attempt}.json")
        try:
            # Exclusive create: the collision check and the write are one step.
            with path.open("x", encoding="utf-8") as handle:
                handle.write(body)
        except FileExistsError:
            attempt += 1
            continue
        return path


def _refusal(reason: str) -> int:
    print(json.dumps({"error": reason}))
    return 2


def run(root: Path, now: datetime) -> int:
    """Write this run's marker under ``root`` and list the ones left by earlier runs."""
    if root.resolve() == Path(root.resolve().anchor):
        return _refusal("refusing the filesystem root")
    if root.exists() and not root.is_dir():
        return _refusal(f"root is not a directory: {root}")

    probe_dir = root / PROBE_DIR
    probe_dir.mkdir(parents=True, exist_ok=True)
    # List before writing so this run's own marker never counts as a prior one.
    prior = [_describe_prior(path, now) for path in sorted(probe_dir.glob("*.json"), key=_order_key)]
    marker = _write_marker(probe_dir, now)

    print(
        json.dumps(
            {
                "root": str(root),
                "marker": f"{PROBE_DIR}/{marker.name}",
                "written_at_utc": now.strftime(_TIME_FORMAT),
                "prior_count": len(prior),
                "prior_markers": prior,
            }
        )
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", help="directory to probe (default: $DCA_CRON_TRACKING_ROOT)")
    parser.add_argument("--now", type=_argparse_time, help="UTC time to record (default: the current time)")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else Path(cron_tracking_root())
    now = args.now or datetime.now(timezone.utc).replace(microsecond=0)
    return run(root, now)


if __name__ == "__main__":
    sys.exit(main())
