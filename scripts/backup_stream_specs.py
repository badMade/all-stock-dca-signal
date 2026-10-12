"""How the backup planner scores each executor-class stream slot: with the primary's own spec.

Owner direction (2026-10-08): nothing is special about 10:30 or 12:30. They differ from the other slots only in
time, so none of the six may depend on the retired 09:45 pre-screen, on the Finance connector, on Yahoo, or on a
per-ticker source exception. Every slot is scored from the Robinhood data the Perplexity task fetches.

Until stage 2 this module patched 10:30 and 12:30 into that shape itself (a no-pre-flight executor subclass and a
re-sourced copy of the midday spec). Stage 2 made the primary's own specs match (12:30 is Robinhood-primary, the
09:45 pre-screen is gone), so :func:`stream_spec` now simply returns each slot's own spec object. That is the point
of the planner's safety gate: the backup cannot size differently from the primary because it is the same spec.
"""

from __future__ import annotations

import scripts.generate_intraday_candidates as intraday
import scripts.generate_market_open_candidates as market_open
import scripts.generate_midday_candidates as midday
import scripts.generate_morning_candidates as morning
from scripts.stream_candidates import StreamSpec

__all__ = ["STREAM_SLOTS", "stream_spec"]

STREAM_SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430")


def stream_spec(slot: str) -> StreamSpec:
    """The primary's own spec for ``slot`` (Robinhood-sourced, no per-ticker exception, no pre-screen)."""
    if slot == "0935":
        return market_open.MARKET_OPEN_SPEC
    if slot == "1030":
        return morning.MORNING_SPEC
    if slot == "1230":
        return midday.MIDDAY_SPEC
    if slot in ("1130", "1330", "1430"):
        return intraday.spec_for_slot(slot)
    raise ValueError(f"{slot!r} is not an executor-class stream slot")
