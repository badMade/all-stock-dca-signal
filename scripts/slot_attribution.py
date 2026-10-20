"""Read-only broker order-ID attribution from each slot's dated run state."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from dca_paths import cron_tracking_root
from scripts.run_classification import _order_ids_from_state
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, _read_state, slot_run_dir_relative


def attribute_orders(
    day: date, order_ids: Sequence[str], *, root: Optional[Path] = None,
) -> dict[str, Any]:
    """Match IDs to artifact claims, never to order times or stream pointers.

    Missing or unreadable states contribute no claims. A match reports only
    that a slot's artifact names an ID, not that the slot verified or an order
    filled; multiple claiming slots are explicitly ambiguous.
    """
    base = Path(root) if root is not None else cron_tracking_root()
    claims: dict[str, list[str]] = {order_id: [] for order_id in order_ids}
    for slot_id in SLOT_EVIDENCE_LAYOUT:
        _, state = _read_state(base / slot_run_dir_relative(slot_id, day) / "run_state.json")
        if state is None:
            continue
        for order_id in _order_ids_from_state(state):
            if order_id in claims:
                claims[order_id].append(slot_id)
    return {
        "attributed": {oid: slots[0] for oid, slots in claims.items() if len(slots) == 1},
        "unattributed": [oid for oid, slots in claims.items() if not slots],
        "ambiguous": {oid: slots for oid, slots in claims.items() if len(slots) > 1},
    }
