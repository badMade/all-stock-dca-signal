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
   before D, whose ``ingest`` block still matches its pages, replays here as one complete
   pagination chain, contains every broker order id a validated run_state recorded on a later day,
   and NO order of any side or state in it was created from D 14:00 ET up to the next market
   day's 09:30 ET open.

Condition 3 is the $0 proof. The marker (condition 2) is NOT run evidence and proves nothing about
dollars -- it only covers the fire that wrote it, and a different in-window fire for the same pair
could have placed orders and crashed before writing run_state.json. It is required anyway so an
UNEXPLAINED gap (no marker) still blocks and still reaches the operator, and so only the narrow
"fired, Gate 1 refused it, and the account shows nothing" case is ever excused.

Standard library only; reads files, never writes, never calls the broker.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Collection, Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

import dca_runner
from dca_config import _reject_duplicate_keys
from scripts.abort_markers import MarkerFileInvalid, read_markers_strict
from scripts.assemble_order_pages import TOOL as ASSEMBLER_TOOL
from scripts.assemble_order_pages import _canonical
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

# Any order created from 14:00 ET on D up to the NEXT market day's 09:30 ET open blocks. Wider than
# both acceptance windows on purpose: it covers a late in-window fire, an off-window fire that got
# past Gate 1, after-hours queued orders, and (review 2026-11-11) an order stamped just after
# midnight and queued for the next session -- the old D+1 00:00 end let that one through. The
# next session's own orders (09:30 onward) belong to that day's slots and do not block. Side, state and placed_agent are ignored on purpose (same reasoning as
# dca_runner.gate_sole_executor): rows cannot be attributed to "afternoon-scheduled", so any row at
# all is ambiguous, and a manual afternoon order keeps 12:30 blocked -- the safe direction.
ORDER_WINDOW_START_ET = time(14, 0)
NEXT_OPEN_ET = time(9, 30)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

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
    # The per-line rules (parse, kind, slot/day, tz-aware fire time) are the shared strict reader's;
    # only the gate policy below is this module's own (2026-11-12 consolidation).
    try:
        markers = read_markers_strict(marker_root, day, slot_id)
    except MarkerFileInvalid as exc:
        return False, str(exc)
    if markers is None:
        return False, "no run_state.json and no evidence-free abort marker"
    if not markers:
        return False, "no run_state.json and an empty abort-marker file"
    for marker in markers:
        gate_reason = (marker.gate, marker.reason_code)
        if gate_reason not in EXCUSABLE_MARKERS:
            return False, (f"abort marker line {marker.line} is {gate_reason[0]}/{gate_reason[1]}, "
                           "not a pre-broker gate1/GATE1_OFF_WINDOW abort")
    return True, f"{len(markers)} gate1/GATE1_OFF_WINDOW marker(s)"


def order_window(day: date) -> Tuple[datetime, datetime]:
    """``[D 14:00 ET, next market day 09:30 ET)`` -- the afternoon plus everything queued overnight.

    The next market day comes from Gate 0's own calendar (``dca_runner.market_day_status``), so a
    Friday's window runs through the weekend to Monday's open. An early-close day still opens at
    09:30, so only ``is_closed`` is skipped. A year the calendar does not cover raises, as Gate 0 does.
    """
    nxt = day + timedelta(days=1)
    while dca_runner.market_day_status(nxt).is_closed:
        nxt += timedelta(days=1)
    return datetime.combine(day, ORDER_WINDOW_START_ET, tzinfo=ET), datetime.combine(nxt, NEXT_OPEN_ET, tzinfo=ET)


def ingest_provenance(envelope: Mapping[str, Any], order_count: int) -> Tuple[bool, str]:
    """``(True, detail)`` only if the envelope's ``ingest`` block still describes its pages.

    The replay proves the chain's SHAPE, not its origin: a hand-built ``{"pages": [{"orders": []}]}``
    replays as complete. The assembler records ``ingest.payload_sha256`` (sha256 of the canonical
    pages) and its order count, so recomputing them here refuses a hand-built file (no ingest) and
    any edit after assembly (a row removed, a timestamp moved). ``pages_sha256`` hashes the RAW page
    bytes the assembler read, which the envelope no longer holds, so it can only be checked for
    shape (one sha256 per page). This catches edits, not a forger who recomputes the hashes.
    """
    ingest = envelope.get("ingest")
    if not isinstance(ingest, dict):
        return False, "orders file has no assemble_order_pages ingest block"
    if ingest.get("tool") != ASSEMBLER_TOOL:
        return False, "orders file was not written by scripts.assemble_order_pages"
    if ingest.get("payload_sha256") != hashlib.sha256(_canonical(envelope["pages"])).hexdigest():
        return False, "orders file pages do not match ingest.payload_sha256 (edited after assembly)"
    recorded = ingest.get("order_count")
    if isinstance(recorded, bool) or recorded != order_count:
        return False, f"orders file holds {order_count} orders but ingest.order_count is {recorded!r}"
    if ingest.get("mode") == "state_slices":
        if not isinstance(ingest.get("slices"), dict) or not ingest["slices"]:
            return False, "state-slice orders file has no per-slice ingest record"
    else:
        digests = ingest.get("pages_sha256")
        if (not isinstance(digests, list) or len(digests) != len(envelope["pages"])
                or not all(isinstance(d, str) and _SHA256_HEX.fullmatch(d) for d in digests)):
            return False, "orders file ingest.pages_sha256 does not match its page count"
    return True, "ingest verified"


def known_order_ids_by_day(states: Iterable[Mapping[str, Any]]) -> Dict[date, Set[str]]:
    """Broker order ids each loaded run_state recorded, keyed by its top-level trading_day.

    Used to cross-check the envelope's self-reported fetch boundary: a fetch actually started after D
    would read as an empty D afternoon, but it would also be missing nothing; every order a later
    run_state recorded must be in the envelope too. States with no parseable trading_day are skipped
    (they cannot be placed after D).
    """
    out: Dict[date, Set[str]] = {}
    for state in states:
        try:
            day = date.fromisoformat(str(state.get("trading_day")))
        except ValueError:
            continue
        candidates = state.get("buy_candidates")
        for row in candidates if isinstance(candidates, list) else ():
            order_id = row.get("order_id") if isinstance(row, dict) else None
            if isinstance(order_id, str) and order_id.strip():
                out.setdefault(day, set()).add(order_id.strip().lower())
    return out


def load_orders_envelope(path: str) -> Any:
    """Parse the saved envelope, rejecting duplicate keys (the ``dca_config`` rule).

    Raises ``SystemExit`` with a one-line reason, so the prompt's block aborts with something an
    operator can read instead of a traceback.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"kill-switch gap orders file unreadable: {type(exc).__name__}") from exc


def broker_absence(
    envelope: Any, day: date, *, account_number: str,
    known_order_ids: Optional[Mapping[date, Collection[str]]] = None,
) -> Tuple[bool, str]:
    """``(True, detail)`` only if the envelope proves no order was created on D's afternoon.

    ``known_order_ids`` maps a trading day to broker order ids validated run_states recorded that day;
    every id from a day after D must be in the envelope, or its fetch boundary is unproven.

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
    ok, detail = ingest_provenance(envelope, len(snapshot.orders))
    if not ok:
        return False, detail

    # The envelope's trading_day is self-reported. Cross-check it against orders this repo already
    # knows were placed after D: a later-starting fetch would be missing the earliest of them.
    later_ids = sorted({str(oid).strip().lower() for d, ids in (known_order_ids or {}).items() if d > day for oid in ids})
    present = {str(order.order_id).lower() for order in snapshot.orders}
    for order_id in later_ids:
        if order_id not in present:
            return False, f"orders file does not contain known order {order_id}; fetch boundary unproven"
    boundary_note = (f"known_later_orders_present={len(later_ids)}" if later_ids
                     else "no known later order to cross-check the fetch boundary")

    window_start, window_end = order_window(day)
    in_window = 0
    for order in snapshot.orders:
        created = _parse_aware(order.created_at)
        if created is None:
            return False, f"order {order.order_id} has no timezone-aware created_at"
        if window_start <= created < window_end:
            in_window += 1
    if in_window:
        return False, (f"{in_window} broker order(s) created {day.isoformat()} 14:00 ET - "
                       f"{window_end.date().isoformat()} 09:30 ET")
    return True, f"broker_orders_in_window=0; {boundary_note}"


def classify_missing_pairs(
    missing_pairs: Iterable[Tuple[date, str]],
    *,
    claimed_pairs: Set[Tuple[date, str]],
    marker_root: Path,
    orders_envelope: Any = None,
    account_number: str = "",
    known_order_ids: Optional[Mapping[date, Collection[str]]] = None,
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
        ok, broker_detail = broker_absence(orders_envelope, day, account_number=account_number,
                                           known_order_ids=known_order_ids)
        verdicts[pair] = PairVerdict(EXCUSED if ok else BLOCKED,
                                     f"{detail}; {broker_detail}" if ok else broker_detail)
    return verdicts


def earliest_needing_proof(verdicts: Mapping[Tuple[date, str], PairVerdict]) -> Optional[date]:
    days: Sequence[date] = sorted(day for (day, _s), v in verdicts.items() if v.status == NEEDS_BROKER_PROOF)
    return days[0] if days else None
