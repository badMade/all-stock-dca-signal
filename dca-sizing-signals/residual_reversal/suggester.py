"""Budget-neutral residual-reversal SizingSuggester (docs/adr-residual-reversal-suggester.md).

For each proposed ticker:

  1. Point-in-time daily log returns of the ticker and its caller-mapped sector ETF
     (rows after the as-of date, non-final rows on it and ``interpolated`` rows are never
     read; a malformed row fails the whole run rather than being skipped).
  2. OLS beta of the ticker on the ETF over the trailing ``beta_window`` returns, and the
     residuals of the newest ``ou_window`` of them. Using a shorter OU window inside the
     beta window avoids the Avellaneda-Lee appendix artifact: estimating both on the same
     days forces the last cumulated residual (with an intercept) to exactly zero.
  3. AR(1) / OU fit of the cumulated residual and its s-score.
  4. The relationship-break guard (below). A guarded ticker gets NO suggestion -- not a
     neutral 1.0 -- so SizingAdjustmentReader falls back to the base tier for it without
     having to know why.
  5. A raw multiplier, clip(1 - slope * s, lo, hi) outside the |s| < entry band, 1.0
     inside it, then one common shift across every unguarded ticker so the day's proposed
     dollars are unchanged (model.budget_neutral). The tilt only moves the marginal
     dollar between names; it never adds or removes spend.

Relationship-break guard, any one of which suppresses the ticker:

  * ``insufficient_history`` -- too few aligned returns for the fit plus the half-life
    baseline;
  * ``stale_history`` -- the ticker's or its ETF's newest close is too old;
  * ``no_benchmark`` -- the caller mapped no ETF to the ticker, or the ETF has no closes;
  * ``low_r_squared`` -- the ETF explains less than ``min_r_squared`` of the ticker;
  * ``not_mean_reverting`` -- the AR(1) b is outside (0, b_max), i.e. the OU half-life is
    too long for the window (b_max = exp(-1/30): mean-reversion time under half the
    60-session window, the Avellaneda-Lee tradability bound);
  * ``half_life_jump`` -- today's half-life is more than ``half_life_jump`` times the
    median of the previous ``half_life_lookback`` days' half-lives;
  * ``no_half_life_baseline`` -- too few of those previous days had a tradable fit to
    form that median;
  * ``corporate_event`` -- an 8-K Item 1.01 / 2.01 / 5.01 (or any caller-tagged material
    event) dated within ``event_lookback_days``, or an earnings date within
    ``earnings_window_days`` either side of the as-of date. Only events whose
    ``known_at`` is on or before the as-of date count (point in time: a later filing
    cannot reach back into a past decision).

This module never reads a regime model. Regime and crowding modulation of the tilt's
size belongs in SizingAdjustmentReader, the one component allowed to combine
suggesters (ADR, "What this does not do").
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from dca_shared_interfaces.forecasting import SizingSuggestion
from residual_reversal.model import OUFit, budget_neutral, cumulate, fit_ou, ols, raw_multiplier, s_score

SIGNAL = "residual_reversal"
SOURCE = "residual_reversal.sector_etf_ou"
DEFAULT_MODEL_VERSION = "residual-reversal-sector-etf-ou-v1"

# Same policy as comovement_signals (review round 1 on PR #2106): an unscored rule must
# never read as a perfect 0.0 MAE. +inf can never win a lower-is-better comparison, and
# the shared SuggestionPublisher refuses it as non-finite.
UNSCORED_MAE = math.inf

# Material-event kinds the guard understands. Callers may tag any other material event
# with "material"; kinds outside this set are refused as malformed input.
EVENT_KINDS = frozenset({"8k_1.01", "8k_2.01", "8k_5.01", "material", "earnings"})

GUARD_REASONS = (
    "insufficient_history",
    "stale_history",
    "no_benchmark",
    "low_r_squared",
    "not_mean_reverting",
    "half_life_jump",
    "no_half_life_baseline",
    "corporate_event",
)


@dataclass(frozen=True, slots=True)
class ResidualReversalSuggestion(SizingSuggestion):
    """A SizingSuggestion that also carries, by type, that it is unscored, plus the
    s-score and proposed dollars it was computed from (the publisher re-checks budget
    neutrality from these)."""

    scored: bool = False
    s_score: float = 0.0
    proposed_usd: float = 0.0


class ResidualReversalUnavailable(RuntimeError):
    """No suggestion for this ticker. ``reason`` is the fail-closed or guard reason."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


@dataclass(frozen=True)
class CorporateEvent:
    """One point-in-time event for the break guard.

    Attributes:
        kind: one of EVENT_KINDS.
        event_date: the filing date for an 8-K (SEC filed_date), the report date for
            earnings.
        known_at: the first date this record could have been known. An 8-K is known on
            its filed date; a scheduled earnings date is known when it was announced.
    """

    kind: str
    event_date: date
    known_at: date


@dataclass(frozen=True)
class ResidualReversalConfig:
    """Every threshold. Defaults are the Avellaneda-Lee values or the ADR's proposals;
    none were tuned on this repo's data.

    ``max_price_staleness_days`` matches comovement_signals: 4 calendar days covers a
    Friday close read on a Tuesday after a Monday holiday.
    """

    beta_window: int = 90
    ou_window: int = 60
    b_max: float = math.exp(-1.0 / 30.0)
    entry: float = 1.25
    slope: float = 0.1
    min_multiplier: float = 0.75
    max_multiplier: float = 1.25
    min_r_squared: float = 0.25
    half_life_jump: float = 3.0
    half_life_lookback: int = 20
    min_half_life_baseline: int = 10
    event_lookback_days: int = 90
    earnings_window_days: int = 3
    center_means: bool = False
    max_price_staleness_days: int = 4


@dataclass(frozen=True)
class ResidualReversalInputs:
    """Everything one evaluation reads, supplied by the caller.

    Attributes:
        close_rows: daily close rows for the tickers AND their ETFs. Each row has
            ``ticker`` (str), ``date`` (date or ISO string), ``close`` (positive number)
            and optional bool ``final`` / ``interpolated``. Split/dividend-adjusted closes
            as known at the as-of date: an adjusted history re-pulled later changes past
            returns, so backtests should snapshot it (ADR, "Point in time").
        proposed_buys_usd_by_ticker: the day's proposed dollars per ticker. Budget
            neutrality is over these.
        benchmark_by_ticker: ticker -> sector ETF symbol.
        events_by_ticker: optional ticker -> CorporateEvents.
    """

    close_rows: Sequence[Mapping[str, object]]
    proposed_buys_usd_by_ticker: Mapping[str, float]
    benchmark_by_ticker: Mapping[str, str]
    events_by_ticker: Mapping[str, Sequence[CorporateEvent]] = field(default_factory=dict)


@dataclass(frozen=True)
class TickerFit:
    """Per-ticker diagnostics, published in the outcome detail."""

    ticker: str
    benchmark: str
    beta: float
    r_squared: float
    ou: OUFit
    baseline_half_life: float


@dataclass(frozen=True)
class ResidualReversalOutcome:
    """One evaluation. ``ok`` False means publish nothing at all. ``guarded`` maps each
    suppressed ticker to its guard reason; those tickers have no suggestion."""

    as_of: date
    ok: bool
    reason: str
    suggestions: tuple[ResidualReversalSuggestion, ...] = ()
    guarded: Mapping[str, str] = field(default_factory=dict)
    detail: Mapping[str, object] = field(default_factory=dict)
    signal: str = SIGNAL


class _Failed(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _Guard(Exception):
    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


# ---------------------------------------------------------------------------- input parsing


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise _Failed("invalid_input", f"unparseable date {value!r}") from exc
    raise _Failed("invalid_input", f"date must be a date or ISO string, got {type(value).__name__}")


def _flag(row: Mapping[str, object], name: str) -> bool:
    value = row.get(name, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _Failed("invalid_input", f"{name} flag must be a bool: {row!r}")
    return value


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _Failed("invalid_input", f"{name} must be a finite number, got {value!r}")
    return float(value)


def _clean_closes(close_rows: Sequence[Mapping[str, object]], as_of: date) -> dict[str, dict[date, float]]:
    """Same row rules as comovement_signals.returns._clean_rows (which this package may not
    import): validate every real row, future ones included; drop interpolated rows,
    rows after as_of and non-final rows on it."""
    closes: dict[str, dict[date, float]] = {}
    seen: set[tuple[str, date]] = set()
    for row in close_rows:
        if not isinstance(row, Mapping):
            raise _Failed("invalid_input", f"row must be a mapping, got {type(row).__name__}")
        if _flag(row, "interpolated"):
            continue
        final = _flag(row, "final")
        try:
            ticker, raw_date, raw_close = row["ticker"], row["date"], row["close"]
        except KeyError as exc:
            raise _Failed("invalid_input", f"row missing ticker/date/close: {row!r}") from exc
        if not isinstance(ticker, str) or not ticker:
            raise _Failed("invalid_input", f"ticker must be a non-empty string: {row!r}")
        d = _parse_date(raw_date)
        if (ticker, d) in seen:
            raise _Failed("invalid_input", f"duplicate row for {ticker} on {d.isoformat()}")
        seen.add((ticker, d))
        close = _finite(f"close for {ticker} on {d.isoformat()}", raw_close)
        if close <= 0.0:
            raise _Failed("invalid_input", f"close must be positive: {row!r}")
        if d > as_of or (d == as_of and not final):
            continue
        closes.setdefault(ticker, {})[d] = close
    return closes


def _aligned_returns(
    ticker_closes: Mapping[date, float], etf_closes: Mapping[date, float]
) -> list[tuple[date, float, float]]:
    """(date, ticker log return, ETF log return) on each ETF session where the ticker
    also closed on that session and on the previous ETF session. The ETF's own dates are
    the session calendar (a sector ETF trades every session), so a gap in the ticker's
    history drops that return instead of creating a multi-day one."""
    sessions = sorted(etf_closes)
    out = []
    for prev, cur in zip(sessions, sessions[1:], strict=False):
        if prev in ticker_closes and cur in ticker_closes:
            out.append((
                cur,
                math.log(ticker_closes[cur] / ticker_closes[prev]),
                math.log(etf_closes[cur] / etf_closes[prev]),
            ))
    return out


def _check_config(cfg: ResidualReversalConfig) -> None:
    ints = ("beta_window", "ou_window", "half_life_lookback", "min_half_life_baseline",
            "event_lookback_days", "earnings_window_days", "max_price_staleness_days")
    for name in ints:
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _Failed("invalid_config", f"{name} must be a non-negative int, got {value!r}")
    floats = ("b_max", "entry", "slope", "min_multiplier", "max_multiplier", "min_r_squared", "half_life_jump")
    for name in floats:
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _Failed("invalid_config", f"{name} must be a finite number, got {value!r}")
    if cfg.ou_window < 10 or cfg.ou_window >= cfg.beta_window:
        raise _Failed("invalid_config", "ou_window must be at least 10 and shorter than beta_window")
    if not (0.0 < cfg.b_max < 1.0):
        raise _Failed("invalid_config", "b_max must be in (0, 1)")
    if not (0.0 < cfg.min_multiplier <= 1.0 <= cfg.max_multiplier):
        raise _Failed("invalid_config", "multiplier bounds must satisfy 0 < min <= 1 <= max")
    if cfg.entry < 0.0 or cfg.slope < 0.0 or cfg.half_life_jump <= 1.0:
        raise _Failed("invalid_config", "entry and slope must be >= 0 and half_life_jump > 1")
    if cfg.min_half_life_baseline < 1 or cfg.min_half_life_baseline > cfg.half_life_lookback:
        raise _Failed("invalid_config", "min_half_life_baseline must be in [1, half_life_lookback]")


# ---------------------------------------------------------------------------- suggester


class ResidualReversalSuggester:
    """Structurally a dca_shared_interfaces SizingSuggester: one public ``suggest`` plus
    ``evaluate`` for the whole batch. No write, no apply, no live-engine parameter.

    Args:
        load_inputs: ``asof -> ResidualReversalInputs``. Every price, buy, benchmark and
            event comes from here; this package makes no network or broker call.
        config: thresholds (ResidualReversalConfig defaults if omitted).
        model_version: recorded on every suggestion.
        clock: UTC "now" for ``generated_at`` (injected for tests).
    """

    signal = SIGNAL
    source = SOURCE

    def __init__(
        self,
        load_inputs: Callable[[date], ResidualReversalInputs],
        config: ResidualReversalConfig | None = None,
        model_version: str = DEFAULT_MODEL_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._load_inputs = load_inputs
        self._config = config or ResidualReversalConfig()
        self._model_version = model_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def config(self) -> ResidualReversalConfig:
        return self._config

    def suggest(self, ticker: str, asof: date) -> SizingSuggestion:
        """One ticker's suggestion.

        Raises:
            ResidualReversalUnavailable: the run failed closed (its reason), the ticker
                was guarded (``guarded:<reason>``), or it had no proposed buy
                (``ticker_not_proposed``).
        """
        outcome = self.evaluate(asof)
        if not outcome.ok:
            raise ResidualReversalUnavailable(outcome.reason, str(outcome.detail.get("message", "")))
        if ticker in outcome.guarded:
            raise ResidualReversalUnavailable(f"guarded:{outcome.guarded[ticker]}", ticker)
        for s in outcome.suggestions:
            if s.ticker == ticker:
                return s
        raise ResidualReversalUnavailable("ticker_not_proposed", ticker)

    def evaluate(self, asof: date) -> ResidualReversalOutcome:
        """The whole day's batch. Never raises for bad inputs; a failure is ok=False."""
        try:
            return self._evaluate(asof)
        except _Failed as failed:
            return ResidualReversalOutcome(asof, False, failed.reason, detail={"message": str(failed)})

    # ------------------------------------------------------------------ pipeline

    def _evaluate(self, asof: date) -> ResidualReversalOutcome:
        cfg = self._config
        _check_config(cfg)
        try:
            inputs = self._load_inputs(asof)
        except Exception as exc:  # noqa: BLE001 -- any loader failure must fail closed, not crash
            raise _Failed("input_load_failed", f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(inputs, ResidualReversalInputs):
            raise _Failed("invalid_input", f"loader returned {type(inputs).__name__}, not ResidualReversalInputs")
        for name in ("proposed_buys_usd_by_ticker", "benchmark_by_ticker", "events_by_ticker"):
            if not isinstance(getattr(inputs, name), Mapping):
                raise _Failed("invalid_input", f"{name} must be a mapping")
        if isinstance(inputs.close_rows, (str, bytes)) or not isinstance(inputs.close_rows, Sequence):
            raise _Failed("invalid_input", "close_rows must be a sequence of row mappings")

        buys = {t: _finite(f"proposed buy for {t}", v) for t, v in inputs.proposed_buys_usd_by_ticker.items()}
        if not buys:
            raise _Failed("missing_proposed_buys", "no proposed buys for the day")
        if any(v < 0.0 for v in buys.values()):
            raise _Failed("invalid_input", "a proposed buy is negative")
        events = self._parse_events(inputs.events_by_ticker)
        closes = _clean_closes(inputs.close_rows, asof)
        if not closes:
            raise _Failed("missing_prices", "no usable close rows on or before the as-of date")

        fits: dict[str, TickerFit] = {}
        guarded: dict[str, str] = {}
        for ticker in sorted(buys):
            if buys[ticker] == 0.0:
                continue  # nothing to tilt; not a suggestion and not a guard
            try:
                self._event_guard(events.get(ticker, ()), asof)
                fits[ticker] = self._fit_ticker(ticker, inputs.benchmark_by_ticker.get(ticker), closes, asof)
            except _Guard as guard:
                guarded[ticker] = guard.reason

        mean_shift = 0.0
        if cfg.center_means and fits:
            mean_shift = statistics.fmean(f.ou.m for f in fits.values())
        scores = {t: s_score(f.ou, mean_shift) for t, f in fits.items()}
        if any(not math.isfinite(s) for s in scores.values()):
            raise _Failed("failed_check", "non-finite s-score")
        raw = {t: raw_multiplier(s, cfg.entry, cfg.slope, cfg.min_multiplier, cfg.max_multiplier)
               for t, s in scores.items()}
        try:
            final = budget_neutral(raw, {t: buys[t] for t in raw}, cfg.min_multiplier, cfg.max_multiplier)
        except ValueError as exc:
            raise _Failed("failed_check", str(exc)) from exc

        now = self._clock()
        suggestions = tuple(
            ResidualReversalSuggestion(
                ticker=t,
                asof=asof,
                multiplier_adjustment_pct=0.0 if final[t] == 1.0 else (final[t] - 1.0) * 100.0,
                eval_metric_mae=UNSCORED_MAE,
                source=self.source,
                model_version=self._model_version,
                generated_at=now,
                scored=False,
                s_score=scores[t],
                proposed_usd=buys[t],
            )
            for t in sorted(final)
        )
        detail = {
            "config": asdict(self._config),
            "mean_shift": mean_shift,
            "guarded": dict(sorted(guarded.items())),
            "tickers": {
                t: {
                    "benchmark": f.benchmark,
                    "beta": f.beta,
                    "r_squared": f.r_squared,
                    "ou_b": f.ou.b,
                    "kappa": f.ou.kappa,
                    "half_life_sessions": f.ou.half_life_sessions,
                    "baseline_half_life_sessions": f.baseline_half_life,
                    "sigma_eq": f.ou.sigma_eq,
                    "s_score": scores[t],
                    "raw_multiplier": raw[t],
                    "multiplier": final[t],
                }
                for t, f in sorted(fits.items())
            },
        }
        return ResidualReversalOutcome(asof, True, "ok", suggestions, dict(sorted(guarded.items())), detail)

    def _parse_events(self, events_by_ticker: Mapping[str, object]) -> dict[str, tuple[CorporateEvent, ...]]:
        parsed: dict[str, tuple[CorporateEvent, ...]] = {}
        for ticker, evs in events_by_ticker.items():
            if isinstance(evs, (str, bytes)) or not isinstance(evs, Sequence):
                raise _Failed("invalid_input", f"events for {ticker} must be a sequence")
            for ev in evs:
                if not isinstance(ev, CorporateEvent):
                    raise _Failed("invalid_input", f"event for {ticker} is {type(ev).__name__}, not CorporateEvent")
                if ev.kind not in EVENT_KINDS:
                    raise _Failed("invalid_input", f"unknown event kind {ev.kind!r} for {ticker}")
                if not isinstance(ev.event_date, date) or not isinstance(ev.known_at, date):
                    raise _Failed("invalid_input", f"event dates for {ticker} must be dates")
            parsed[ticker] = tuple(evs)
        return parsed

    def _event_guard(self, events: Sequence[CorporateEvent], asof: date) -> None:
        cfg = self._config
        for ev in events:
            if ev.known_at > asof:
                continue  # not yet knowable at the decision date
            if ev.kind == "earnings":
                if abs((ev.event_date - asof).days) <= cfg.earnings_window_days:
                    raise _Guard("corporate_event", f"earnings on {ev.event_date.isoformat()}")
            elif asof - timedelta(days=cfg.event_lookback_days) <= ev.event_date <= asof:
                raise _Guard("corporate_event", f"{ev.kind} filed {ev.event_date.isoformat()}")

    def _fit_ticker(self, ticker: str, benchmark: object, closes, asof: date) -> TickerFit:
        cfg = self._config
        if not isinstance(benchmark, str) or not benchmark or benchmark not in closes:
            raise _Guard("no_benchmark")
        if benchmark == ticker:
            raise _Guard("no_benchmark", "a ticker cannot be its own benchmark")
        if ticker not in closes:
            raise _Guard("insufficient_history")
        for name in (ticker, benchmark):
            if (asof - max(closes[name])).days > cfg.max_price_staleness_days:
                raise _Guard("stale_history", f"{name} newest close {max(closes[name]).isoformat()}")
        series = _aligned_returns(closes[ticker], closes[benchmark])
        needed = cfg.beta_window + cfg.half_life_lookback
        if len(series) < needed:
            raise _Guard("insufficient_history", f"{len(series)} aligned returns, need {needed}")
        # The newest aligned return must be the ETF's newest session: otherwise the
        # ticker skipped the last session and its s-score would describe an older day.
        if series[-1][0] != max(closes[benchmark]):
            raise _Guard("stale_history", f"{ticker} has no return on {max(closes[benchmark]).isoformat()}")

        current = self._fit_window(series)
        if isinstance(current, _Guard):
            raise current
        beta, r_squared, ou = current

        history = []
        for offset in range(1, cfg.half_life_lookback + 1):
            past = self._fit_window(series[:-offset])
            if not isinstance(past, _Guard):
                history.append(past[2].half_life_sessions)
        if len(history) < cfg.min_half_life_baseline:
            raise _Guard("no_half_life_baseline", f"{len(history)} tradable fits in the lookback")
        baseline = statistics.median(history)
        if ou.half_life_sessions > cfg.half_life_jump * baseline:
            raise _Guard("half_life_jump", f"{ou.half_life_sessions:.2f} vs median {baseline:.2f}")
        return TickerFit(ticker, benchmark, beta, r_squared, ou, baseline)

    def _fit_window(self, series: Sequence[tuple[date, float, float]]):
        """Fit the window ending at the last element of ``series``. Returns
        (beta, r_squared, OUFit) or the _Guard that this window trips."""
        cfg = self._config
        window = series[-cfg.beta_window:]
        try:
            reg = ols([r[2] for r in window], [r[1] for r in window])
        except ValueError as exc:
            return _Guard("low_r_squared", str(exc))
        if reg.r_squared < cfg.min_r_squared:
            return _Guard("low_r_squared", f"R^2 {reg.r_squared:.3f}")
        x = cumulate(reg.residuals[-cfg.ou_window:])
        try:
            ou = fit_ou(x)
        except ValueError as exc:
            return _Guard("not_mean_reverting", str(exc))
        if ou.b >= cfg.b_max:
            return _Guard("not_mean_reverting", f"b={ou.b:.4f} >= {cfg.b_max:.4f}")
        return reg.beta, reg.r_squared, ou
