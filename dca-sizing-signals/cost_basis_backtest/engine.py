"""The walk-forward replay: baseline and candidate on the same cash flows, side by side.

For every decision session t in ``baseline_buys`` (the graded baseline's proposed dollars,
supplied by the caller -- see ledger_io.baseline_from_ledger_fills):

  1. Build a PointInTimeView of closes before t and events known before t.
  2. Ask the candidate for multipliers on that session's baseline dollars.
  3. Fill both arms at the close of t: baseline ``usd``, candidate ``usd * multiplier``,
     the same one-way buy cost on each. Nothing is ever sold.

Then score the two arms on the same fills (metrics.py). The run is rejected, not scored,
if the candidate read anything dated t or later through the view, or returned a malformed
decision. A candidate that fails closed on a session (an ``ok=False`` outcome, or an
exception) is replayed at 1.0 for that session and counted -- the live fallback -- so an
always-failing candidate shows up as zero tilt, not as an error that hides the count.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date

from cost_basis_backtest import metrics as m
from cost_basis_backtest.candidate import Candidate, CandidateDecision
from cost_basis_backtest.overfitting import sharpe
from cost_basis_backtest.view import (
    BacktestInputError,
    PointInTimeView,
    PriceIndex,
    event_known_at,
    finite,
    parse_day,
)


@dataclass(frozen=True)
class BacktestConfig:
    """Every knob. None was tuned on this repo's data.

    Attributes:
        buy_cost_bps: one-way buy cost (spread plus slippage) on every dollar, both arms.
            Robinhood charges no commission; this is the half-spread you pay to buy. There
            are no sell costs because nothing is sold.
        per_order_cost_usd: optional fixed cost per order, both arms (default 0).
        horizons: forward sessions for tilt returns, hit rate and the bucket test (the
            report's 5 to 20 days).
        oos_start: first out-of-sample session. Metrics are reported for the whole run and,
            when set, separately before and from it. The promotion test reads only the
            out-of-sample half, so it cannot pass without one.
        score_edges: interior bucket edges for the candidate's score (residual_reversal's
            entry band is |s| >= 1.25).
        score_sign: -1 if forward residual returns should fall as the score rises.
        min_bucket_count: below this many points in any bucket, monotonicity is "too thin",
            not pass.
        rolling_window_sessions: the "3 months" in worst rolling underperformance.
        lookahead_ic_threshold / lookahead_min_days: the oracle canary (metrics.lookahead_ic).
        max_multiplier: a decision outside [0, max_multiplier] is malformed.
        universe_point_in_time: set True only if the tickers were chosen from a dated
            membership table; otherwise the result is labelled survivor-biased, as the
            Phase 0 ADR requires.
    """

    buy_cost_bps: float = 0.0
    per_order_cost_usd: float = 0.0
    horizons: tuple[int, ...] = (5, 10, 20)
    oos_start: date | None = None
    score_edges: tuple[float, ...] = (-2.0, -1.25, 1.25, 2.0)
    score_sign: int = -1
    min_bucket_count: int = 30
    rolling_window_sessions: int = 63
    lookahead_ic_threshold: float = 0.3
    lookahead_min_days: int = 20
    max_multiplier: float = 4.0
    universe_point_in_time: bool = False

    def check(self) -> None:
        for name in ("buy_cost_bps", "per_order_cost_usd", "lookahead_ic_threshold", "max_multiplier"):
            value = finite(getattr(self, name), f"config.{name}")
            if value < 0.0:
                raise BacktestInputError(f"config.{name} must be >= 0")
        if self.buy_cost_bps >= 1e4:
            raise BacktestInputError("config.buy_cost_bps must be below 10000")
        if not self.horizons or any(not isinstance(h, int) or isinstance(h, bool) or h < 1 for h in self.horizons):
            raise BacktestInputError("config.horizons must be positive ints")
        if list(self.score_edges) != sorted(set(self.score_edges)):
            raise BacktestInputError("config.score_edges must be strictly increasing")
        if self.score_sign not in (-1, 1):
            raise BacktestInputError("config.score_sign must be -1 or 1")
        for name in ("min_bucket_count", "rolling_window_sessions", "lookahead_min_days"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise BacktestInputError(f"config.{name} must be a positive int")


@dataclass(frozen=True)
class DayRecord:
    session: date
    available: bool
    reason: str
    n_filled: int
    n_unfilled: int
    net_tilt_usd: float
    baseline_usd: float


@dataclass(frozen=True)
class BacktestResult:
    """``status`` is ``ok`` or ``rejected``. A rejected run has no metrics: its numbers
    would be computed from a candidate that broke the point-in-time rule or the decision
    contract, so there is nothing honest to report."""

    candidate: str
    status: str
    reason: str
    config: BacktestConfig
    fills: tuple[m.FillRecord, ...] = ()
    days: tuple[DayRecord, ...] = ()
    active_returns: tuple[tuple[date, float, float], ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)
    lookahead_flags: tuple[str, ...] = ()
    promotion: Mapping[str, object] = field(default_factory=dict)

    def active_series(self, start: date | None = None) -> list[float]:
        """Daily candidate-minus-baseline returns (the series the overfitting guards use)."""
        return [rc - rb for d, rb, rc in self.active_returns if start is None or d >= start]

    def summary(self) -> dict[str, object]:
        """JSON-safe: dates as ISO strings, non-finite numbers as None, no per-fill rows."""
        return _json_safe({
            "candidate": self.candidate,
            "status": self.status,
            "reason": self.reason,
            "config": asdict(self.config),
            "metrics": self.metrics,
            "lookahead_flags": list(self.lookahead_flags),
            "promotion": self.promotion,
        })


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# ---------------------------------------------------------------------------- input checks


def _parse_buys(baseline_buys: Mapping[object, Mapping[str, float]]) -> dict[date, dict[str, float]]:
    if not isinstance(baseline_buys, Mapping) or not baseline_buys:
        raise BacktestInputError("baseline_buys must be a non-empty mapping of session -> {ticker: usd}")
    out: dict[date, dict[str, float]] = {}
    for raw_day, buys in baseline_buys.items():
        d = parse_day(raw_day, "baseline_buys")
        if d in out:
            raise BacktestInputError(f"baseline_buys: duplicate session {d.isoformat()}")
        if not isinstance(buys, Mapping):
            raise BacktestInputError(f"baseline_buys[{d.isoformat()}] must be a mapping")
        day: dict[str, float] = {}
        for ticker, usd in buys.items():
            if not isinstance(ticker, str) or not ticker:
                raise BacktestInputError(f"baseline_buys[{d.isoformat()}]: bad ticker {ticker!r}")
            value = finite(usd, f"baseline_buys[{d.isoformat()}][{ticker}]")
            if value < 0.0:
                raise BacktestInputError(f"baseline_buys[{d.isoformat()}][{ticker}] is negative")
            if value > 0.0:
                day[ticker] = value
        out[d] = day
    return out


def _parse_events(events_by_ticker: Mapping[str, Sequence[object]] | None) -> dict[str, tuple[object, ...]]:
    if events_by_ticker is None:
        return {}
    if not isinstance(events_by_ticker, Mapping):
        raise BacktestInputError("events_by_ticker must be a mapping")
    out = {}
    for ticker, events in events_by_ticker.items():
        if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
            raise BacktestInputError(f"events for {ticker} must be a sequence")
        for e in events:
            event_known_at(e, f"events[{ticker}]")
        out[ticker] = tuple(events)
    return out


def _check_decision(decision: object, buys: Mapping[str, float], cfg: BacktestConfig) -> CandidateDecision:
    if not isinstance(decision, CandidateDecision):
        raise BacktestInputError(f"candidate returned {type(decision).__name__}, not CandidateDecision")
    for ticker, mult in decision.multipliers.items():
        if ticker not in buys:
            raise BacktestInputError(f"multiplier for {ticker}, which the baseline did not propose")
        value = finite(mult, f"multiplier for {ticker}")
        if not 0.0 <= value <= cfg.max_multiplier:
            raise BacktestInputError(f"multiplier {value} for {ticker} outside [0, {cfg.max_multiplier}]")
    for ticker, score in decision.scores.items():
        finite(score, f"score for {ticker}")
    for ticker, hedge in decision.hedges.items():
        if not (isinstance(hedge, tuple) and len(hedge) == 2 and isinstance(hedge[0], str)):
            raise BacktestInputError(f"hedge for {ticker} must be (benchmark, beta)")
        finite(hedge[1], f"beta for {ticker}")
    return decision


def _shares(usd: float, price: float, cfg: BacktestConfig) -> float:
    """Shares bought for ``usd``. The same function for both arms, so identical dollars
    give bit-identical shares (the zero-signal check relies on it)."""
    if usd <= 0.0:
        return 0.0
    return max(0.0, usd - cfg.per_order_cost_usd) * (1.0 - cfg.buy_cost_bps / 1e4) / price


# ---------------------------------------------------------------------------- replay


def run_backtest(
    candidate: Candidate,
    close_rows: Sequence[Mapping[str, object]],
    baseline_buys: Mapping[object, Mapping[str, float]],
    events_by_ticker: Mapping[str, Sequence[object]] | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay ``candidate`` against the baseline. Raises BacktestInputError on malformed
    inputs (fail closed before any decision); returns a ``rejected`` result if the candidate
    breaks the point-in-time rule or the decision contract."""
    cfg = config or BacktestConfig()
    cfg.check()
    index = PriceIndex(close_rows)
    buys_by_day = _parse_buys(baseline_buys)
    events = _parse_events(events_by_ticker)
    name = str(getattr(candidate, "name", type(candidate).__name__))

    fills: list[m.FillRecord] = []
    days: list[DayRecord] = []
    for session in sorted(buys_by_day):
        buys = buys_by_day[session]
        view = PointInTimeView(index, events, session)
        try:
            decision = candidate.decide(view, dict(buys))
        except Exception as exc:  # noqa: BLE001 -- a candidate failure is the live fallback, counted
            decision = CandidateDecision(available=False, reason=f"candidate_error:{type(exc).__name__}")
        if view.violations:
            return BacktestResult(name, "rejected", f"lookahead_access: {view.violations[0]}", cfg)
        try:
            decision = _check_decision(decision, buys, cfg)
        except BacktestInputError as exc:
            return BacktestResult(name, "rejected", f"invalid_decision on {session.isoformat()}: {exc}", cfg)
        mults = decision.multipliers if decision.available else {}
        n_unfilled = 0
        net = base_total = 0.0
        for ticker in sorted(buys):
            price = index.close_on(ticker, session)
            if price is None:
                n_unfilled += 1  # no close on the fill session: neither arm buys
                continue
            base_usd = buys[ticker]
            mult = float(mults.get(ticker, 1.0))
            cand_usd = base_usd * mult
            fills.append(m.FillRecord(
                session=session, ticker=ticker, price=price,
                base_usd=base_usd, cand_usd=cand_usd,
                base_shares=_shares(base_usd, price, cfg), cand_shares=_shares(cand_usd, price, cfg),
                multiplier=mult,
                score=decision.scores.get(ticker) if decision.available else None,
                hedge=decision.hedges.get(ticker) if decision.available else None,
            ))
            net += cand_usd - base_usd
            base_total += base_usd
        days.append(DayRecord(session, decision.available, decision.reason, len(buys) - n_unfilled,
                              n_unfilled, net, base_total))

    active = _active_returns(fills, index)
    result_metrics, flags = _score(fills, days, active, index, cfg)
    promotion = _promotion(result_metrics, flags, cfg)
    return BacktestResult(name, "ok", "ok", cfg, tuple(fills), tuple(days), tuple(active),
                          result_metrics, tuple(flags), promotion)


def _active_returns(fills: Sequence[m.FillRecord], index: PriceIndex) -> list[tuple[date, float, float]]:
    """Daily time-weighted returns of each arm's holdings: holdings after the previous
    session's fills, valued at this session's close against the previous one (prices carried
    forward over a ticker's missing sessions). New cash at today's close earns nothing today."""
    if not fills:
        return []
    by_day: dict[date, list[m.FillRecord]] = {}
    for f in fills:
        by_day.setdefault(f.session, []).append(f)
    first = min(by_day)
    hold_b: dict[str, float] = {}
    hold_c: dict[str, float] = {}
    last_price: dict[str, float] = {}
    out: list[tuple[date, float, float]] = []
    for session in index.sessions:
        if session < first:
            continue
        if hold_b:
            prev_b = math.fsum(h * last_price[t] for t, h in hold_b.items())
            prev_c = math.fsum(h * last_price[t] for t, h in hold_c.items())
            for t in hold_b:
                p = index.close_on(t, session)
                if p is not None:
                    last_price[t] = p
            cur_b = math.fsum(h * last_price[t] for t, h in hold_b.items())
            cur_c = math.fsum(h * last_price[t] for t, h in hold_c.items())
            if prev_b > 0.0 and prev_c > 0.0:
                out.append((session, cur_b / prev_b - 1.0, cur_c / prev_c - 1.0))
        for f in by_day.get(session, ()):
            hold_b[f.ticker] = hold_b.get(f.ticker, 0.0) + f.base_shares
            hold_c[f.ticker] = hold_c.get(f.ticker, 0.0) + f.cand_shares
            last_price[f.ticker] = f.price
    return out


def _period_metrics(fills: Sequence[m.FillRecord], active: Sequence[tuple[date, float, float]],
                    index: PriceIndex, cfg: BacktestConfig) -> dict[str, object]:
    end = index.sessions[-1]
    diffs = [rc - rb for _, rb, rc in active]
    out: dict[str, object] = {
        "n_fills": len(fills),
        "n_sessions": len({f.session for f in fills}),
        "cost_basis": m.cost_basis(fills),
        "terminal_wealth_per_dollar": m.terminal_wealth(fills, index, end),
        "tilt_forward_return": {
            **{str(h): m.tilt_forward_return(fills, index, h, end, cfg.buy_cost_bps) for h in cfg.horizons},
            "terminal": m.tilt_forward_return(fills, index, None, end, cfg.buy_cost_bps),
        },
        "hit_rate": {str(h): m.hit_rate(fills, index, h) for h in cfg.horizons},
        "turnover": m.turnover(fills),
        "score_buckets": {
            str(h): m.score_buckets(m.forward_residuals(fills, index, h), cfg.score_edges,
                                    cfg.min_bucket_count, cfg.score_sign)
            for h in cfg.horizons
        },
        "active_daily_sharpe": sharpe(diffs),
        "active_n_days": len(diffs),
        "worst_rolling_underperformance_bp": m.worst_rolling(active, cfg.rolling_window_sessions),
    }
    return out


def _score(fills, days, active, index, cfg):
    periods: dict[str, tuple[list[m.FillRecord], list]] = {"all": (list(fills), list(active))}
    if cfg.oos_start is not None:
        periods["in_sample"] = (m.select(fills, lambda d: d < cfg.oos_start),
                                [a for a in active if a[0] < cfg.oos_start])
        periods["out_of_sample"] = (m.select(fills, lambda d: d >= cfg.oos_start),
                                    [a for a in active if a[0] >= cfg.oos_start])
    by_period = {name: _period_metrics(f, a, index, cfg) for name, (f, a) in periods.items()}

    unavailable = Counter(d.reason for d in days if not d.available)
    tol = 1e-9
    max_net = max((abs(d.net_tilt_usd) for d in days), default=0.0)
    neutral = all(abs(d.net_tilt_usd) <= tol * max(1.0, d.baseline_usd) for d in days)
    ic = m.lookahead_ic(fills, index)
    flags = []
    for name, stats in ic.items():
        mean_ic, n_days = stats["mean_ic"], stats["n_days"]
        if mean_ic is not None and n_days >= cfg.lookahead_min_days and abs(mean_ic) >= cfg.lookahead_ic_threshold:
            flags.append(f"suspected_lookahead:{name} mean IC {mean_ic:.3f} over {n_days} sessions "
                         f"(threshold {cfg.lookahead_ic_threshold})")
    summary = {
        "periods": by_period,
        "n_decision_sessions": len(days),
        "unavailable_sessions": dict(sorted(unavailable.items())),
        "unfilled_orders": sum(d.n_unfilled for d in days),
        "budget_neutral": neutral,
        "max_abs_net_tilt_usd": max_net,
        "lookahead_ic": ic,
        "survivor_biased": not cfg.universe_point_in_time,
    }
    return summary, flags


def _promotion(summary: Mapping[str, object], flags: Sequence[str], cfg: BacktestConfig) -> dict[str, object]:
    """The report's promotion test: out of sample, forward residual returns are strictly
    monotone across score buckets in the expected direction, at every horizon. Necessary,
    not sufficient -- the overfitting guards (compare_variants) and the operator still
    decide. Nothing reads this to change sizing."""
    reasons: list[str] = []
    if flags:
        reasons.append("lookahead canary tripped")
    oos = summary["periods"].get("out_of_sample") if isinstance(summary.get("periods"), Mapping) else None
    by_h: dict[str, object] = {}
    if oos is None:
        reasons.append("no out-of-sample period (set oos_start)")
    else:
        for h, b in oos["score_buckets"].items():
            by_h[h] = b["monotone"]
            if b["monotone"] is None:
                reasons.append(f"h={h}: a bucket has fewer than {cfg.min_bucket_count} points")
            elif not b["monotone"]:
                reasons.append(f"h={h}: bucket means not monotone")
    if summary.get("survivor_biased"):
        reasons.append("note: universe is not point-in-time (survivor-biased)")
    passed = not [r for r in reasons if not r.startswith("note:")]
    return {"passed": passed, "monotone_by_horizon": by_h, "reasons": reasons}
