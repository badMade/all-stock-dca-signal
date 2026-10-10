"""How the backup planner scores each executor-class stream slot: all six alike.

Owner direction (2026-10-08): nothing is special about 10:30 or 12:30. They differ from the other slots only in
time, so none of the six may depend on the retired 09:45 pre-flight, on the Finance connector, on Yahoo, or on a
per-ticker source exception. Every slot is scored from the Robinhood data the Perplexity task fetches.

Slots 0935, 1130, 1330 and 1430 already are Robinhood-primary. The other two need a small adaptation, made here
and not in their generators so the live Gate 5 is untouched by this change:

* **1030** is ``MORNING_SPEC`` without its pre-flight ``universe_filter``, run through
  :class:`NoPreflightMorningExecutor`, whose pre-flight gate is a no-op. Without that, the executor would consult
  the retired pre-flight artifact inside ``evaluate()``.
* **1230** is ``MIDDAY_SPEC`` as Robinhood-primary, the same change 09:35 went through on 2026-09-09: default
  5-minute bars and trend length, and no ``ticker_source_overrides`` (so no ticker is named here).

Stage 2 makes the primary's own specs match and retires this module's adaptations; the registry and tests stay.
"""

from __future__ import annotations

import dataclasses

import scripts.generate_intraday_candidates as intraday
import scripts.generate_market_open_candidates as market_open
import scripts.generate_midday_candidates as midday
import scripts.generate_morning_candidates as morning
from morning_dca import MorningDCAExecutor
from near_close_dca import SourceName
from scripts.stream_candidates import StreamSpec

__all__ = ["STREAM_SLOTS", "NoPreflightMorningExecutor", "stream_spec"]

STREAM_SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430")


class NoPreflightMorningExecutor(MorningDCAExecutor):
    """The 10:30 executor with the retired 09:45 pre-flight gate switched off.

    The gate's own contract was already non-blocking (a missing artifact meant the full universe), so skipping it
    changes what is scored only on a day an artifact existed. The backup must never depend on it either way.
    """

    def _apply_preflight_gate(self, now, tickers, *, manual=False):  # noqa: ARG002 - signature of the method it replaces
        self.preflight_gate = None
        return tickers


def stream_spec(slot: str) -> StreamSpec:
    """The spec ``slot`` is scored with: Robinhood-sourced, no pre-flight, no per-ticker exception."""
    if slot == "0935":
        return market_open.MARKET_OPEN_SPEC
    if slot == "1030":
        return dataclasses.replace(morning.MORNING_SPEC, universe_filter=None, executor_cls=NoPreflightMorningExecutor)
    if slot == "1230":
        return dataclasses.replace(
            midday.MIDDAY_SPEC,
            source=SourceName.ROBINHOOD,
            source_interval_minutes=None,
            trend_min_bars=None,
            ticker_source_overrides={},
        )
    if slot in ("1130", "1330", "1430"):
        return intraday.spec_for_slot(slot)
    raise ValueError(f"{slot!r} is not an executor-class stream slot")
