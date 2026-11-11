"""Prove a missing afternoon run_state pair deployed $0, or keep it blocking.

Why this exists (2026-11-10, DCA Issues key ``kill_switch_evidence_incomplete``): the 12:30 midday
prompt's Gate 5 derives ``--deployed-last-7d-usd`` from afternoon ``run_state.json`` artifacts and
requires one validated artifact for every ``(full market day, 14:30|15:30)`` pair in the trailing
``dca_risk.KILL_SWITCH_WINDOW_DAYS``, so the figure can never be understated. On 2026-11-09 the
Claude app was down, every slot fired at 19:24 ET and aborted ``GATE1_OFF_WINDOW`` before any run
evidence could exist, and 12:30 then aborted every day until 11-09 left the window. Writing
synthetic run_state files was refused (2026-11-10) because it would loosen the check itself. This
module is the narrow, fail-closed alternative; the full safety argument is
``docs/kill_switch_evidence_gap_proof.md``.

The rule, in short. A missing pair ``(D, S)`` is excused as $0 only when ALL hold:

1. no run_state.json claims the pair at all, not even a rejected one (that is a different defect);
2. its abort-marker file exists, is non-empty, and EVERY line is a well-formed Gate-1
   ``GATE1_OFF_WINDOW`` evidence-free abort marker for that day and slot -- a later-gate marker, a
   Gate-2 marker, a malformed line, or a mix blocks;
3. a ``scripts.assemble_order_pages`` envelope pinned to the account and to a ``trading_day`` on or
   before D replays here as one complete pagination chain, and NO order of any side or state in it
   was created in ``[D 14:00 ET, D+1 00:00 ET)``.

Condition 3 is the $0 proof. The marker (condition 2) is NOT run evidence and proves nothing about
dollars -- it only covers the fire that wrote it, and a different in-window fire for the same pair
could have placed orders and crashed before writing run_state.json. It is required anyway so an
UNEXPLAINED gap (no marker) still blocks and still reaches the operator, and so only the narrow
"fired, Gate 1 refused it, and the account shows nothing" case is ever excused.

Standard library only; reads files, never writes, never calls the broker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from dca_config import _reject_duplicate_keys
from scripts.abort_markers import MARKER_KIND, marker_path
from scripts.broker_reconciliation import same_day_order_snapshot
from scripts.same_day_top_up_gate import _PageReplayer

ET = ZoneInfo("America/New_York")

# Afternoon scheduled_for_et -> the abort-marker slot id record_evidence_free_abort.sh writes.
SLOT_IDS: Mapping[str, str] = {"14:30": "1430", "15:30": "1530"}

# The ONLY (gate, reason_code) a marker may carry to be a precondition for excusal. Gate 1 runs
# before Gate 4's broker read and long before Gate 7 placement, so the fire that wrote it placed
# nothing. Gate 2's GATE2_ABORT_NO_PYTHON311 is also pre-broker but is deliberately left out: keep
# the excused case exactly the outage this was built for, and widen it only by a reviewed change.
EXCUSABLE_MARKERS = frozenset({("gate1", "GATE1_OFF_WINDOW")})

# Any order created from 14:00 ET to midnight on D blocks. Wider than both acceptance windows on
# purpose: it covers a late in-window fire, an off-window fire that got past Gate 1, and after-hours
# queued orders. Side, state and placed_agent are ignored on purpose (same reasoning as
# dca_runner.gate_sole_executor): rows cannot be attributed to "afternoon-scheduled", so any row at
# all is ambiguous, and a manual afternoon order keeps 12:30 blocked -- the safe direction.
ORDER_WINDOW_START_ET = time(14, 0)

EXCUSED = "excused"
NEEDS_BROKER_PROOF = "needs_broker_proof"
BLOCKED = "blocked"


@dataclass(frozen=True)
class PairVerdict:
    status: str  # EXCUSED | NEEDS_BROKER_PROOF | BLOCKED
    reason: str


def _parse_aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def marker_precondition(marker_root: Path, day: date, scheduled_for: str) -> Tuple[bool, str]:
    """``(True, detail)`` only if every line of the pair's marker file is a clean Gate-1 marker.

    Deliberately stricter than ``scripts.abort_markers.read_abort_markers``, which SKIPS bad lines
    so the watchdog degrades toward ``slot_did_not_fire``. Here a skipped line could hide a
    later-gate marker, so any line that does not parse and match blocks the whole pair.
    """
    slot_id = SLOT_IDS.get(scheduled_for)
    if slot_id is None:
        return False, f"not an afternoon slot: {scheduled_for!r}"
    path = marker_path(marker_root, day, slot_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, "no run_state.json and no evidence-free abort marker"
    except (OSError, UnicodeError) as exc:
        return False, f"abort marker unreadable ({type(exc).__name__})"
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False, "no run_state.json and an empty abort-marker file"
    for number, line in enumerate(lines, start=1):
        try:
            marker = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except ValueError:
            return False, f"abort marker line {number} is malformed"
        if not isinstance(marker, dict) or marker.get("kind") != MARKER_KIND:
            return False, f"abort marker line {number} is not an evidence-free abort marker"
        if marker.get("slot_id") != slot_id or marker.get("trading_day") != day.isoformat():
            return False, f"abort marker line {number} is for another slot or day"
        if _parse_aware(marker.get("fired_at_utc")) is None:
            return False, f"abort marker line {number} has no timezone-aware fire time"
        gate_reason = (marker.get("gate"), marker.get("reason_code"))
        if gate_reason not in EXCUSABLE_MARKERS:
            return False, (f"abort marker line {number} is {gate_reason[0]}/{gate_reason[1]}, "
                           "not a pre-broker gate1/GATE1_OFF_WINDOW abort")
    return True, f"{len(lines)} gate1/GATE1_OFF_WINDOW marker(s)"


def load_orders_envelope(path: str) -> Any:
    """Parse the saved envelope, rejecting duplicate keys (the ``dca_config`` rule).

    Raises ``SystemExit`` with a one-line reason, so the prompt's block aborts with something an
    operator can read instead of a traceback.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"kill-switch gap orders file unreadable: {type(exc).__name__}") from exc


def broker_absence(envelope: Any, day: date, *, account_number: str) -> Tuple[bool, str]:
    """``(True, detail)`` only if the envelope proves no order was created on D's afternoon.

    The pages are REPLAYED here through the same ``same_day_order_snapshot`` that
    ``scripts.assemble_order_pages`` used to write them, so a hand-built or truncated file is refused
    for what it is rather than trusted because it has the right keys. The 2026-10-22 completeness
    carve-out (log-and-continue) does not apply: this is a proof of absence, so unproven
    completeness blocks.
    """
    if not isinstance(envelope, dict) or not isinstance(envelope.get("pages"), list) or not envelope["pages"]:
        return False, "orders file is not an assemble_order_pages envelope"
    account = str(envelope.get("account_number") or "").strip()
    if not account_number.strip() or account != account_number.strip():
        return False, "orders file is not pinned to this account"
    try:
        boundary = date.fromisoformat(str(envelope.get("trading_day")))
    except ValueError:
        return False, "orders file has no trading_day boundary"
    if boundary > day:
        return False, f"orders file starts {boundary.isoformat()}, after {day.isoformat()}"

    replayer = _PageReplayer(envelope["pages"])
    snapshot = same_day_order_snapshot(
        replayer, account_number=account, created_at_gte=boundary.isoformat(),
        max_pages=max(len(envelope["pages"]) + 1, 2),
    )
    if not replayer.fully_consumed or not snapshot.pagination_complete:
        reasons = ",".join(snapshot.incomplete_reasons) or "unconsumed_saved_order_pages"
        return False, f"order history not provably complete ({reasons})"

    window_start = datetime.combine(day, ORDER_WINDOW_START_ET, tzinfo=ET)
    window_end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=ET)
    in_window = 0
    for order in snapshot.orders:
        created = _parse_aware(order.created_at)
        if created is None:
            return False, f"order {order.order_id} has no timezone-aware created_at"
        if window_start <= created < window_end:
            in_window += 1
    if in_window:
        return False, f"{in_window} broker order(s) created {day.isoformat()} 14:00-24:00 ET"
    return True, "broker_orders_in_window=0"


def classify_missing_pairs(
    missing_pairs: Iterable[Tuple[date, str]],
    *,
    claimed_pairs: Set[Tuple[date, str]],
    marker_root: Path,
    orders_envelope: Any = None,
    account_number: str = "",
) -> Dict[Tuple[date, str], PairVerdict]:
    """One verdict per missing pair. Only ``EXCUSED`` pairs may count as $0 coverage."""
    verdicts: Dict[Tuple[date, str], PairVerdict] = {}
    for pair in sorted(missing_pairs):
        day, scheduled_for = pair
        if pair in claimed_pairs:
            verdicts[pair] = PairVerdict(BLOCKED, "a run_state.json claims this pair but was rejected")
            continue
        ok, detail = marker_precondition(marker_root, day, scheduled_for)
        if not ok:
            verdicts[pair] = PairVerdict(BLOCKED, detail)
            continue
        if orders_envelope is None:
            verdicts[pair] = PairVerdict(NEEDS_BROKER_PROOF, detail)
            continue
        ok, broker_detail = broker_absence(orders_envelope, day, account_number=account_number)
        verdicts[pair] = PairVerdict(EXCUSED if ok else BLOCKED,
                                     f"{detail}; {broker_detail}" if ok else broker_detail)
    return verdicts


def earliest_needing_proof(verdicts: Mapping[Tuple[date, str], PairVerdict]) -> Optional[date]:
    days: Sequence[date] = sorted(day for (day, _s), v in verdicts.items() if v.status == NEEDS_BROKER_PROOF)
    return days[0] if days else None
