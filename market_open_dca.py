"""Market-open graded-DCA executor (9:35 AM ET live Robinhood execution stream).

This module adds a *market-open* live-execution stream on top of the morning
executor pipeline (:mod:`morning_dca`), reusing its evaluate/build/write
machinery via subclassing and overriding only the stream-identity attributes.

Before this module existed, cron ``2ba19526`` was marked
``no_python_entry_point: true`` in ``config/scheduled_tasks.json``: an external
scheduler/agent ran the 9:35 slot directly from the manifest's prose, so none of
this repo's code enforced its approved-trading-window cutoff, budget ledger,
per-order ceiling, or same-day top-up guard. Two consequences were documented
rather than fixed -- the slot had no code-level date gate at all until 2026-07-27
(it would have kept placing real orders past ``config/trading_window.json``'s
end date), and its ``$180/$900`` pooled cap was policy an external agent had to
self-enforce because it could not read sibling ledgers back. Both are now
enforced here.

Key properties (see ``config/market_open_dca.json`` and
``schemas/market_open_run_classification.schema.json``):

    - **Live Robinhood-only execution** (cron ``2ba19526``, 9:35 AM ET weekdays,
      ``run_type='market_open_dca_live'``, ``trades=true``) on account
      ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (supplied by the environment, never
      committed); Alpaca/Finance/Yahoo stay read-only.
    - Uses the **shared 0x -> 5x sizing ladder** and the dynamic $1/$2 base
      every live stream uses, the same active universe (excluding the
      whole-share tickers BYDDY and RVII), and the same shared $180/$900
      Normal-tier ceiling.
    - **Trend validation is exempted for this slot only** (user-confirmed
      2026-07-27, ``config/scheduled_tasks.json:2ba19526.trend_validation_exemption``):
      at 9:35 only five minutes of regular-session data exist, so a completed
      60-minute intraday downtrend can never validate and requiring it would zero
      the slot. Every other circuit breaker stays fully enforced.
    - **First buy of the day.** It normally finds no prior same-day allocation
      and places the full desired amount. The inherited top-up guard still runs so
      a hand-placed manual recovery fill earlier the same day is honoured.
    - **Independent** of every other stream: ``stream='market_open'``,
      ``valid_morning_evidence=false``, ``valid_afternoon_evidence=false``. Its
      fills are prior-allocation context for every later same-day slot and are
      never those streams' scheduled evidence.

Configured with ``live_execution_enabled=true`` in its config (promoted to live
following full shadow-versus-live parity validation against external execution).

Writes ONLY under its own run-state root
``<cron_tracking_root>/market_open_dca/YYYYMMDD_0935ET/`` and never touches the
morning, midday, intraday, or afternoon artifact trees.

Inherited behavior this slot gets for free (documented 2026-08-03; previously
undocumented here and absent from ``config/market_open_dca.json``, so a reader
of either file alone would not know these apply):

* **Raw connector-input persistence.** ``MorningDCAExecutor._run_impl()`` gates
  raw persistence on ``mode == MORNING_LIVE_MODE and trades and not manual`` --
  which is stream-agnostic and therefore TRUE for this slot. Every quote/bar/
  broker response is written under this run's ``raw/`` via
  ``_PersistingMarketDataProvider``/``_PersistingBrokerAdapter`` BEFORE any
  signal, budget, or top-up computation, and a persistence failure fails closed
  through ``_blocked_live_result(error_class='missing_or_invalid_raw_inputs')``
  before any order could be placed. ``config/market_open_dca.json`` has no
  ``raw_data_persistence`` block (unlike ``config/morning_dca.json``) and omits
  these files from its ``state_file_guarantee.always_write`` list, but they are
  written regardless.
* **Post-artifact drift detection.** The inherited ``_run_drift_check()`` call
  fires for this slot too, reconciling signals against submitted orders. This
  module contains no literal ``drift_detection`` reference precisely because it
  does not override ``_run_impl``.
* **``budget_ledger.json``.** ``writes_budget_ledger = True`` below causes
  ``write_artifacts()`` to emit it on every live run; it is likewise missing
  from the config's ``always_write`` list.

Because all three arrive by NOT overriding ``_run_impl``/``write_artifacts``,
overriding either method here without re-invoking ``super()`` would silently
drop raw persistence, the fail-closed guard, and drift detection at once.
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


# --- Market-open stream identity / classification constants ------------------
MARKET_OPEN_STREAM = "market_open"

# Reuses the literal "live" mode key, exactly as the midday stream does, so every
# inherited `self.mode == MORNING_LIVE_MODE` check in MorningDCAExecutor treats
# this as a live run without needing to be generalized.
MARKET_OPEN_LIVE_MODE = "live"
MARKET_OPEN_LIVE_CRON_ID = "2ba19526"
MARKET_OPEN_LIVE_RUN_TYPE = "market_open_dca_live"
MARKET_OPEN_LIVE_SCHEDULED_FOR_ET = "09:35"
# Opens AT the 9:35 decision time, never before it (the decision reads the completed 9:30 bar).
MARKET_OPEN_LIVE_WINDOW_START_ET = "09:35"
MARKET_OPEN_LIVE_WINDOW_END_ET = "09:50"
MARKET_OPEN_LIVE_SCHEMA_VERSION = "market-open-live-v1"

# Artifact root: a sibling of, and never nested under, any other stream's root.
DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT = stream_root("market_open_dca")

MARKET_OPEN_PROFILES: Dict[str, Dict[str, Any]] = {
    MARKET_OPEN_LIVE_MODE: {
        "mode": MARKET_OPEN_LIVE_MODE,
        "artifact_mode": "live",
        "trades": True,
        "cron_id": MARKET_OPEN_LIVE_CRON_ID,
        "run_type": MARKET_OPEN_LIVE_RUN_TYPE,
        "scheduled_for_et": MARKET_OPEN_LIVE_SCHEDULED_FOR_ET,
        "window_start_et": MARKET_OPEN_LIVE_WINDOW_START_ET,
        "window_end_et": MARKET_OPEN_LIVE_WINDOW_END_ET,
        "state_schema_version": MARKET_OPEN_LIVE_SCHEMA_VERSION,
        "state_root": DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT,
        "state_root_stream": "market_open_dca",
        "folder_suffix": "0935ET",
        "scheduled_run_id_prefix": "market_open_live",
        "manual_run_id_prefix": "manual_market_open_live",
        "decision_time_et": time(9, 35),
        # Exempt: five minutes of session data cannot validate a completed
        # 60-minute downtrend. See the module docstring.
        "validate_trend": False,
    },
}

# The same shared 0x -> 5x ladder every live stream uses (config/sizing_ladder.json).
MARKET_OPEN_SIZING_TIERS: Tuple[Tuple[float, float], ...] = StrategyConfig().sizing_tiers
MARKET_OPEN_MINIMUM_DROP = StrategyConfig().minimum_drop_vs_previous_close

# The shared Normal-tier ceiling every live stream draws from
# (near_close_dca.BUDGET_TIERS[0], mirrored in config/budgets.json and
# config/market_open_dca.json's budget_ledger block) -- not an isolated
# per-stream share. Like morning and midday, this slot never independently
# escalates to Elevated/High-volatility/Stress; only the 3:30 final run does,
# from its fuller same-day view.
DEFAULT_MARKET_OPEN_DAILY_CAP_USD = 180.0
DEFAULT_MARKET_OPEN_WEEKLY_CAP_USD = 900.0


def default_market_open_prior_allocation_roots() -> tuple[str, ...]:
    """Roots scanned for same-day allocations that could precede 9:35 AM ET.

    Deliberately narrower than the later streams' lists: nothing scheduled runs
    before the market-open slot, so the only possible same-day prior allocation
    is a hand-placed manual fill. ``market_open_dca`` (this stream's own root)
    is included because ``MarketOpenDCAExecutor.run(..., manual=True)`` writes
    a successful manual recovery fill under its own
    ``market_open_dca/manual_runs/YYYYMMDD_*`` tree -- omitting it left a
    recovery order placed shortly before 9:35 invisible to the scheduled
    invocation's own top-up guard, so a lower-price scheduled signal saw no
    prior allocation and the broad duplicate check skipped the ticker outright
    instead of submitting the intended incremental top-up (Codex review:
    "Scan market-open manual runs as prior allocations"), matching how
    ``morning_dca``'s own default roots include its own root the same way.
    ``morning_dca`` is included because ``load_prior_same_day_allocations``
    recurses and so catches ``morning_dca/manual_recovery/YYYYMMDD_*``;
    ``final_manual_review`` catches the out-of-tree manual final-buy override
    root. All three contribute nothing on a normal day.

    A function, not a module-level tuple: a literal would bind
    ``$DCA_CRON_TRACKING_ROOT`` at import time, leaving any instance built after
    the variable is set scanning the old tree (Codex review, PR #219).
    """
    return (
        stream_root("market_open_dca"),
        stream_root("morning_dca"),
        stream_root("final_manual_review"),
    )


def default_market_open_cap_seed_roots() -> tuple[str, ...]:
    """Sibling roots whose same-day/week spend counts against the shared cap.

    Every live stream including this one, so a restart or retry re-seeds from
    this stream's own prior fills. Unlike the prior-allocation roots above (which
    feed only the per-ticker top-up guard's reference price), these feed the
    shared daily/weekly ceiling.
    """
    return (
        stream_root("market_open_dca"),
        stream_root("morning_dca"),
        stream_root("midday_dca"),
        stream_root("intraday_dca"),
        stream_root("ce16ba6b"),
        stream_root("final_manual_review"),
    )


@dataclass(frozen=True)
class MarketOpenConfig(MorningConfig):
    """Configuration for the market-open live-execution stream.

    Subclasses :class:`MorningConfig` so it reuses every field the shared
    executor reads, overriding only the ladder/cap defaults and the root lists.
    """

    sizing_tiers: Tuple[Tuple[float, float], ...] = MARKET_OPEN_SIZING_TIERS
    minimum_drop_vs_previous_close: float = MARKET_OPEN_MINIMUM_DROP
    morning_daily_cap_usd: float = DEFAULT_MARKET_OPEN_DAILY_CAP_USD
    morning_weekly_cap_usd: float = DEFAULT_MARKET_OPEN_WEEKLY_CAP_USD
    prior_allocation_roots: Tuple[str, ...] = field(
        default_factory=default_market_open_prior_allocation_roots
    )
    cap_seed_roots: Tuple[str, ...] = field(default_factory=default_market_open_cap_seed_roots)


def load_market_open_config(config_dir=None, *, allow_unpinned_account: bool = False):
    """Read ``config/market_open_dca.json`` and return a :class:`MarketOpenConfig`."""
    return load_stream_config(
        config_dir,
        filename="market_open_dca.json",
        config_cls=MarketOpenConfig,
        daily_cap_key="market_open_daily_cap_usd",
        weekly_cap_key="market_open_weekly_cap_usd",
        source_label="market-open",
        allow_unpinned_account=allow_unpinned_account,
    )


class MarketOpenDCAExecutor(MorningDCAExecutor):
    """Market-open live graded-DCA executor (9:35 AM ET Robinhood execution).

    Subclasses :class:`MorningDCAExecutor` to reuse the entire
    evaluate/build/write pipeline under the market-open stream identity. It needs
    no top-up hook overrides: as the day's first buy it normally has no prior
    same-day allocation, and the inherited guard already handles the manual
    recovery case correctly.
    """

    stream = MARKET_OPEN_STREAM
    PROFILES: Dict[str, Dict[str, Any]] = MARKET_OPEN_PROFILES
    ledger_namespace: str = MARKET_OPEN_STREAM
    ladder_name: str = "market_open_0x_to_5x"
    signals_csv_name: str = "market_open_signals.csv"
    latest_pointer_name: str = "latest_market_open_run.json"
    budget_daily_key: str = "market_open_daily_cap_usd"
    budget_weekly_key: str = "market_open_weekly_cap_usd"
    budget_label: str = "market_open"
    manual_run_subdir: str = "manual_runs"
    # manual_runs/ here holds reruns of this same scheduled task, so they count
    # against this stream's own cap ledger -- matching morning, not midday
    # (whose manual_reruns/ folder holds post-slot backfill artifacts documented
    # as feeding only the afternoon guard).
    scan_manual_reruns_for_ledger: bool = True
    # config/scheduled_tasks.json:2ba19526.budget_ledger defines a ledger_dir and
    # ledger_filename with a bootstrap note authorising creation, so this stream
    # persists the snapshot the same way morning does.
    writes_budget_ledger: bool = True

    def __init__(
        self,
        config: Optional[MarketOpenConfig] = None,
        ledger=None,
        *,
        mode: str = MARKET_OPEN_LIVE_MODE,
        config_dir: Optional[str] = None,
    ) -> None:
        super().__init__(config, ledger, mode=mode, config_dir=config_dir)

    def _load_default_config(self, config_dir: Optional[str], mode: str) -> MarketOpenConfig:
        # Live-only stream: tolerate an unresolvable account so the run reaches
        # its fail-closed path and leaves blocked evidence, rather than raising
        # before an executor exists and leaving the slot with no artifact at all.
        return load_market_open_config(config_dir, allow_unpinned_account=True)


__all__ = [
    "DEFAULT_MARKET_OPEN_DAILY_CAP_USD",
    "DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT",
    "DEFAULT_MARKET_OPEN_WEEKLY_CAP_USD",
    "MARKET_OPEN_LIVE_CRON_ID",
    "MARKET_OPEN_LIVE_MODE",
    "MARKET_OPEN_LIVE_RUN_TYPE",
    "MARKET_OPEN_LIVE_SCHEMA_VERSION",
    "MARKET_OPEN_PROFILES",
    "MARKET_OPEN_SIZING_TIERS",
    "MARKET_OPEN_STREAM",
    "MarketOpenConfig",
    "MarketOpenDCAExecutor",
    "default_market_open_cap_seed_roots",
    "default_market_open_prior_allocation_roots",
    "load_market_open_config",
]
