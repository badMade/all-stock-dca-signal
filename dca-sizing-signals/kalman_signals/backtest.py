"""Walk-forward backtest of the residual-dip signal and the ADR D7 diagnostics (Phase 1).

Protocol (ADR D5, research 5.5): at the first evaluation session of each calendar month,
every (ticker, factor) model is fit and filtered on the trailing window that ends at the
prior session -- exactly what ``residual_dip_shadow`` does for one day, so the first day of
each block matches it bit for bit (a test pins this). Within the month the parameters stay
frozen and the filter just keeps stepping forward: each session is scored against the
predicted state first, then the observation updates it. Nothing reads a session after the
one being scored except the forward-return outcome, which is only ever an outcome.

Diagnostics (ADR D7):

  1. calibration of the standardized prior innovation z_total against the market factor:
     variance, share beyond +/-1.96, mean, pooled lag-1 autocorrelation; overall and split
     by market volatility regime (trailing 20-session realized vol above / below its median);
  2. forward ``horizon``-session market-adjusted log returns (sum of r_stock - beta * r_market
     after the session, beta frozen at scoring time) by intraday-z bucket, with standard
     errors clustered by date: per (bucket, date) mean first, then mean and SE across dates;
  3. event counts per bucket and the number of calendar months covered;
  4. the trial count (the q grid size) and how often each q was chosen.

Earnings (ADR D6): the caller may pass past announcement dates per ticker. Each scored session
within ``earnings_pad`` sessions of the first session on or after an announcement date is
flagged ``earnings_window`` -- a pad of 1 covers the session before, the announcement session
(a before-the-open report) and the session after (an after-the-close report). Flagged
sessions are still scored and the filter still steps through them, exactly as the live
shadow function would skip the ticker for the day but keep its history; the caller decides
which set the D7 gate is read on. Announcement dates are schedule facts published ahead of
time, not outcomes. Without them nothing is flagged and the report says so. Pure: rows in,
a dict out.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from kalman_signals.kalman_filter import beta_step, predict
from kalman_signals.residual_dip import (
    FACTOR_CLUSTER,
    FACTOR_MARKET,
    FactorModel,
    FactorScore,
    FactorUnavailable,
    ResidualDipConfig,
    SessionMove,
    build_factor_model,
    cluster_moves,
    label,
    moves,
    observations,
    parse_bars,
    score_session,
)

#: Intraday-z bucket edges (upper bounds, inclusive), most negative first.
BUCKET_EDGES = (-2.0, -1.0, 0.0, 1.0, math.inf)
BUCKET_NAMES = ("z<=-2", "-2<z<=-1", "-1<z<=0", "0<z<=1", "z>1")
VOL_LOOKBACK = 20


@dataclass(frozen=True)
class Event:
    ticker: str
    session: date
    label: str | None
    z_total: float
    z_intraday: float | None
    q: float
    forward_adjusted: float | None
    high_vol: bool | None
    earnings_window: bool = False


def bucket_of(z: float) -> str:
    for edge, name in zip(BUCKET_EDGES, BUCKET_NAMES, strict=True):
        if z <= edge:
            return name
    return BUCKET_NAMES[-1]


def _advance(model: FactorModel, stock: SessionMove | None, factor: SessionMove | None) -> FactorModel:
    """Commit one session to the filter: update on an observation, predict-only without."""
    params = model.params
    if stock is None or factor is None:
        return replace(model, state=predict(model.state, params.q_alpha, params.q_beta))
    step = beta_step(model.state, stock.total, factor.total, params)
    return replace(model, state=step.posterior, steps_since_init=model.steps_since_init + 1)


def walk_forward(
    close_rows: Sequence[Mapping[str, object]],
    start: date,
    end: date,
    config: ResidualDipConfig | None = None,
    cluster_members: Mapping[str, Sequence[str]] | None = None,
    non_equity: frozenset[str] = frozenset(),
    horizon: int = 5,
    earnings_dates: Mapping[str, Sequence[date]] | None = None,
    earnings_pad: int = 1,
) -> list[Event]:
    """Score every eligible (ticker, session) in [start, end]. Rows must be final closes."""
    config = config or ResidualDipConfig()
    cluster_members = cluster_members or {}
    bars = parse_bars(close_rows, end)
    calendar = sorted(bars.get(config.market_ticker, {}))
    if len(calendar) < config.window_sessions + 2:
        raise ValueError("market history is shorter than one fit window")
    all_moves = {t: moves(by_date, calendar) for t, by_date in bars.items()}  # index i = calendar[i + 1]
    market = all_moves[config.market_ticker]
    tickers = [t for t in sorted(bars) if t != config.market_ticker and t not in non_equity]
    clusters: dict[str, list[SessionMove | None]] = {}
    for t in tickers:
        peers = [m for m in cluster_members.get(t, ()) if m != t and m != config.market_ticker
                 and m not in non_equity and m in all_moves]
        if peers:
            clusters[t] = cluster_moves([all_moves[m] for m in peers])
    flagged = _earnings_sessions(calendar, earnings_dates or {}, earnings_pad)
    vol = _trailing_vol(market)
    vol_median = statistics.median([v for v in vol if v is not None]) if any(v is not None for v in vol) else None

    first = max(config.window_sessions + 1, next((i for i, d in enumerate(calendar) if d >= start), len(calendar)))
    events: list[Event] = []
    models: dict[tuple[str, str], FactorModel | None] = {}
    block_month: tuple[int, int] | None = None
    for i in range(first, len(calendar)):
        session = calendar[i]
        if session > end:
            break
        mi = i - 1  # move index of this session
        if (session.year, session.month) != block_month:
            block_month = (session.year, session.month)
            lo = mi - config.window_sessions
            fit_through = calendar[i - 1].isoformat()
            models = {}
            for t in tickers:
                models[(t, FACTOR_MARKET)] = _try_model(all_moves[t][lo:mi], market[lo:mi], fit_through, config)
                if t in clusters:
                    models[(t, FACTOR_CLUSTER)] = _try_model(all_moves[t][lo:mi], clusters[t][lo:mi], fit_through,
                                                             config)
        for t in tickers:
            stock_today = all_moves[t][mi]
            event = _score(t, session, stock_today, market[mi], models, clusters, mi, config)
            if event is not None:
                fwd = _forward_adjusted(all_moves[t], market, mi, horizon, event[1].beta)
                high = None if vol[mi] is None or vol_median is None else vol[mi] > vol_median
                events.append(Event(t, session, event[0], event[1].z_total, event[1].z_intraday, event[1].q, fwd,
                                    high, (t, session) in flagged))
            for factor_name, factor_moves in ((FACTOR_MARKET, market), (FACTOR_CLUSTER, clusters.get(t))):
                key = (t, factor_name)
                if models.get(key) is not None and factor_moves is not None:
                    models[key] = _advance(models[key], stock_today, factor_moves[mi])  # type: ignore[arg-type]
    return events


def _earnings_sessions(calendar: Sequence[date], earnings_dates: Mapping[str, Sequence[date]],
                       pad: int) -> set[tuple[str, date]]:
    """(ticker, session) pairs within ``pad`` sessions of each announcement's first session."""
    if pad < 0:
        raise ValueError("earnings_pad must be >= 0")
    out: set[tuple[str, date]] = set()
    for ticker, dates in earnings_dates.items():
        for d in dates:
            k = bisect.bisect_left(calendar, d)
            for j in range(max(0, k - pad), min(len(calendar), k + pad + 1)):
                out.add((ticker, calendar[j]))
    return out


def _try_model(stock, factor, fit_through, config) -> FactorModel | None:
    try:
        return build_factor_model(observations(stock, factor), fit_through, config)
    except FactorUnavailable:
        return None


def _score(t, session, stock_today, market_today, models, clusters, mi,
           config) -> tuple[str | None, FactorScore] | None:
    model = models.get((t, FACTOR_MARKET))
    if model is None or stock_today is None or market_today is None:
        return None
    try:
        market_score = score_session(model, stock_today, market_today, config)
    except FactorUnavailable:
        return None
    cluster_score = None
    cluster_model = models.get((t, FACTOR_CLUSTER))
    if cluster_model is not None and clusters[t][mi] is not None:
        try:
            cluster_score = score_session(cluster_model, stock_today, clusters[t][mi], config)
        except FactorUnavailable:
            cluster_score = None
    return label(market_score, cluster_score, config)[0], market_score


def _forward_adjusted(stock, market, mi, horizon, beta) -> float | None:
    window = range(mi + 1, mi + 1 + horizon)
    if window.stop > len(stock):
        return None
    total = 0.0
    for k in window:
        if stock[k] is None or market[k] is None:
            return None
        total += stock[k].total - beta * market[k].total
    return total


def _trailing_vol(market: Sequence[SessionMove | None]) -> list[float | None]:
    """Realized sd of the VOL_LOOKBACK market moves BEFORE each index (point in time)."""
    out: list[float | None] = []
    for i in range(len(market)):
        prior = [m.total for m in market[max(0, i - VOL_LOOKBACK):i] if m is not None]
        out.append(statistics.stdev(prior) if len(prior) >= VOL_LOOKBACK // 2 else None)
    return out


# --- diagnostics -----------------------------------------------------------------------


def calibration(events: Sequence[Event]) -> dict[str, object]:
    zs = [e.z_total for e in events]
    if len(zs) < 2:
        return {"n": len(zs)}
    by_ticker: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_ticker[e.ticker].append(e)
    pairs = [(a.z_total, b.z_total) for evs in by_ticker.values() for a, b in zip(evs, evs[1:], strict=False)]
    lag1 = None
    if len(pairs) > 2:
        xs, ys = [a for a, _ in pairs], [b for _, b in pairs]
        try:
            lag1 = statistics.correlation(xs, ys)
        except statistics.StatisticsError:
            lag1 = None
    return {
        "n": len(zs),
        "mean": statistics.fmean(zs),
        "variance": statistics.pvariance(zs),
        "share_beyond_1_96": sum(1 for z in zs if abs(z) > 1.96) / len(zs),
        "lag1_autocorr": lag1,
    }


def _clustered(groups: Sequence[str], keyed: Sequence[tuple[str, Event]]) -> dict[str, dict[str, object]]:
    """Per group: date-clustered mean and SE of forward_adjusted (per-date mean first)."""
    per_date: dict[str, dict[date, list[float]]] = {name: defaultdict(list) for name in groups}
    counts: Counter[str] = Counter()
    for name, e in keyed:
        if e.forward_adjusted is None:
            continue
        per_date[name][e.session].append(e.forward_adjusted)
        counts[name] += 1
    out: dict[str, dict[str, object]] = {}
    for name in groups:
        date_means = [statistics.fmean(v) for v in per_date[name].values()]
        mean = statistics.fmean(date_means) if date_means else None
        se = statistics.stdev(date_means) / math.sqrt(len(date_means)) if len(date_means) > 1 else None
        out[name] = {"events": counts[name], "dates": len(date_means), "mean": mean, "se_date_clustered": se}
    return out


def forward_by_bucket(events: Sequence[Event]) -> dict[str, dict[str, object]]:
    return _clustered(BUCKET_NAMES, [(bucket_of(e.z_intraday), e) for e in events if e.z_intraday is not None])


def forward_by_label(events: Sequence[Event]) -> dict[str, dict[str, object]]:
    """Descriptive only, not a D7 check: forward adjusted return per dip label."""
    keyed = [(e.label or "unlabeled", e) for e in events]
    return _clustered(sorted({name for name, _ in keyed}), keyed)


def _calibrated_variance(cal: Mapping[str, object]) -> bool:
    v = cal.get("variance")
    return isinstance(v, float) and 0.9 <= v <= 1.1


def _calibrated_tails(cal: Mapping[str, object]) -> bool:
    s = cal.get("share_beyond_1_96")
    return isinstance(s, float) and 0.03 <= s <= 0.07


def report(events: Sequence[Event], q_grid: Sequence[float], horizon: int = 5) -> dict[str, object]:
    """The D7 report. ``gate`` gives each D7 check pass/fail; trials are the q grid."""
    overall = calibration(events)
    regimes = {
        "high_vol": calibration([e for e in events if e.high_vol is True]),
        "low_vol": calibration([e for e in events if e.high_vol is False]),
    }
    buckets = forward_by_bucket(events)
    means = [buckets[name]["mean"] for name in BUCKET_NAMES]
    monotonic = all(m is not None for m in means) and all(
        a >= b for a, b in zip(means, means[1:], strict=False))  # type: ignore[operator]
    months = sorted({(e.session.year, e.session.month) for e in events})
    min_events = min((buckets[name]["events"] for name in BUCKET_NAMES), default=0)
    gate = {
        "variance_0_9_to_1_1": _calibrated_variance(overall),
        "share_beyond_1_96_3_to_7_pct": _calibrated_tails(overall),
        # ADR D7.1 also asks for both checks "by volatility regime": each regime on its own.
        "variance_0_9_to_1_1_each_vol_regime": all(_calibrated_variance(c) for c in regimes.values()),
        "share_beyond_1_96_3_to_7_pct_each_vol_regime": all(_calibrated_tails(c) for c in regimes.values()),
        "forward_returns_monotonic": monotonic,
        "at_least_1300_events_per_bucket": min_events >= 1300,
        "at_least_6_months": len(months) >= 6,
    }
    return {
        "events": len(events),
        "months": len(months),
        "first_session": min(e.session for e in events).isoformat() if events else None,
        "last_session": max(e.session for e in events).isoformat() if events else None,
        "horizon_sessions": horizon,
        "calibration": overall,
        "calibration_by_regime": regimes,
        "forward_adjusted_by_intraday_z": buckets,
        "forward_adjusted_by_label": forward_by_label(events),
        "labels": dict(Counter(e.label or "unlabeled" for e in events)),
        "trials": {"q_grid": list(q_grid), "count": len(q_grid),
                   "q_chosen": {f"{q:g}": n for q, n in sorted(Counter(e.q for e in events).items())}},
        "gate": gate,
        "gate_passed": all(gate.values()),
        "earnings_window_events": sum(1 for e in events if e.earnings_window),
        "notes": [
            "Earnings windows are only flagged when the caller passes announcement dates; see"
            " earnings_window_events and the caller's own filtering.",
            "Statistical significance of the bucket ordering is not tested here; read the date-clustered SEs.",
            "Record the q grid in the Phase 0 ledger's trials table before acting on any result.",
        ],
    }
