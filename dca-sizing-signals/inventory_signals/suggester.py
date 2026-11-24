"""InventoryReservationSuggester: a shrink-only, fail-closed, unscored SizingSuggester.

Spec: docs/inventory_reservation_suggester.md. It:

  * structurally satisfies dca_shared_interfaces.forecasting.SizingSuggester (one public
    ``suggest(ticker, asof)``; no write, no apply, no live-engine parameter anywhere);
  * gets every input from an injected ``load_inputs(asof)`` callable -- committed cost
    basis, volatility forecasts, the intraday seasonal profile and reference prices all
    come from the caller, so this package makes no network, broker or yfinance call;
  * is bound to ONE slot at construction (one instance per stream, like the executor
    classes), and refuses inputs captured for a different slot or day, or captured after
    the clock says "now" (a look-ahead in a backtest replay);
  * fails closed exactly like comovement_signals: ``evaluate()`` returns ``ok=False`` with
    a reason and no suggestions on any missing, stale or malformed input, ``suggest()``
    raises InventoryUnavailable, and publishing a failed outcome writes nothing. With no
    file, SizingAdjustmentReader falls back to the unadjusted tier -- the same effect the
    research note's "return neutral" asked for, without inventing a number.

The multiplier is never above 1.0. The threshold output (``max_buy_price``) has no slot in
SizingSuggestion, so it rides on the InventorySuggestion subclass and in the published
file only; nothing in the live paths reads it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from dca_shared_interfaces.forecasting import SizingSuggestion
from inventory_signals.reservation import (
    DEFAULT_B0_BPS,
    DEFAULT_CAP_SIGMAS,
    DEFAULT_GAMMA_M,
    DEFAULT_KAPPA,
    DEFAULT_M_MIN,
    DEFAULT_Q_MAX,
    DEFAULT_Z,
    SLOTS,
    finite,
    inventory_excess,
    max_buy_price,
    median,
    normalized_profile,
    remaining_weight,
    reservation_discount,
    size_multiplier,
)

SOURCE = "inventory_signals.inventory_reservation"
DEFAULT_MODEL_VERSION = "inventory-reservation-v1"

# Same reasoning as comovement_signals.UNSCORED_MAE (review round 1 on PR #2106): 0.0 would
# read as a PERFECT MAE to any MAE-ranking reader. +inf can never win a lower-is-better
# comparison and the shared SuggestionPublisher refuses it outright as non-finite.
UNSCORED_MAE = math.inf

# Tolerance for "target weights sum to 1" -- generous enough for JSON round trips of a
# 100+ name equal-weight vector, tight enough to catch a missing or doubled name.
_WEIGHT_SUM_TOLERANCE = 1e-6

try:  # zoneinfo is stdlib; tzdata can still be missing on a bare container.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    _ET: object | None = ZoneInfo("America/New_York")
except (ImportError, ZoneInfoNotFoundError):  # pragma: no cover - environment dependent
    _ET = None


@dataclass(frozen=True, slots=True)
class InventorySuggestion(SizingSuggestion):
    """A SizingSuggestion that says, by type, that nobody has scored it yet, plus the
    reservation detail SizingSuggestion has no slot for."""

    scored: bool = False
    slot: str = ""
    q: float = 0.0
    discount: float = 0.0
    remaining_variance: float = 0.0
    max_buy_price: float | None = None


class InventoryUnavailable(RuntimeError):
    """No suggestion can be made. ``reason`` is the fail-closed reason code."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


@dataclass(frozen=True)
class InventoryReservationParams:
    """Every knob, defaulting to the research note's starting priors (not calibrated).

    ``max_input_age_minutes``: inputs captured longer ago than this, by the injected clock,
    are stale. 60 covers a slot that starts late; anything older belongs to an earlier slot.
    """

    z: float = DEFAULT_Z
    q_max: float = DEFAULT_Q_MAX
    gamma_m: float = DEFAULT_GAMMA_M
    m_min: float = DEFAULT_M_MIN
    b0_bps: float = DEFAULT_B0_BPS
    kappa: float = DEFAULT_KAPPA
    cap_sigmas: float = DEFAULT_CAP_SIGMAS
    max_input_age_minutes: float = 60.0


@dataclass(frozen=True)
class InventoryInputs:
    """Everything one evaluation reads, supplied by the caller, point-in-time.

    Attributes:
        as_of: the trading day.
        slot: the slot these inputs were captured for; must equal the suggester's slot.
        known_at: tz-aware time the snapshot was taken. Must fall on ``as_of`` (ET), not
            after the clock, no more than ``max_input_age_minutes`` old, and no more than
            5 minutes before the slot's scheduled time -- an earlier slot's snapshot is
            refused.
        committed_usd_by_ticker: cumulative COST-BASIS dollars committed per ticker, for the
            whole universe (zero for a name never bought). The total is the denominator of
            every target.
        candidate_tickers: the names this slot proposes to buy; one suggestion each.
        daily_vol_by_ticker: forecast daily log-return standard deviation per ticker, for
            the universe (the median is taken over these). Volatility only, never direction.
        slot_realized_vol_by_ticker: realized log-return std-dev of the last slot interval
            per candidate, from live bars only.
        seasonal_profile: per-slot share of daily variance (see reservation.normalized_profile),
            estimated on a window ending the prior day.
        target_weight_by_ticker: optional target share of committed capital per ticker,
            summing to 1 over the universe. None means equal weight over
            ``committed_usd_by_ticker``'s keys.
        reference_price_by_ticker: optional earlier same-day reference price per candidate.
            A candidate without one (the day's first buy) gets ``max_buy_price`` None.
    """

    as_of: date
    slot: str
    known_at: datetime
    committed_usd_by_ticker: Mapping[str, float]
    candidate_tickers: Sequence[str]
    daily_vol_by_ticker: Mapping[str, float]
    slot_realized_vol_by_ticker: Mapping[str, float]
    seasonal_profile: Mapping[str, float]
    target_weight_by_ticker: Mapping[str, float] | None = None
    reference_price_by_ticker: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class InventoryOutcome:
    """The result of one evaluation. ``ok`` False means publish nothing."""

    slot: str
    as_of: date
    ok: bool
    reason: str
    suggestions: tuple[InventorySuggestion, ...] = ()
    detail: Mapping[str, object] = field(default_factory=dict)


class _Failed(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _slot_time(asof: date, slot: str) -> datetime:
    if _ET is None:
        raise _Failed("timezone_unavailable", "America/New_York tz data is not installed")
    hh, mm = (int(p) for p in slot.split(":"))
    return datetime.combine(asof, time(hh, mm), tzinfo=_ET)  # type: ignore[arg-type]


def _numbers(name: str, mapping: object) -> dict[str, float]:
    if not isinstance(mapping, Mapping):
        raise _Failed("invalid_input", f"{name} must be a mapping")
    try:
        return {str(t): finite(f"{name}[{t}]", v) for t, v in mapping.items()}
    except ValueError as exc:
        raise _Failed("invalid_input", str(exc)) from exc


class InventoryReservationSuggester:
    """Shrinks (never enlarges) an overweight ticker's tier and deepens its top-up
    discount, scaled by the forecast variance left in the day.

    Args:
        slot: one of reservation.SLOTS; the instance only ever evaluates that slot.
        load_inputs: ``load_inputs(asof) -> InventoryInputs``. Any exception fails closed.
        params: InventoryReservationParams.
        clock: returns "now" (tz-aware); injected so a backtest replay can pin it.
    """

    def __init__(
        self,
        slot: str,
        load_inputs: Callable[[date], InventoryInputs],
        params: InventoryReservationParams | None = None,
        model_version: str = DEFAULT_MODEL_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if slot not in SLOTS:
            raise ValueError(f"unknown slot {slot!r}; expected one of {SLOTS}")
        self.slot = slot
        self._load_inputs = load_inputs
        self._params = params or InventoryReservationParams()
        self._model_version = model_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    source = SOURCE

    # ------------------------------------------------------------------ public API

    def suggest(self, ticker: str, asof: date) -> SizingSuggestion:
        """One candidate's suggestion for ``asof`` at this instance's slot.

        Raises:
            InventoryUnavailable: when the evaluation failed closed (with its reason), or
                with reason ``ticker_not_candidate`` for a ticker this slot is not buying.
        """
        outcome = self.evaluate(asof)
        if not outcome.ok:
            raise InventoryUnavailable(outcome.reason, str(outcome.detail.get("message", "")))
        for s in outcome.suggestions:
            if s.ticker == ticker:
                return s
        raise InventoryUnavailable("ticker_not_candidate", ticker)

    def evaluate(self, asof: date) -> InventoryOutcome:
        """Run the whole pipeline for ``asof``. Never raises for bad inputs."""
        try:
            return self._evaluate(asof)
        except _Failed as failed:
            return InventoryOutcome(self.slot, asof, False, failed.reason, detail={"message": str(failed)})

    # ------------------------------------------------------------------ pipeline

    def _evaluate(self, asof: date) -> InventoryOutcome:
        p = self._params
        try:
            inputs = self._load_inputs(asof)
        except Exception as exc:  # noqa: BLE001 -- any loader failure must fail closed, not crash
            raise _Failed("input_load_failed", f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(inputs, InventoryInputs):
            raise _Failed("invalid_input", f"loader returned {type(inputs).__name__}, not InventoryInputs")
        if inputs.as_of != asof:
            raise _Failed("stale_inputs", f"inputs are for {inputs.as_of}, not {asof.isoformat()}")
        if inputs.slot != self.slot:
            raise _Failed("slot_mismatch", f"inputs are for slot {inputs.slot!r}, not {self.slot!r}")
        self._check_known_at(inputs.known_at, asof)

        committed = _numbers("committed_usd_by_ticker", inputs.committed_usd_by_ticker)
        if any(v < 0.0 for v in committed.values()):
            raise _Failed("invalid_input", "a committed cost basis is negative")
        total = sum(committed.values())
        if total <= 0.0:
            # Nothing committed yet means nothing can be overweight; there is no target to
            # divide by, so say so rather than publish a file of 1.0s.
            raise _Failed("no_committed_capital", "total committed cost basis is zero")

        candidates = inputs.candidate_tickers
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise _Failed("invalid_input", "candidate_tickers must be a sequence of tickers")
        candidates = [str(t) for t in candidates]
        if not candidates:
            raise _Failed("missing_candidates", "no candidate tickers for the slot")
        if len(set(candidates)) != len(candidates):
            raise _Failed("invalid_input", "duplicate candidate ticker")
        not_in_universe = sorted(set(candidates) - set(committed))
        if not_in_universe:
            raise _Failed("missing_cost_basis", f"no committed cost basis row for {not_in_universe}")

        weights = self._target_weights(inputs.target_weight_by_ticker, committed)
        vols = _numbers("daily_vol_by_ticker", inputs.daily_vol_by_ticker)
        slot_vols = _numbers("slot_realized_vol_by_ticker", inputs.slot_realized_vol_by_ticker)
        refs = _numbers("reference_price_by_ticker", inputs.reference_price_by_ticker)
        for label, table in (("daily volatility", vols), ("slot realized volatility", slot_vols)):
            missing = sorted(t for t in candidates if t not in table)
            if missing:
                raise _Failed("missing_volatility", f"no {label} for {missing}")
        if any(v <= 0.0 for v in vols.values()):
            raise _Failed("invalid_input", "a daily volatility forecast is not positive")
        try:
            profile = normalized_profile(inputs.seasonal_profile)
        except ValueError as exc:
            raise _Failed("invalid_seasonal_profile", str(exc)) from exc
        w_left = remaining_weight(profile, self.slot)
        sig_med = median(list(vols.values()))

        now = self._clock()
        rows: list[InventorySuggestion] = []
        try:
            for t in sorted(candidates):
                q = inventory_excess(committed[t], weights[t] * total, p.q_max)
                delta, variance_left = reservation_discount(
                    q, vols[t], sig_med, w_left, slot_vols[t],
                    z=p.z, b0_bps=p.b0_bps, kappa=p.kappa, cap_sigmas=p.cap_sigmas,
                )
                m = size_multiplier(q, gamma_m=p.gamma_m, m_min=p.m_min)
                p_max = max_buy_price(refs[t], delta) if t in refs else None
                rows.append(InventorySuggestion(
                    ticker=t,
                    asof=asof,
                    multiplier_adjustment_pct=0.0 if m == 1.0 else (m - 1.0) * 100.0,
                    eval_metric_mae=UNSCORED_MAE,
                    source=SOURCE,
                    model_version=self._model_version,
                    generated_at=now,
                    scored=False,
                    slot=self.slot,
                    q=q,
                    discount=delta,
                    remaining_variance=variance_left,
                    max_buy_price=p_max,
                ))
        except ValueError as exc:
            raise _Failed("invalid_input", str(exc)) from exc

        detail = {
            "slot": self.slot,
            "known_at": inputs.known_at.isoformat(),
            "total_committed_usd": total,
            "universe_size": len(committed),
            "sigma_daily_median": sig_med,
            "weight_remaining": w_left,
            "target_weights": "equal" if inputs.target_weight_by_ticker is None else "supplied",
            "params": asdict(p),
        }
        return InventoryOutcome(self.slot, asof, True, "ok", tuple(rows), detail)

    def _check_known_at(self, known_at: object, asof: date) -> None:
        if not isinstance(known_at, datetime) or known_at.tzinfo is None:
            raise _Failed("invalid_input", "known_at must be a tz-aware datetime")
        slot_start = _slot_time(asof, self.slot)
        if known_at.astimezone(slot_start.tzinfo).date() != asof:
            raise _Failed("stale_inputs", f"known_at {known_at.isoformat()} is not on {asof.isoformat()} ET")
        now = self._clock()
        if known_at > now:
            raise _Failed("future_inputs", f"known_at {known_at.isoformat()} is after now {now.isoformat()}")
        age = now - known_at
        if age > timedelta(minutes=self._params.max_input_age_minutes):
            raise _Failed("stale_inputs", f"inputs are {age} old")
        # An earlier slot's snapshot replayed into this slot would understate committed
        # capital (this morning's buys missing) and overstate the remaining variance.
        if known_at < slot_start - timedelta(minutes=5):
            raise _Failed("stale_inputs", f"known_at {known_at.isoformat()} predates slot {self.slot}")

    def _target_weights(self, supplied: object, committed: Mapping[str, float]) -> dict[str, float]:
        if supplied is None:
            share = 1.0 / len(committed)
            return {t: share for t in committed}
        weights = _numbers("target_weight_by_ticker", supplied)
        if set(weights) != set(committed):
            raise _Failed("invalid_target_weights", "target weights must cover exactly the committed universe")
        if any(w <= 0.0 for w in weights.values()):
            raise _Failed("invalid_target_weights", "every target weight must be positive")
        if abs(sum(weights.values()) - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise _Failed("invalid_target_weights", f"target weights sum to {sum(weights.values())}, not 1")
        return weights
