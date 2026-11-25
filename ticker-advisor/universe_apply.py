"""Gated writer that applies an approved universe proposal to ``config/tickers.json``.

``universe.py`` only ever *proposes* a membership (``universe_proposal.json``). This module is the
one place allowed to turn a passing proposal into a change of the buy list, as decided in
``docs/adr-program-maintained-universe.md``:

- It is a BUY list. A name that leaves ``active`` simply stops being considered for new buys
  (reported as ``stopped_buying``). Nothing here sells, trims or closes a position.
- Only a proposal that passed every safeguard (``status == "pending_approval"``), whose
  ``content_sha256`` still matches its own content, that was built on the list ``tickers.json``
  holds right now, and that carries an approval id, is applied. Every failed check raises
  :class:`ApplyRefused` and leaves both files byte-identical (fail closed).
- There is NO limit on how many names change in one day. The cap of 100 and the floor of 90 still
  apply, and so does every input check ``universe.apply_safeguards`` already ran.
- Only the ``active`` array is rewritten. ``excluded``, ``whole_share``, ``symbol_corrections``,
  the notes and the formatting of the rest of the file stay byte-for-byte as they were.
- The write is atomic (temp file plus ``os.replace``), and the new active set is appended to the
  history file in its existing ``date,ticker`` format.

Pure stdlib, no network, no broker. The caller supplies every path; nothing here knows where the
live checkout is.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from scoring import TICKER_SYMBOL_PATTERN
from universe import proposal_hash_matches

#: ADR decision item 3: the active set never exceeds 100 and never drops below the 90 name floor.
MAX_ACTIVE = 100
MIN_ACTIVE = 90

# Matches the top-level ``"active": [ ... ]`` array of simple strings, and nothing else. A symbol
# list holds no brackets, so ``[^\]]*`` stops at the array's own closing bracket.
_ACTIVE_ARRAY = re.compile(r'("active"\s*:\s*)\[[^\]]*\]')
_SYMBOLS_PER_LINE = 10


class ApplyRefused(Exception):
    """A proposal was not applied. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ApplyResult:
    """What an applied proposal changed. ``stopped_buying`` is NOT a sale."""

    applied: bool
    added: tuple[str, ...]
    stopped_buying: tuple[str, ...]
    content_sha256: str


def _load_proposal(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApplyRefused("unreadable_proposal", str(exc)) from exc
    if not isinstance(document, dict) or document.get("kind") != "universe_proposal":
        raise ApplyRefused("unreadable_proposal", "not a universe_proposal document")
    return document


def _string_list(document: dict, key: str, code: str) -> list[str]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ApplyRefused(code, f"{key!r} must be a list of strings")
    return value


def _format_active(symbols: list[str]) -> str:
    """The array text, ``_SYMBOLS_PER_LINE`` quoted symbols per line, like the hand-kept file."""
    rows = [
        ", ".join(json.dumps(s) for s in symbols[i : i + _SYMBOLS_PER_LINE])
        for i in range(0, len(symbols), _SYMBOLS_PER_LINE)
    ]
    return "[\n" + ",\n".join(f"    {row}" for row in rows) + "\n  ]"


def _rewritten_tickers_text(original_text: str, original: dict, new_active: list[str]) -> str:
    """``original_text`` with only the ``active`` array replaced, verified by re-parsing."""
    matches = _ACTIVE_ARRAY.findall(original_text)
    if len(matches) != 1:
        raise ApplyRefused("rewrite_failed", f"expected one top-level active array, found {len(matches)}")
    new_text = _ACTIVE_ARRAY.sub(lambda m: m.group(1) + _format_active(new_active), original_text, count=1)
    try:
        reparsed = json.loads(new_text)
    except ValueError as exc:
        raise ApplyRefused("rewrite_failed", f"rewritten file is not valid JSON: {exc}") from exc
    untouched = {k: v for k, v in original.items() if k != "active"}
    if reparsed.get("active") != new_active or {k: v for k, v in reparsed.items() if k != "active"} != untouched:
        raise ApplyRefused("rewrite_failed", "rewrite changed something other than the active array")
    return new_text


def _history_text(history_path: Path, applied_on: date, new_active: list[str]) -> str:
    existing = history_path.read_text(encoding="utf-8") if history_path.exists() else "date,ticker\n"
    if existing and not existing.endswith("\n"):
        existing += "\n"
    return existing + "".join(f"{applied_on.isoformat()},{ticker}\n" for ticker in new_active)


def _stage(target: Path, text: str) -> str:
    """Write ``text`` to a temp file beside ``target`` and return its path."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return tmp


def apply_proposal(
    proposal_path: str | os.PathLike[str],
    tickers_path: str | os.PathLike[str],
    history_path: str | os.PathLike[str],
    *,
    approval_id: str,
    applied_on: date,
) -> ApplyResult:
    """Apply a passing proposal to ``tickers_path`` and append the result to ``history_path``.

    Args:
        proposal_path: A ``universe_proposal.json`` written by ``universe.write_proposal``.
        tickers_path: The ``config/tickers.json`` to update (only its ``active`` array changes).
        history_path: The ``ticker_history.csv`` to append the new active set to.
        approval_id: Record of the approval this write rests on (the standing ADR approval plus
            the proposal hash, or an operator approval). Must be non-empty.
        applied_on: The date stamped on the appended history rows.

    Returns:
        An :class:`ApplyResult` naming the names added and the names that stopped being bought.

    Raises:
        ApplyRefused: Any check failed. Both files are left exactly as they were.
        OSError: A file could not be written. The original files are restored or untouched.

    Side effects:
        Rewrites ``tickers_path`` and ``history_path`` atomically. Never places or cancels an order.
    """
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise ApplyRefused("no_approval_id", "a program write without an approval id is rejected")

    tickers_file, history_file = Path(tickers_path), Path(history_path)
    document = _load_proposal(Path(proposal_path))
    if document.get("status") != "pending_approval":
        raise ApplyRefused("status_not_pending_approval", f"status is {document.get('status')!r}")
    if not proposal_hash_matches(document):
        raise ApplyRefused("hash_mismatch", "content_sha256 does not match the proposal content")

    new_active = _string_list(document, "active", "invalid_proposal")
    prior_active = _string_list(document, "prior_active", "invalid_proposal")
    if _string_list(document, "recommended_active", "invalid_proposal") != new_active:
        raise ApplyRefused("inconsistent_proposal", "recommended_active differs from active")

    try:
        original_text = tickers_file.read_text(encoding="utf-8")
        original = json.loads(original_text)
    except (OSError, ValueError) as exc:
        raise ApplyRefused("unreadable_tickers", str(exc)) from exc
    if not isinstance(original, dict) or not isinstance(original.get("active"), list):
        raise ApplyRefused("unreadable_tickers", "tickers.json has no active list")
    if original["active"] != prior_active:
        raise ApplyRefused("stale_base", "the proposal was not built on the current active list")
    pinned = original.get("pinned", [])
    if not isinstance(pinned, list) or not all(isinstance(item, str) for item in pinned):
        raise ApplyRefused("unreadable_tickers", "tickers.json's pinned must be a list of strings")
    dropped_pins = sorted(set(pinned) - set(new_active))
    if dropped_pins:
        # Pinned names are hand-held: only the operator removes them, never a daily proposal.
        raise ApplyRefused("drops_pinned_name", ", ".join(dropped_pins))

    bad = [t for t in new_active if not TICKER_SYMBOL_PATTERN.match(t)]
    if bad:
        raise ApplyRefused("invalid_symbol", ", ".join(bad))
    if len(set(new_active)) != len(new_active):
        raise ApplyRefused("duplicate_symbol", "the proposed active list repeats a symbol")
    blocked = sorted(set(new_active) & set(original.get("excluded") or []))
    if blocked:
        raise ApplyRefused("excluded_name", ", ".join(blocked))
    whole_share = sorted(set(new_active) & set(original.get("whole_share") or []))
    if whole_share:
        raise ApplyRefused("whole_share_name", ", ".join(whole_share))
    if len(new_active) > MAX_ACTIVE:
        raise ApplyRefused("over_max_active", f"{len(new_active)} > {MAX_ACTIVE}")
    if len(new_active) < MIN_ACTIVE:
        raise ApplyRefused("below_min_active", f"{len(new_active)} < {MIN_ACTIVE}")

    new_tickers_text = _rewritten_tickers_text(original_text, original, new_active)
    new_history_text = _history_text(history_file, applied_on, new_active)

    staged: list[str] = []
    try:
        tickers_tmp = _stage(tickers_file, new_tickers_text)
        staged.append(tickers_tmp)
        history_tmp = _stage(history_file, new_history_text)
        staged.append(history_tmp)
        os.replace(tickers_tmp, tickers_file)
        staged.remove(tickers_tmp)
        try:
            os.replace(history_tmp, history_file)
            staged.remove(history_tmp)
        except BaseException:
            # The list changed but its history row did not. Put the original list back so the two
            # files never disagree, then surface the error.
            rollback = _stage(tickers_file, original_text)
            staged.append(rollback)
            os.replace(rollback, tickers_file)
            staged.remove(rollback)
            raise
    finally:
        for leftover in staged:
            Path(leftover).unlink(missing_ok=True)

    return ApplyResult(
        applied=True,
        added=tuple(t for t in new_active if t not in prior_active),
        stopped_buying=tuple(t for t in prior_active if t not in new_active),
        content_sha256=str(document["content_sha256"]),
    )
