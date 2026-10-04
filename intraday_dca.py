"""Intraday graded-DCA executor (11:30 AM / 1:30 PM / 2:30 PM ET live stream).

This module adds an *intraday* live-execution stream on top of the morning
executor pipeline (:mod:`morning_dca`), reusing its evaluate/build/write
machinery via subclassing and overriding only the stream-identity attributes.

Before this module existed, cron ``7be82b97`` was marked
``no_python_entry_point: true`` in ``config/scheduled_tasks.json``: an external
scheduler/agent ran all three slots directly from the manifest's prose, so none
of this repo's code enforced their approved-trading-window cutoff, budget ledger,
per-order ceiling, or same-day top-up guard.

It also closes the specific gap the now-removed GATE5 proposal doc's section 1.4 raised
(see ``PRODUCTION_READINESS.md`` gap #1):
fills at 11:30/1:30/2:30 existed nowhere in the prior-allocation chain, so a
ticker bought at one of those slots was invisible to the guard at the next and
would be re-bought full-size at a successively higher price -- exactly what the
guard exists to prevent. Writing real artifacts under ``intraday_dca`` (a root
already listed in every sibling stream's ``prior_allocation_roots``) closes it in
both directions.

**One stream, one cron id, three fire times.**
``schemas/intraday_run_classification.schema.json`` pins ``stream`` to the
constant ``"intraday"`` (explicitly not a slot-suffixed variant),
``scheduled_for_et`` to the enum ``11:30|13:30|14:30``, and ``run_id`` to
``intraday_live_YYYYMMDD_(1130|1330|1430)ET``. The three slots therefore share
one stream identity, one cron id, and one pooled budget ledger, differing only in
fire time, run id, and dated artifact folder. Select one with the ``slot``
constructor argument::

    IntradayDCAExecutor(slot="1330").run(now, data, broker=broker)

The caller passes whichever slot actually fired. Because the slot varies the
*profile contents* rather than the mode key, every inherited
``self.mode == MORNING_LIVE_MODE`` check in :class:`MorningDCAExecutor` still
treats all three as live runs without needing to be generalized.

Other key properties (see ``config/intraday_dca.json``):

    - **Live Robinhood-only execution** on account
      ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (supplied by the environment, never
      committed); Alpaca/Finance/Yahoo stay read-only.
    - Uses the **shared 0x -> 5x sizing ladder** and dynamic $1/$2 base, the same
      active universe (excluding the whole-share tickers BYDDY and RVII),
      and the same shared $180/$900 Normal-tier ceiling -- pooled across all
      three slots combined, not $180/$900 each.
    - **Trend validation applies to all three slots**, unlike the 9:35
      market-open slot: each has a full 60-minute regular-session window
      available. A candidate whose price/drop qualifies but whose trend cannot be
      validated is flagged ``manual_review_candidate`` and NOT auto-bought.
    - **Independent** of every other stream: ``stream='intraday'``,
      ``valid_morning_evidence=false``, ``valid_afternoon_evidence=false``.

Configured with ``live_execution_enabled=true`` in its config (promoted to live
following full shadow-versus-live parity validation against external execution).

Writes ONLY under its own run-state root
``<cron_tracking_root>/intraday_dca/YYYYMMDD_<slot>ET/`` and never touches the
market-open, morning, midday, or afternoon artifact trees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Dict, Optional, Tuple

from dca_paths import stream_root
from morning_dca import (
    MorningConfig,
    MorningDCAExecutor,
    load_stream_config,
)
from near_close_dca import StrategyConfig, _DiagnosticRedactionFilter

_log = logging.getLogger(__name__)
_log.addFilter(_DiagnosticRedactionFilter())


# --- Intraday stream identity / classification constants ---------------------
INTRADAY_STREAM = "intraday"

# Reuses the literal "live" mode key, exactly as the midday stream does. The slot
# is a separate axis (see INTRADAY_SLOT_PROFILES) so the mode key stays "live"
# for all three and every inherited live-mode check keeps working.
INTRADAY_LIVE_MODE = "live"
INTRADAY_LIVE_CRON_ID = "7be82b97"
INTRADAY_LIVE_RUN_TYPE = "intraday_dca_live"
INTRADAY_LIVE_SCHEMA_VERSION = "intraday-live-v1"

# Artifact root: a sibling of, and never nested under, any other stream's root.
DEFAULT_INTRADAY_LIVE_STATE_ROOT = stream_root("intraday_dca")

# The three fire times. Keys are the slot tokens used in run ids and folder
# names; the values are the only fields that vary between slots.
INTRADAY_SLOTS: Tuple[str, ...] = ("1130", "1330", "1430")
DEFAULT_INTRADAY_SLOT = "1130"

_SLOT_SPECS: Dict[str, Dict[str, Any]] = {
    "1130": {
        "scheduled_for_et": "11:30",
        "window_start_et": "11:25",
        "window_end_et": "11:35",
        "decision_time_et": time(11, 30),
    },
    "1330": {
        "scheduled_for_et": "13:30",
        "window_start_et": "13:25",
        "window_end_et": "13:35",
        "decision_time_et": time(13, 30),
    },
    "1430": {
        "scheduled_for_et": "14:30",
        "window_start_et": "14:25",
        "window_end_et": "14:35",
        "decision_time_et": time(14, 30),
    },
}


def _build_slot_profile(slot: str) -> Dict[str, Any]:
    """Build the executor profile for one intraday slot."""
    spec = _SLOT_SPECS[slot]
    return {
        "mode": INTRADAY_LIVE_MODE,
        "artifact_mode": "live",
        "trades": True,
        "cron_id": INTRADAY_LIVE_CRON_ID,
        "run_type": INTRADAY_LIVE_RUN_TYPE,
        "scheduled_for_et": spec["scheduled_for_et"],
        "window_start_et": spec["window_start_et"],
        "window_end_et": spec["window_end_et"],
        "state_schema_version": INTRADAY_LIVE_SCHEMA_VERSION,
        "state_root": DEFAULT_INTRADAY_LIVE_STATE_ROOT,
        "state_root_stream": "intraday_dca",
        "folder_suffix": f"{slot}ET",
        "scheduled_run_id_prefix": "intraday_live",
        "manual_run_id_prefix": "manual_intraday_live",
        "decision_time_et": spec["decision_time_et"],
        # All three slots have a full 60-minute session window available, so
        # unlike the 9:35 market-open slot none of them is trend-exempt.
        "validate_trend": True,
    }


# Slot token -> profile. Not keyed by mode: the executor installs the selected
# slot's profile under the "live" mode key per-instance (see __init__).
INTRADAY_SLOT_PROFILES: Dict[str, Dict[str, Any]] = {
    slot: _build_slot_profile(slot) for slot in INTRADAY_SLOTS
}

# The same shared 0x -> 5x ladder every live stream uses (config/sizing_ladder.json).
INTRADAY_SIZING_TIERS: Tuple[Tuple[float, float], ...] = StrategyConfig().sizing_tiers
INTRADAY_MINIMUM_DROP = StrategyConfig().minimum_drop_vs_previous_close

# The shared Normal-tier ceiling every live stream draws from, pooled across all
# THREE slots combined rather than granted to each: cap_seed_roots below includes
# this stream's own root, so 1:30 seeds from 11:30's fills and 2:30 from both.
# Like morning and midday, these slots never independently escalate to
# Elevated/High-volatility/Stress; only the 3:30 final run does.
DEFAULT_INTRADAY_DAILY_CAP_USD = 180.0
DEFAULT_INTRADAY_WEEKLY_CAP_USD = 900.0


def default_intraday_prior_allocation_roots() -> tuple[str, ...]:
    """Roots scanned for same-day allocations preceding an intraday slot.

    One list for all three slots rather than a per-slot list. ``midday_dca`` is
    included even for the 11:30 slot, which fires before midday: a root that did
    not run that day contributes nothing, so listing it is harmless on a normal
    day and strictly safer on an abnormal one -- an early midday retry or a
    hand-placed midday recovery before 11:30 is then correctly seen as prior
    allocation rather than silently re-bought at a higher price.

    ``intraday_dca`` is this stream's own root, which is what lets 1:30 see
    11:30's fills and 2:30 see both. ``ce16ba6b`` is deliberately absent:
    scheduled afternoon runs are never prior context, and the afternoon slot
    fires after all three of these anyway.

    A function, not a module-level tuple: a literal would bind
    ``$DCA_CRON_TRACKING_ROOT`` at import time (Codex review, PR #219).
    """
    return (
        stream_root("market_open_dca"),
        stream_root("morning_dca"),
        stream_root("midday_dca"),
        stream_root("intraday_dca"),
        stream_root("final_manual_review"),
    )


def default_intraday_cap_seed_roots() -> tuple[str, ...]:
    """Sibling roots whose same-day/week spend counts against the shared cap.

    Every live stream including this one, so the three slots share one pooled
    ceiling and a restart/retry re-seeds from this stream's own prior fills.
    """
    return (
        stream_root("intraday_dca"),
        stream_root("market_open_dca"),
        stream_root("morning_dca"),
        stream_root("midday_dca"),
        stream_root("ce16ba6b"),
        stream_root("final_manual_review"),
    )


@dataclass(frozen=True)
class IntradayConfig(MorningConfig):
    """Configuration for the intraday live-execution stream (all three slots)."""

    sizing_tiers: Tuple[Tuple[float, float], ...] = INTRADAY_SIZING_TIERS
    minimum_drop_vs_previous_close: float = INTRADAY_MINIMUM_DROP
    morning_daily_cap_usd: float = DEFAULT_INTRADAY_DAILY_CAP_USD
    morning_weekly_cap_usd: float = DEFAULT_INTRADAY_WEEKLY_CAP_USD
    prior_allocation_roots: Tuple[str, ...] = field(
        default_factory=default_intraday_prior_allocation_roots
    )
    cap_seed_roots: Tuple[str, ...] = field(default_factory=default_intraday_cap_seed_roots)


def load_intraday_config(config_dir=None, *, allow_unpinned_account: bool = False):
    """Read ``config/intraday_dca.json`` and return an :class:`IntradayConfig`."""
    return load_stream_config(
        config_dir,
        filename="intraday_dca.json",
        config_cls=IntradayConfig,
        daily_cap_key="intraday_daily_cap_usd",
        weekly_cap_key="intraday_weekly_cap_usd",
        source_label="intraday",
        allow_unpinned_account=allow_unpinned_account,
    )


class IntradayDCAExecutor(MorningDCAExecutor):
    """Intraday live graded-DCA executor (11:30 AM / 1:30 PM / 2:30 PM ET).

    Subclasses :class:`MorningDCAExecutor` to reuse the entire
    evaluate/build/write pipeline under the intraday stream identity. The fired
    slot is selected with the ``slot`` constructor argument; it varies the
    profile contents, never the mode key, so all three slots are ordinary live
    runs as far as the inherited pipeline is concerned.
    """

    stream = INTRADAY_STREAM
    # Class-level default so the attribute always exists; __init__ replaces it
    # with a single-entry instance dict carrying the selected slot's profile.
    PROFILES: Dict[str, Dict[str, Any]] = {
        INTRADAY_LIVE_MODE: INTRADAY_SLOT_PROFILES[DEFAULT_INTRADAY_SLOT]
    }
    ledger_namespace: str = INTRADAY_STREAM
    ladder_name: str = "intraday_0x_to_5x"
    signals_csv_name: str = "intraday_signals.csv"
    latest_pointer_name: str = "latest_intraday_run.json"
    budget_daily_key: str = "intraday_daily_cap_usd"
    budget_weekly_key: str = "intraday_weekly_cap_usd"
    budget_label: str = "intraday"
    manual_run_subdir: str = "manual_runs"
    # manual_runs/ here holds reruns of this same scheduled task, so they count
    # against this stream's own cap ledger -- matching morning and market-open.
    scan_manual_reruns_for_ledger: bool = True
    # config/scheduled_tasks.json:7be82b97.budget_ledger defines a ledger_dir and
    # ledger_filename with a bootstrap note authorising creation.
    writes_budget_ledger: bool = True

    def __init__(
        self,
        config: Optional[IntradayConfig] = None,
        ledger=None,
        *,
        slot: str = DEFAULT_INTRADAY_SLOT,
        mode: str = INTRADAY_LIVE_MODE,
        config_dir: Optional[str] = None,
    ) -> None:
        if slot not in INTRADAY_SLOT_PROFILES:
            raise ValueError(
                f"unknown intraday slot {slot!r}; expected one of {sorted(INTRADAY_SLOT_PROFILES)}"
            )
        self.slot = slot
        # Shadow the class attribute with a per-instance mapping so the inherited
        # __init__ resolves self.profile to THIS slot. PROFILES is read exactly
        # once, in MorningDCAExecutor.__init__, which is why varying it per
        # instance is sufficient and no other inherited code needs changing.
        self.PROFILES = {INTRADAY_LIVE_MODE: INTRADAY_SLOT_PROFILES[slot]}
        super().__init__(config, ledger, mode=mode, config_dir=config_dir)

    def _load_default_config(self, config_dir: Optional[str], mode: str) -> IntradayConfig:
        # Live-only stream: tolerate an unresolvable account so the run reaches
        # its fail-closed path and leaves blocked evidence, rather than raising
        # before an executor exists and leaves the slot with no artifact at all.
        return load_intraday_config(config_dir, allow_unpinned_account=True)

    def _idempotency_stream(self) -> str:
        # All three slots share self.stream == INTRADAY_STREAM (pinned by
        # schemas/intraday_run_classification.schema.json), so the inherited
        # ref_id/_same_stream_order_exists namespace must include the fired
        # slot too -- otherwise a later slot's legitimate, lower-price
        # incremental top-up collides with an earlier slot's ref id and gets
        # treated as an already-submitted retry, capping a qualifying ticker
        # to at most one intraday order all day (Codex review, P1: "Scope
        # intraday idempotency keys to each slot"). A retry of THIS SAME slot
        # still resolves to the identical namespace, so same-slot idempotency
        # is preserved.
        return f"{self.stream}-{self.slot}"


__all__ = [
    "DEFAULT_INTRADAY_DAILY_CAP_USD",
    "DEFAULT_INTRADAY_LIVE_STATE_ROOT",
    "DEFAULT_INTRADAY_SLOT",
    "DEFAULT_INTRADAY_WEEKLY_CAP_USD",
    "INTRADAY_LIVE_CRON_ID",
    "INTRADAY_LIVE_MODE",
    "INTRADAY_LIVE_RUN_TYPE",
    "INTRADAY_LIVE_SCHEMA_VERSION",
    "INTRADAY_SIZING_TIERS",
    "INTRADAY_SLOTS",
    "INTRADAY_SLOT_PROFILES",
    "INTRADAY_STREAM",
    "IntradayConfig",
    "IntradayDCAExecutor",
    "default_intraday_cap_seed_roots",
    "default_intraday_prior_allocation_roots",
    "load_intraday_config",
]
