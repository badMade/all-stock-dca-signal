#!/usr/bin/env python3
"""Show or release the dated ``no_buy`` block in ``config/tickers.json``.

A ticker sold at a realized loss is paused from buying for the wash-sale window (``docs/adr-no-buy-window.md``,
``docs/adr-tax-loss-sale.md``). This command is the operator's way to look at that block and, if they decide,
release a ticker early. It places no orders and reads no broker data.

Usage::

    python -m scripts.no_buy_window status [--on YYYY-MM-DD]
    python -m scripts.no_buy_window release SYMBOL --reason "why" --accept-wash [--on YYYY-MM-DD]

Safety rules (fail closed, exit status 2 and the file untouched on any of them):

* A release INSIDE the window refuses unless ``--accept-wash`` is given: buying the same stock within 30 days
  of a loss sale can wash that loss, and a purchase in an IRA or Roth can disallow it permanently. The flag
  says the operator has read that.
* ``--reason`` is always required and is recorded as ``release_reason``.
* A release never deletes the entry; it sets ``released_on`` so the history stays in the file. Only that
  entry's own line changes.
* A malformed ``no_buy`` block is refused, not edited.

This is not tax advice; confirm the consequences with a tax professional.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dca_config import _today_et, get_config_dir, no_buy_blocked_tickers  # noqa: E402

_WARNING = (
    "Releasing inside the window means a buy of this ticker can wash the loss from the sale. A buy in any of "
    "your accounts counts, and in an IRA or Roth the loss is disallowed permanently. Confirm with a tax "
    "professional."
)


def _parse_on(raw: str | None) -> date:
    return date.fromisoformat(raw) if raw else _today_et()


def _fail(message: str) -> int:
    print(f"no_buy_window: {message}", file=sys.stderr)
    return 2


def _state(entry: dict, on: date) -> str:
    blocked_on, until = date.fromisoformat(entry["blocked_on"]), date.fromisoformat(entry["until"])
    released = entry.get("released_on")
    if released is not None and on >= date.fromisoformat(released):
        return "released"
    if on < blocked_on:
        return "scheduled"
    if on > until:
        return "expired"
    return "blocked"


def _status(path: Path, on: date) -> int:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    no_buy_blocked_tickers(cfg, on)  # validates the whole block; raises ValueError if malformed
    block = cfg.get("no_buy") or {}
    if not block:
        print("no_buy: empty")
        return 0
    print(f"no_buy as of {on}:")
    for symbol, entry in block.items():
        released = entry.get("released_on")
        extra = f" released_on={released}" if released else ""
        print(
            f"  {symbol:<6} {_state(entry, on):<9} {entry['blocked_on']} .. {entry['until']}{extra}  {entry['reason']}"
        )
    return 0


def _release(path: Path, symbol: str, on: date, reason: str | None, accept_wash: bool) -> int:
    text = path.read_text(encoding="utf-8")
    cfg = json.loads(text)
    no_buy_blocked_tickers(cfg, on)  # refuse to edit a malformed block
    entry = (cfg.get("no_buy") or {}).get(symbol)
    if entry is None:
        return _fail(f"{symbol} is not in no_buy")
    if entry.get("released_on") is not None:
        return _fail(f"{symbol} was already released on {entry['released_on']}")
    blocked_on, until = date.fromisoformat(entry["blocked_on"]), date.fromisoformat(entry["until"])
    if on > until:
        return _fail(f"{symbol}'s window already over (ended {until}); nothing to release")
    if on < blocked_on:
        return _fail(f"--on {on} is before {symbol}'s blocked_on {blocked_on}")
    if not reason or not reason.strip():
        return _fail("--reason is required")
    if not accept_wash:
        return _fail(f"{symbol} is blocked until {until}. {_WARNING} Re-run with --accept-wash to release anyway.")

    # Text surgery on the one entry line, not a json round-trip: tickers.json is hand-formatted and carries a
    # very long notes field, and a rewrite would reformat the whole file and bury this change in the diff.
    line_re = re.compile(rf'^(\s*"{re.escape(symbol)}":\s*\{{.*)"released_on": null(\}}.*)$', re.MULTILINE)
    release_fields = f'"released_on": {json.dumps(on.isoformat())}, "release_reason": {json.dumps(reason.strip())}'
    new_text, n = line_re.subn(lambda m: f"{m.group(1)}{release_fields}{m.group(2)}", text, count=1)
    if n != 1:
        return _fail(f"could not find {symbol}'s single-line entry with released_on null; edit tickers.json by hand")
    # Re-validate the result before writing: it must parse and the release must actually take effect.
    new_cfg = json.loads(new_text)
    if symbol in no_buy_blocked_tickers(new_cfg, on):
        return _fail("internal error: release did not unblock the ticker; file left unchanged")

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tickers.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"Released {symbol} from {on} (window was {entry['blocked_on']} .. {until}). Reason: {reason.strip()}")
    print(f"WARNING: {_WARNING}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="no_buy_window", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", type=Path, default=None, help="directory holding tickers.json")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="list every no_buy entry and its state")
    status.add_argument("--on", default=None, help="evaluate as of YYYY-MM-DD (default: today, US Eastern)")
    release = sub.add_parser("release", help="release a ticker early (recorded, never deleted)")
    release.add_argument("symbol")
    release.add_argument("--reason", default=None, help="why (required)")
    release.add_argument("--accept-wash", action="store_true", help="acknowledge a release inside the window")
    release.add_argument("--on", default=None, help="release effective YYYY-MM-DD (default: today, US Eastern)")
    # --config-dir is also accepted after the subcommand, where operators naturally type it.
    for sp in (status, release):
        sp.add_argument("--config-dir", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    path = (args.config_dir or get_config_dir()) / "tickers.json"
    try:
        on = _parse_on(args.on)
        if args.command == "status":
            return _status(path, on)
        return _release(path, args.symbol.strip().upper(), on, args.reason, args.accept_wash)
    except (ValueError, OSError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
