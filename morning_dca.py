"""Morning graded-DCA dry-run executor.

This module adds a *morning* analytics stream on top of the existing near-close
strategy library (:mod:`near_close_dca`). It is deliberately isolated from the
afternoon scheduled live task (cron ``10ce362c``, which supersedes the deleted
``ce16ba6b``):

    - It uses its OWN sizing ladder (a coarser 0x -> 3x morning ladder) rather
      than the afternoon 0x -> 5x ladder.
    - It keeps its OWN budget ledger namespace (:class:`MorningLedger`) so morning
      dry-run / future morning buys can never consume the afternoon 3:20/3:45
      budget ledger.
    - It writes artifacts under its OWN run-state root
      (``/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_0945ET/``) and
      NEVER touches ``.../ce16ba6b/latest_scheduled_run.json`` or the afternoon
      ``scheduled_runs/`` folders.
    - Its run_classification carries ``run_type='morning_dca_dry_run'`` and a
      distinct ``stream='morning'`` marker so downstream afternoon reporting (which
      only globs ``.../ce16ba6b/scheduled_runs/**``) never mistakes it for
      afternoon scheduled evidence.

The morning stream is DRY-RUN / ANALYTICS ONLY. It computes hypothetical dollar
amounts and writes signal artifacts. It NEVER places orders, never runs a
Robinhood order review, and never calls any broker placement path. Robinhood
account 562057042 remains the only execution broker for the afternoon live task;
Alpaca stays read-only.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from near_close_dca import (
    EASTERN,
    Action,
    Bar,
    StrategyConfig,
    graded_buy_multiplier,
    iso_week_key,
    latest_bar_at_or_before,
)

# --- Morning stream identity / classification constants ---------------------
#
# The morning stream is a NEW, independent scheduled analytics task. It does not
# supersede the afternoon live task and is never valid afternoon evidence. These
# constants intentionally differ from the afternoon 15:20 classification so the
# two streams can never be confused (see docs/morning_dca.md).
MORNING_STREAM = "morning"
MORNING_CRON_ID = "morning-dca-0945"
MORNING_RUN_TYPE = "morning_dca_dry_run"
MORNING_SCHEDULED_FOR_ET = "09:45"
MORNING_WINDOW_START_ET = "09:40"
MORNING_WINDOW_END_ET = "09:50"
STATE_SCHEMA_VERSION = "run-classification-v1"

# Default artifact root for the morning stream. Deliberately a sibling of, and
# never nested under, the afternoon ce16ba6b root.
DEFAULT_MORNING_STATE_ROOT = "/home/user/workspace/cron_tracking/morning_dca"

# Morning sizing ladder: a coarser 0x -> 3x ladder keyed on the drop vs.
# *yesterday's regular-session close*. Thresholds are fractions of the previous
# close (0.02 == 2.0%). The largest threshold <= the actual drop wins; a drop
# below the first threshold yields no buy (0x). This is SEPARATE from the
# afternoon 0x -> 5x ladder in StrategyConfig.sizing_tiers.
#     drop < 0.1%           -> 0x  (no buy)
#     0.1% <= drop < 2.0%   -> 1x
#     2.0% <= drop < 5.0%   -> 1.5x
#     5.0% <= drop < 8.0%   -> 2x
#     drop >= 8.0%          -> 3x  (no upper cutoff)
MORNING_SIZING_TIERS: Tuple[Tuple[float, float], ...] = (
    (0.001, 1.0),
    (0.020, 1.5),
    (0.050, 2.0),
    (0.080, 3.0),
)

MORNING_MINIMUM_DROP = 0.001

# Morning budget caps. The morning stream owns these; they are intentionally not
# the afternoon BUDGET_TIERS caps so morning activity is budgeted independently.
DEFAULT_MORNING_DAILY_CAP_USD = 90.0
DEFAULT_MORNING_WEEKLY_CAP_USD = 450.0


@dataclass(frozen=True)
class MorningConfig:
    """Configuration hooks for the morning dry-run stream.

    Kept separate from :class:`~near_close_dca.StrategyConfig` so the morning
    ladder, caps, decision anchor, and run-state root can never leak into the
    afternoon scheduled path. ``base_buy_amount_usd`` matches the afternoon $3
    base by default so the hypothetical dollar figures are comparable.
    """

    tickers: Sequence[str] = ()
    excluded_tickers: frozenset[str] = frozenset()
    base_buy_amount_usd: float = 3.0
    sizing_tiers: Tuple[Tuple[float, float], ...] = MORNING_SIZING_TIERS
    minimum_drop_vs_previous_close: float = MORNING_MINIMUM_DROP
    decision_time_et: time = time(9, 45)
    interval_minutes: int = 5
    morning_daily_cap_usd: float = DEFAULT_MORNING_DAILY_CAP_USD
    morning_weekly_cap_usd: float = DEFAULT_MORNING_WEEKLY_CAP_USD
    morning_run_state_root: str = DEFAULT_MORNING_STATE_ROOT

    def as_strategy_config(self) -> StrategyConfig:
        """Return a StrategyConfig view carrying the morning ladder.

        Only the fields the shared sizing math reads (``sizing_tiers``,
        ``minimum_drop_vs_previous_close``, ``base_buy_amount_usd``) are set; the
        result is used solely to drive :func:`graded_buy_multiplier` and never to
        run the afternoon live path. ``dry_run`` stays True.
        """
        return StrategyConfig(
            base_buy_amount_usd=self.base_buy_amount_usd,
            sizing_tiers=tuple(self.sizing_tiers),
            minimum_drop_vs_previous_close=self.minimum_drop_vs_previous_close,
            dry_run=True,
        )


@dataclass
class MorningLedger:
    """A budget ledger namespaced to the morning stream.

    This is a self-contained session that tracks only *hypothetical* morning
    dry-run notional (and, if the morning stream ever executes in the future, its
    real notional). It shares no state with the afternoon
    :class:`~near_close_dca.StrategyState`, so morning activity can never consume
    or validate the afternoon 3:20/3:45 daily/weekly budget ledger.
    """

    namespace: str = MORNING_STREAM
    daily_cap_usd: float = DEFAULT_MORNING_DAILY_CAP_USD
    weekly_cap_usd: float = DEFAULT_MORNING_WEEKLY_CAP_USD
    planned_by_day: Dict[date, float] = field(default_factory=dict)
    planned_by_week: Dict[Tuple[int, int], float] = field(default_factory=dict)

    def record_hypothetical(self, trading_day: date, amount_usd: float) -> None:
        if amount_usd <= 0:
            return
        self.planned_by_day[trading_day] = self.day_planned(trading_day) + amount_usd
        week = iso_week_key(trading_day)
        self.planned_by_week[week] = self.planned_by_week.get(week, 0.0) + amount_usd

    def day_planned(self, trading_day: date) -> float:
        return self.planned_by_day.get(trading_day, 0.0)

    def week_planned(self, trading_day: date) -> float:
        return self.planned_by_week.get(iso_week_key(trading_day), 0.0)

    def remaining_daily(self, trading_day: date) -> float:
        return max(0.0, self.daily_cap_usd - self.day_planned(trading_day))

    def remaining_weekly(self, trading_day: date) -> float:
        return max(0.0, self.weekly_cap_usd - self.week_planned(trading_day))

    def over_daily_cap(self, trading_day: date) -> bool:
        return self.day_planned(trading_day) > self.daily_cap_usd

    def over_weekly_cap(self, trading_day: date) -> bool:
        return self.week_planned(trading_day) > self.weekly_cap_usd


@dataclass(frozen=True)
class MorningSignal:
    """One morning dry-run signal row for a single ticker."""

    ticker: str
    source: str
    action: Action
    reason: str
    current_price: Optional[float]
    previous_close: Optional[float]
    drop_pct: Optional[float]
    multiplier: float
    hypothetical_amount_usd: float
    warnings: Tuple[str, ...] = ()

    def to_row(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "source": self.source,
            "action": self.action.value,
            "reason": self.reason,
            "current_price": self.current_price,
            "previous_close": self.previous_close,
            "drop_pct": self.drop_pct,
            "multiplier": self.multiplier,
            "hypothetical_amount_usd": self.hypothetical_amount_usd,
            "warnings": "; ".join(self.warnings),
        }


class DCAExecutor:
    """Base class for class-based DCA executors over the strategy library.

    The historical afternoon path is functional (``near_close_dca.run_once`` /
    ``run_daily_compact``) and remains the authoritative live path. This base
    introduces the shared extension points a class-based executor needs — the
    sizing ladder, a run-state root, and a run-classification block — so new
    streams (starting with the morning dry-run) can plug in without touching the
    afternoon code.

    Subclasses provide the concrete ladder via :attr:`sizing_config`, the artifact
    root via :meth:`run_state_dir`, and the classification via
    :meth:`run_classification`.
    """

    stream: str = "base"
    run_type: str = "dca"

    def multiplier_for_drop(self, drop_fraction: float) -> float:
        """Map a drop (fraction of previous close) to a buy multiplier."""
        return graded_buy_multiplier(drop_fraction, self.sizing_config)

    @property
    def sizing_config(self) -> StrategyConfig:  # pragma: no cover - abstract
        raise NotImplementedError

    def run_state_dir(self, now: datetime) -> Path:  # pragma: no cover - abstract
        raise NotImplementedError

    def run_classification(self, now: datetime) -> Dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError


class MorningDCAExecutor(DCAExecutor):
    """Morning dry-run executor: drop-vs-yesterday's-close analytics only.

    Evaluates each ticker's morning price weakness relative to yesterday's
    regular-session close, grades it on the morning 0x -> 3x ladder, and writes
    dry-run artifacts under the morning run-state root. It NEVER places orders,
    reviews orders, or contacts a broker, and it NEVER writes into the afternoon
    ce16ba6b artifact tree.
    """

    stream = MORNING_STREAM
    run_type = MORNING_RUN_TYPE

    def __init__(
        self,
        config: Optional[MorningConfig] = None,
        ledger: Optional[MorningLedger] = None,
    ) -> None:
        self.config = config or MorningConfig()
        self.ledger = ledger or MorningLedger(
            daily_cap_usd=self.config.morning_daily_cap_usd,
            weekly_cap_usd=self.config.morning_weekly_cap_usd,
        )
        self._sizing_config = self.config.as_strategy_config()

    # -- DCAExecutor hooks ---------------------------------------------------
    @property
    def sizing_config(self) -> StrategyConfig:
        return self._sizing_config

    def run_state_dir(self, now: datetime) -> Path:
        now_et = now.astimezone(EASTERN)
        hhmm = MORNING_SCHEDULED_FOR_ET.replace(":", "")
        folder = f"{now_et.strftime('%Y%m%d')}_{hhmm}ET"
        return Path(self.config.morning_run_state_root) / folder
    def run_id(self, now: datetime, *, manual: bool) -> str:
        now_et = now.astimezone(EASTERN)
        if manual:
            now_utc = now.astimezone(timezone.utc)
            return f"manual_morning_{now_utc.strftime('%Y%m%d_%H%M%S')}Z"
        return f"morning_{now_et.strftime('%Y%m%d')}_0945ET"

    def run_classification(self, now: datetime, *, manual: bool = False) -> Dict[str, Any]:
        """Build the morning run-classification block.

        A real scheduled morning dry run is ``run_origin='scheduled'`` with
        ``run_type='morning_dca_dry_run'`` and ``manual_run=false``. The
        ``stream='morning'`` marker and ``valid_afternoon_evidence=false`` flag
        make it unambiguous that this artifact is NOT afternoon scheduled evidence.
        """
        triggered_iso = now.astimezone(timezone.utc).isoformat()
        classification: Dict[str, Any] = {
            "run_origin": "manual" if manual else "scheduled",
            "run_type": self.run_type,
            "stream": self.stream,
            "cron_id": MORNING_CRON_ID,
            "supersedes_cron_id": None,
            "scheduled_for_et": None if manual else MORNING_SCHEDULED_FOR_ET,
            "scheduled_window_start_et": None if manual else MORNING_WINDOW_START_ET,
            "scheduled_window_end_et": None if manual else MORNING_WINDOW_END_ET,
            "manual_run": manual,
            "run_id": self.run_id(now, manual=manual),
            "triggered_at_utc": triggered_iso,
            "state_schema_version": STATE_SCHEMA_VERSION,
            # Explicit, machine-checkable separation from the afternoon stream.
            "valid_afternoon_evidence": False,
            "afternoon_live_cron_id": "10ce362c",
        }
        return classification

    # -- Evaluation ----------------------------------------------------------
    def evaluate_signal(self, ticker: str, now: datetime, data: Any) -> MorningSignal:
        """Grade one ticker's morning weakness vs. yesterday's close (dry-run).

        Uses the provider's previous-close bar and the latest completed intraday
        bar (or a live quote via ``get_quote`` when available) at/or before the
        9:45 AM ET decision anchor. Never places or reviews orders.
        """
        source = getattr(getattr(data, "source", None), "value", "unknown")
        now_et = now.astimezone(EASTERN)
        trading_day = now_et.date()
        decision_dt = datetime.combine(trading_day, self.config.decision_time_et, tzinfo=EASTERN)

        warnings: List[str] = []
        previous_close: Optional[float] = None
        current_price: Optional[float] = None

        try:
            prev_bar: Optional[Bar] = data.get_previous_close_bar(ticker, decision_dt)
        except Exception as exc:  # noqa: BLE001 - degrade to NO_DATA with a warning
            prev_bar = None
            warnings.append(f"previous_close provider error: {exc}")
        if prev_bar is not None:
            previous_close = prev_bar.close
        else:
            warnings.append("missing previous close")

        try:
            bars = data.get_intraday_bars(
                ticker=ticker,
                start=datetime.combine(trading_day, time(9, 30), tzinfo=EASTERN),
                end=decision_dt,
                interval_minutes=self.config.interval_minutes,
            )
        except Exception as exc:  # noqa: BLE001
            bars = []
            warnings.append(f"intraday provider error: {exc}")

        current_price = self._current_price(data, ticker, decision_dt, bars, warnings)

        if previous_close is None or not previous_close or current_price is None:
            return MorningSignal(
                ticker=ticker,
                source=source,
                action=Action.NO_DATA,
                reason="missing previous close or current price",
                current_price=current_price,
                previous_close=previous_close,
                drop_pct=None,
                multiplier=0.0,
                hypothetical_amount_usd=0.0,
                warnings=tuple(warnings),
            )

        drop_fraction = (previous_close - current_price) / previous_close
        drop_pct = drop_fraction * 100.0
        multiplier = self.multiplier_for_drop(drop_fraction)
        hypothetical = round(self.config.base_buy_amount_usd * multiplier, 2)

        if drop_fraction < self.config.minimum_drop_vs_previous_close:
            action = Action.SKIP if current_price >= previous_close else Action.NO_ACTION
            reason = (
                "above/at prior close" if current_price >= previous_close
                else "below prior close but under 0.1% minimum drop"
            )
            multiplier = 0.0
            hypothetical = 0.0
        else:
            action = Action.BUY
            reason = f"morning weakness {drop_pct:.2f}% vs prior close -> {multiplier:g}x (dry-run)"

        return MorningSignal(
            ticker=ticker,
            source=source,
            action=action,
            reason=reason,
            current_price=current_price,
            previous_close=previous_close,
            drop_pct=drop_pct,
            multiplier=multiplier,
            hypothetical_amount_usd=hypothetical,
            warnings=tuple(warnings),
        )

    def _current_price(
        self,
        data: Any,
        ticker: str,
        decision_dt: datetime,
        bars: Sequence[Bar],
        warnings: List[str],
    ) -> Optional[float]:
        get_quote = getattr(data, "get_quote", None)
        if callable(get_quote):
            try:
                quote = get_quote(ticker, decision_dt)
                if quote is not None and isinstance(quote, (tuple, list)) and len(quote) >= 1:
                    return float(quote[0])
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"quote provider error: {exc}")
        from datetime import timedelta

        decision_anchor = (
            decision_dt - timedelta(minutes=self.config.interval_minutes)
            if self.sizing_config.assume_bar_timestamps_are_start_times
            else decision_dt
        )
        bar = latest_bar_at_or_before(bars, decision_anchor)
        if bar is None:
            warnings.append(
                f"missing intraday bar at/or before {decision_anchor.astimezone(EASTERN).strftime('%H:%M')} ET"
            )
            return None
        return bar.close

    def evaluate(self, now: datetime, data: Any) -> List[MorningSignal]:
        """Evaluate every non-excluded ticker for the morning stream."""
        tickers = [t for t in self.config.tickers if t not in self.config.excluded_tickers]
        return [self.evaluate_signal(t, now, data) for t in tickers]

    # -- Artifacts -----------------------------------------------------------
    def build_artifacts(
        self,
        now: datetime,
        signals: Sequence[MorningSignal],
        data_source: str,
        *,
        manual: bool = False,
    ) -> Dict[str, Any]:
        """Assemble the morning run_state dict (dry-run / analytics only)."""
        now_et = now.astimezone(EASTERN)
        trading_day = now_et.date()

        signal_rows = [s.to_row() for s in signals]
        buy_candidates = [s for s in signals if s.action == Action.BUY and s.multiplier > 0]
        for candidate in buy_candidates:
            self.ledger.record_hypothetical(trading_day, candidate.hypothetical_amount_usd)

        hypothetical_spend = round(sum(c.hypothetical_amount_usd for c in buy_candidates), 2)

        warnings: List[str] = []
        for sig in signals:
            warnings.extend(f"{sig.ticker}: {w}" for w in sig.warnings)
        if self.ledger.over_daily_cap(trading_day):
            warnings.append(
                f"hypothetical morning day spend {self.ledger.day_planned(trading_day):.2f} exceeds morning daily cap "
                f"{self.ledger.daily_cap_usd:.2f}"
            )
        if self.ledger.over_weekly_cap(trading_day):
            warnings.append(
                f"hypothetical morning week spend {self.ledger.week_planned(trading_day):.2f} exceeds "
                f"morning weekly cap {self.ledger.weekly_cap_usd:.2f}"
            )

        classification = self.run_classification(now, manual=manual)

        return {
            "run_id": classification["run_id"],
            "stream": self.stream,
            "mode": "dry_run",
            "trades": False,
            "generated_at": now_et.isoformat(),
            "trading_day": trading_day.isoformat(),
            "iso_week": list(iso_week_key(trading_day)),
            "data_source": data_source,
            "ladder": {
                "name": "morning_0x_to_3x",
                "base_amount_usd": self.config.base_buy_amount_usd,
                "tiers": [list(t) for t in self.config.sizing_tiers],
                "no_buy_below_pct": self.config.minimum_drop_vs_previous_close * 100.0,
            },
            "budget": {
                "ledger_namespace": self.ledger.namespace,
                "morning_daily_cap_usd": self.ledger.daily_cap_usd,
                "morning_weekly_cap_usd": self.ledger.weekly_cap_usd,
                "hypothetical_planned_spend_usd": hypothetical_spend,
                "hypothetical_day_spend_usd": round(self.ledger.day_planned(trading_day), 2),
                "hypothetical_week_spend_usd": round(self.ledger.week_planned(trading_day), 2),
                "remaining_daily_usd": round(self.ledger.remaining_daily(trading_day), 2),
                "remaining_weekly_usd": round(self.ledger.remaining_weekly(trading_day), 2),
            },
            "counts": {
                "evaluated": len(signals),
                "buy_candidates": len(buy_candidates),
                "no_data": sum(1 for s in signals if s.action == Action.NO_DATA),
            },
            "run_classification": classification,
            "signal_rows": signal_rows,
            "buy_candidates": [c.to_row() for c in buy_candidates],
            "warnings": warnings,
        }

    def write_artifacts(self, artifacts: Dict[str, Any], now: datetime) -> Dict[str, str]:
        """Write morning run_state.json (+ CSV) under the morning run-state root.

        Writes ONLY under the morning root and, for scheduled runs, updates the
        morning-only ``latest_morning_run.json`` pointer. It never writes into the
        afternoon ce16ba6b tree or touches ``latest_scheduled_run.json``.
        """
        manual_run = artifacts["run_classification"]["manual_run"]
        out = (
            Path(self.config.morning_run_state_root) / "manual_runs" / artifacts["run_id"]
            if manual_run
            else self.run_state_dir(now)
        )
        out.mkdir(parents=True, exist_ok=True)

        run_state_path = out / "run_state.json"
        run_state_path.write_text(json.dumps(artifacts, indent=2, sort_keys=True), encoding="utf-8")

        signals_path = out / "morning_signals.csv"
        _write_csv(signals_path, artifacts["signal_rows"])

        paths = {"run_state": str(run_state_path), "signals": str(signals_path)}

        if not artifacts["run_classification"]["manual_run"]:
            pointer = Path(self.config.morning_run_state_root) / "latest_morning_run.json"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(
                json.dumps(
                    {
                        "run_id": artifacts["run_id"],
                        "run_state_path": str(run_state_path),
                        "trading_day": artifacts["trading_day"],
                        "stream": self.stream,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            paths["latest_morning_run"] = str(pointer)

        return paths

    def run(
        self,
        now: datetime,
        data: Any,
        *,
        data_source: Optional[str] = None,
        manual: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate the morning stream and write dry-run artifacts.

        Returns ``{"artifacts": <run_state dict>, "paths": <written paths>}``. No
        orders are placed and no broker is contacted.
        """
        signals = self.evaluate(now, data)
        source = data_source or getattr(getattr(data, "source", None), "value", "unknown")
        artifacts = self.build_artifacts(now, signals, source, manual=manual)
        paths = self.write_artifacts(artifacts, now)
        return {"artifacts": artifacts, "paths": paths}


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
