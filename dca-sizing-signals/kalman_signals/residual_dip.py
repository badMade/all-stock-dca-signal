"""Residual-dip shadow signal: Kalman time-varying beta vs the market and the ticker's
co-movement cluster, scored on one session (docs/adr-kalman-residual-dip-shadow.md).

For each ticker and each factor (the market ticker, default SPY; and the mean return of the
ticker's other cluster members), the filter is fit and run over a trailing window of daily
log returns that ends at the PRIOR session, then the as-of session's return is scored
against that frozen, predicted state (ADR D4). The result is a standardized residual
``z`` split into an overnight part (prior close to open) and an intraday part (open to
close); only the intraday part feeds the label, because only intraday moves show reliable
reversal (research F6).

Labels (ADR D1, open question 2 default):

  * ``market_selloff``: the market's own intraday move is at or below ``factor_z`` sigma;
  * ``cluster_selloff``: not that, but the cluster's intraday move is;
  * ``idiosyncratic_dip``: neither, and the intraday residual is at or below ``dip_z``
    sigma against the market (and against the cluster, when it has a score);
  * ``none``: anything else. ``None`` with a ``label_reason`` when opens are missing.

Fail closed (ADR D6): a malformed row fails the whole run; so does a missing or stale
market bar. A ticker gets no record, with a reason in ``skipped``, when it is an ETF or
fund the caller lists (``not_applicable``), is inside an earnings window, lacks the
session's bar or the prior close, has too little history, is still in burn-in, has a beta
too uncertain to trust, or yields any non-finite number.

Pure: rows in, a ShadowResult out. No I/O, no network, no clock. The as-of bar must be
marked ``final: True`` -- at an intraday slot the caller builds a final synthetic bar from
the current quote (Phase 2), so a still-forming daily bar never leaks in.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from kalman_signals.kalman_filter import (
    DEFAULT_BURN_IN,
    DEFAULT_MIN_TRAIN,
    DEFAULT_Q_GRID,
    BetaState,
    FilterParams,
    beta_step,
    fit_params,
    ols,
    run_filter,
)
from kalman_signals.shadow import ShadowRecord, ShadowResult

LABEL_MARKET = "market_selloff"
LABEL_CLUSTER = "cluster_selloff"
LABEL_IDIOSYNCRATIC = "idiosyncratic_dip"
LABEL_NONE = "none"
FACTOR_MARKET = "market"
FACTOR_CLUSTER = "cluster"
#: Window for the rolling-OLS sanity beta logged beside the Kalman beta (ADR D5).
OLS_BASELINE_SESSIONS = 60


class KalmanInputError(ValueError):
    """A caller-supplied row is malformed. Raised, never skipped."""


@dataclass(frozen=True)
class ResidualDipConfig:
    """Every threshold, at the ADR's accepted defaults.

    ``window_sessions``: trailing return sessions the fit and filter read (about two
    years). ``max_beta_sd``: a beta whose filtered sd exceeds this is not trusted.
    ``max_staleness_days``: calendar days allowed between the prior market close and the
    as-of session (4 covers Friday to Tuesday over a Monday holiday).
    """

    market_ticker: str = "SPY"
    window_sessions: int = 504
    burn_in: int = DEFAULT_BURN_IN
    min_train: int = DEFAULT_MIN_TRAIN
    q_grid: tuple[float, ...] = DEFAULT_Q_GRID
    dip_z: float = -2.0
    factor_z: float = -1.0
    max_beta_sd: float = 0.5
    max_staleness_days: int = 4


@dataclass(frozen=True)
class ResidualDipInputs:
    """Everything one evaluation reads, supplied by the caller.

    Attributes:
        close_rows: mappings with ``ticker``, ``date`` (date or ISO string), ``close``
            (positive number), optional ``open`` (positive number or None), and optional
            bool ``final`` / ``interpolated``. Must include the market ticker's rows.
        cluster_members: ticker -> the members of its co-movement cluster (for example
            from comovement_signals.build_clusters, which the caller runs; this package may
            not import it). A ticker with no entry, or no other member, gets no cluster score.
        non_equity: ETFs and funds; they get ``not_applicable``.
        earnings_window: tickers with earnings within one trading day of the session.
    """

    close_rows: Sequence[Mapping[str, object]]
    cluster_members: Mapping[str, Sequence[str]] = field(default_factory=dict)
    non_equity: frozenset[str] = frozenset()
    earnings_window: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Bar:
    close: float
    open: float | None


# --- row parsing -----------------------------------------------------------------------


def _parse_date(value: object) -> date:
    if isinstance(value, date):
        return date(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise KalmanInputError(f"unparseable date {value!r}") from exc
    raise KalmanInputError(f"date must be a date or ISO string, got {type(value).__name__}")


def _bool_flag(row: Mapping[str, object], name: str) -> bool:
    value = row.get(name, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise KalmanInputError(f"{name} flag must be a bool: {row!r}")
    return value


def _positive(value: object, what: str, row: Mapping[str, object]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KalmanInputError(f"{what} must be a number: {row!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise KalmanInputError(f"{what} must be finite and positive: {row!r}")
    return number


def parse_bars(close_rows: Iterable[Mapping[str, object]], as_of: date) -> dict[str, dict[date, Bar]]:
    """Validate every real row; keep bars dated before ``as_of``, or on it when final.

    Same strictness as comovement_signals.returns: an ``interpolated`` (pre-listing fill)
    row is dropped before its numbers are read, a malformed row raises, a duplicate raises,
    and a future row is validated but never kept (the lookahead guard).
    """
    bars: dict[str, dict[date, Bar]] = {}
    seen: set[tuple[str, date]] = set()
    for row in close_rows:
        if not isinstance(row, Mapping):
            raise KalmanInputError(f"row must be a mapping, got {type(row).__name__}")
        if _bool_flag(row, "interpolated"):
            continue
        final = _bool_flag(row, "final")
        try:
            ticker, raw_date, raw_close = row["ticker"], row["date"], row["close"]
        except KeyError as exc:
            raise KalmanInputError(f"row missing ticker/date/close: {row!r}") from exc
        if not isinstance(ticker, str) or not ticker:
            raise KalmanInputError(f"ticker must be a non-empty string: {row!r}")
        d = _parse_date(raw_date)
        if (ticker, d) in seen:
            raise KalmanInputError(f"duplicate row for {ticker} on {d.isoformat()}")
        seen.add((ticker, d))
        close = _positive(raw_close, "close", row)
        raw_open = row.get("open")
        opened = None if raw_open is None else _positive(raw_open, "open", row)
        if d > as_of or (d == as_of and not final):
            continue
        bars.setdefault(ticker, {})[d] = Bar(close=close, open=opened)
    return bars


# --- return series ---------------------------------------------------------------------


@dataclass(frozen=True)
class SessionMove:
    """One session's log return, and its overnight / intraday split when an open exists."""

    total: float
    overnight: float | None
    intraday: float | None


def session_move(prev: Bar | None, cur: Bar | None) -> SessionMove | None:
    if prev is None or cur is None:
        return None
    total = math.log(cur.close / prev.close)
    if cur.open is None:
        return SessionMove(total, None, None)
    return SessionMove(total, math.log(cur.open / prev.close), math.log(cur.close / cur.open))


def moves(by_date: Mapping[date, Bar], calendar: Sequence[date]) -> list[SessionMove | None]:
    """Moves on ``calendar[1:]``. A missing close on either side gives None, never a
    multi-day return."""
    return [session_move(by_date.get(a), by_date.get(b)) for a, b in zip(calendar, calendar[1:], strict=False)]


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def cluster_moves(member_moves: Sequence[Sequence[SessionMove | None]]) -> list[SessionMove | None]:
    """Equal-weight mean of the members' moves per session; intraday/overnight averaged
    over the members that have them. None on a session where no member has a move."""
    if not member_moves:
        return []
    out: list[SessionMove | None] = []
    for i in range(len(member_moves[0])):
        present = [m[i] for m in member_moves if m[i] is not None]
        if not present:
            out.append(None)
            continue
        total = _mean([m.total for m in present])
        overnight = _mean([m.overnight for m in present if m.overnight is not None])
        intraday = _mean([m.intraday for m in present if m.intraday is not None])
        out.append(SessionMove(total, overnight, intraday))  # type: ignore[arg-type]
    return out


def observations(stock: Sequence[SessionMove | None], factor: Sequence[SessionMove | None]) -> list[
        tuple[float, float] | None]:
    return [(s.total, f.total) if s is not None and f is not None else None for s, f in zip(stock, factor, strict=True)]


# --- one factor model ------------------------------------------------------------------


@dataclass(frozen=True)
class FactorModel:
    params: FilterParams
    state: BetaState
    steps_since_init: int
    ols_beta: float | None


@dataclass(frozen=True)
class FactorScore:
    """The as-of session scored against the predicted (prior) state."""

    alpha: float
    beta: float
    beta_sd: float
    ols_beta: float | None
    innovation_var: float
    z_total: float
    z_overnight: float | None
    z_intraday: float | None
    factor_z_total: float
    factor_z_intraday: float | None
    params_version: str
    q: float
    r_obs: float
    steps_since_init: int

    def as_payload(self) -> dict[str, object]:
        return {
            "alpha": self.alpha, "beta": self.beta, "beta_sd": self.beta_sd, "ols60_beta": self.ols_beta,
            "innovation_var": self.innovation_var, "z_total": self.z_total, "z_overnight": self.z_overnight,
            "z_intraday": self.z_intraday, "factor_z_total": self.factor_z_total,
            "factor_z_intraday": self.factor_z_intraday, "params_version": self.params_version, "q": self.q,
            "r_obs": self.r_obs, "steps_since_init": self.steps_since_init,
        }


class FactorUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def build_factor_model(obs: Sequence[tuple[float, float] | None], fit_through: str,
                       config: ResidualDipConfig) -> FactorModel:
    """Fit q on ``obs`` and filter through it. Raises FactorUnavailable with a reason."""
    params = fit_params(obs, fit_through, q_grid=config.q_grid, burn_in=config.burn_in, min_train=config.min_train)
    if params is None:
        raise FactorUnavailable("insufficient_history")
    run = run_filter(obs, params, burn_in=config.burn_in)
    if run is None:
        raise FactorUnavailable("insufficient_history")
    if run.steps_since_init < config.burn_in:
        raise FactorUnavailable("burn_in")
    recent = [ob for ob in obs if ob is not None][-OLS_BASELINE_SESSIONS:]
    baseline = ols(recent)
    return FactorModel(params=params, state=run.state, steps_since_init=run.steps_since_init,
                       ols_beta=None if baseline is None else baseline.beta)


def score_session(model: FactorModel, stock: SessionMove, factor: SessionMove,
                  config: ResidualDipConfig) -> FactorScore:
    """Score one session against ``model``'s predicted state. Raises FactorUnavailable."""
    step = beta_step(model.state, stock.total, factor.total, model.params)
    prior = step.prior
    if prior.beta_sd > config.max_beta_sd:
        raise FactorUnavailable("beta_uncertain")
    sd = math.sqrt(step.innovation_var)
    z_overnight = z_intraday = factor_z_intraday = None
    if stock.intraday is not None and factor.intraday is not None:
        # The intraday part carries no alpha: a daily alpha is attributed to the overnight
        # part, so the two parts add back up to z_total (both scaled by the daily sd).
        z_intraday = (stock.intraday - prior.beta * factor.intraday) / sd
        factor_z_intraday = factor.intraday / model.params.factor_sd
    if stock.overnight is not None and factor.overnight is not None:
        z_overnight = (stock.overnight - prior.alpha - prior.beta * factor.overnight) / sd
    score = FactorScore(
        alpha=prior.alpha, beta=prior.beta, beta_sd=prior.beta_sd, ols_beta=model.ols_beta,
        innovation_var=step.innovation_var, z_total=step.z, z_overnight=z_overnight, z_intraday=z_intraday,
        factor_z_total=factor.total / model.params.factor_sd, factor_z_intraday=factor_z_intraday,
        params_version=model.params.version, q=model.params.q, r_obs=model.params.r_obs,
        steps_since_init=model.steps_since_init,
    )
    if not all(v is None or math.isfinite(v) for v in score.as_payload().values() if not isinstance(v, str)):
        raise FactorUnavailable("non_finite")
    return score


def label(market: FactorScore, cluster: FactorScore | None, config: ResidualDipConfig) -> tuple[str | None, str]:
    """(label, reason). See the module docstring for the rules."""
    if market.z_intraday is None or market.factor_z_intraday is None:
        return None, "no_open"
    if market.factor_z_intraday <= config.factor_z:
        return LABEL_MARKET, "market_move"
    if cluster is not None and cluster.factor_z_intraday is not None and cluster.factor_z_intraday <= config.factor_z:
        return LABEL_CLUSTER, "cluster_move"
    cluster_ok = cluster is None or cluster.z_intraday is None or cluster.z_intraday <= config.dip_z
    if market.z_intraday <= config.dip_z and cluster_ok:
        return LABEL_IDIOSYNCRATIC, "residual_move"
    return LABEL_NONE, "within_band"


# --- the shadow evaluation -------------------------------------------------------------


def residual_dip_shadow(inputs: ResidualDipInputs, as_of: date,
                        config: ResidualDipConfig | None = None) -> ShadowResult:
    """Score every ticker in ``inputs.close_rows`` on ``as_of``. Never raises on bad input:
    a whole-run failure is ``ok=False`` with a reason, and nothing is written for it."""
    config = config or ResidualDipConfig()
    try:
        bars = parse_bars(inputs.close_rows, as_of)
    except KalmanInputError as exc:
        return ShadowResult(as_of=as_of, ok=False, reason=f"invalid_input: {exc}")
    market_bars = bars.get(config.market_ticker)
    if not market_bars or as_of not in market_bars:
        return ShadowResult(as_of=as_of, ok=False, reason="market_missing_session")
    calendar = sorted(market_bars)
    if len(calendar) < 2:
        return ShadowResult(as_of=as_of, ok=False, reason="market_insufficient_history")
    prior_session = calendar[-2]
    if (as_of - prior_session).days > config.max_staleness_days:
        return ShadowResult(as_of=as_of, ok=False, reason="stale_market")
    # window_sessions return sessions before as_of need window_sessions + 1 closes, plus as_of.
    window = calendar[-(config.window_sessions + 2):]
    history_calendar, fit_through = window[:-1], prior_session.isoformat()

    all_moves = {t: moves(by_date, window) for t, by_date in bars.items()}
    market_moves = all_moves[config.market_ticker]
    market_today = market_moves[-1]
    records: list[ShadowRecord] = []
    skipped: dict[str, str] = {}
    market_models: dict[str, FactorModel] = {}

    for ticker in sorted(bars):
        if ticker == config.market_ticker:
            continue
        reason = _ticker_gate(ticker, bars[ticker], as_of, prior_session, inputs)
        if reason:
            skipped[ticker] = reason
            continue
        stock_moves = all_moves[ticker]
        stock_today = stock_moves[-1]
        try:
            model = build_factor_model(observations(stock_moves[:-1], market_moves[:-1]), fit_through, config)
            market_models[ticker] = model
            market_score = score_session(model, stock_today, market_today, config)  # type: ignore[arg-type]
        except FactorUnavailable as exc:
            skipped[ticker] = exc.reason
            continue
        cluster_score, cluster_reason = _cluster_score(ticker, inputs, all_moves, fit_through, config)
        chosen, why = label(market_score, cluster_score, config)
        payload = {
            "ticker": ticker,
            "session": as_of.isoformat(),
            "market_ticker": config.market_ticker,
            "history_sessions": len(history_calendar) - 1,
            "market": market_score.as_payload(),
            "cluster": None if cluster_score is None else cluster_score.as_payload(),
            "cluster_reason": cluster_reason,
            "label": chosen,
            "label_reason": why,
            "thresholds": {"dip_z": config.dip_z, "factor_z": config.factor_z},
        }
        records.append(ShadowRecord(as_of=as_of, inputs_known_at=as_of, payload=payload))
    return ShadowResult(as_of=as_of, ok=True, reason="ok", records=tuple(records), skipped=skipped)


def _ticker_gate(ticker: str, by_date: Mapping[date, Bar], as_of: date, prior_session: date,
                 inputs: ResidualDipInputs) -> str | None:
    if ticker in inputs.non_equity:
        return "not_applicable"
    if ticker in inputs.earnings_window:
        return "earnings_window"
    if as_of not in by_date:
        return "no_session_price"
    if prior_session not in by_date:
        return "no_prior_close"
    return None


def _cluster_score(ticker: str, inputs: ResidualDipInputs, all_moves: Mapping[str, list[SessionMove | None]],
                   fit_through: str, config: ResidualDipConfig) -> tuple[FactorScore | None, str]:
    """The cluster factor excludes the ticker itself (no self-regression), the market
    ticker and listed non-equities."""
    members = [m for m in inputs.cluster_members.get(ticker, ())
               if m != ticker and m != config.market_ticker and m not in inputs.non_equity and m in all_moves]
    if not members:
        return None, "no_cluster_peers"
    factor = cluster_moves([all_moves[m] for m in members])
    stock = all_moves[ticker]
    if factor[-1] is None:
        return None, "no_cluster_session_move"
    try:
        model = build_factor_model(observations(stock[:-1], factor[:-1]), fit_through, config)
        return score_session(model, stock[-1], factor[-1], config), "ok"  # type: ignore[arg-type]
    except FactorUnavailable as exc:
        return None, exc.reason
