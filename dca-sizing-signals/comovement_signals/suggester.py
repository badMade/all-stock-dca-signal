"""SizingSuggester-conforming wrappers for co-movement signals 1 and 2.

Each wrapper:
  * structurally satisfies dca_shared_interfaces.forecasting.SizingSuggester (one public
    ``suggest(ticker, asof)``; no write, no apply, no live-engine parameter anywhere);
  * gets every input from an injected ``load_inputs(asof)`` callable, so prices, proposed
    buys and day returns all come from the caller and this package makes no network or
    broker call of its own;
  * fails closed (ADR rule 3): ``evaluate()`` returns ``ok=False`` with an explicit
    reason, and no suggestions, on any missing, stale or malformed input or failed check,
    and ``suggest()`` raises ComovementUnavailable carrying that reason rather than
    inventing a neutral number. Publishing a failed outcome writes nothing (publish.py).

Signal 1 and signal 2 are separate classes with separate sources and separate output
files (ADR rule 2), even though they share the input loading and clustering steps below.
Neither ever reads the other's output.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from comovement_signals.clustering import DEFAULT_CUT, Cluster, build_clusters
from comovement_signals.returns import (
    DEFAULT_MIN_SESSIONS,
    DEFAULT_WINDOW_SESSIONS,
    ComovementInputError,
    ReturnsMatrix,
    returns_matrix,
)
from comovement_signals.signals import (
    DEFAULT_CAP_MIN_CLUSTER_SIZE,
    DEFAULT_CAP_SHARE,
    DEFAULT_DROP_THRESHOLD,
    DEFAULT_FLOOR,
    DEFAULT_SELLOFF_MIN_CLUSTER_SIZE,
    DEFAULT_SHARE_DOWN,
    cluster_cap_suggestions,
    correlated_selloff,
)
from dca_shared_interfaces.forecasting import SizingSuggestion

SIGNAL_CLUSTER_CAP = "cluster_cap"
SIGNAL_SELLOFF = "correlated_selloff"
SOURCE_PREFIX = "comovement_signals."

# These signals are rules, not fitted models, and the Phase 0 ledger has not scored them
# yet, so there is no real error metric. SizingSuggestion requires a float, so 0.0 is a
# placeholder that is NEVER written as a claim: the published file records mae = null
# with status "unscored_rule_based" (see publish.py).
UNSCORED_MAE = 0.0


class ComovementUnavailable(RuntimeError):
    """No suggestion can be made. ``reason`` is the fail-closed reason code."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


@dataclass(frozen=True)
class ComovementConfig:
    """Every threshold, defaulting to the ADR's proposed defaults.

    ``max_price_staleness_days`` is calendar days between the newest close used and the
    as-of date. 4 covers a Friday close read on a Tuesday after a Monday holiday; at the
    3:30 PM slot the newest daily close is normally yesterday's.
    ``min_clustered_tickers``: fewer eligible tickers than this means clustering had
    nothing to work with, which is a failed check, not "no cap needed".
    """

    window_sessions: int = DEFAULT_WINDOW_SESSIONS
    min_sessions: int = DEFAULT_MIN_SESSIONS
    min_overlap_sessions: int = DEFAULT_MIN_SESSIONS
    cut: float = DEFAULT_CUT
    cap_share: float = DEFAULT_CAP_SHARE
    floor: float = DEFAULT_FLOOR
    cap_min_cluster_size: int = DEFAULT_CAP_MIN_CLUSTER_SIZE
    selloff_min_cluster_size: int = DEFAULT_SELLOFF_MIN_CLUSTER_SIZE
    share_down: float = DEFAULT_SHARE_DOWN
    drop_threshold: float = DEFAULT_DROP_THRESHOLD
    max_price_staleness_days: int = 4
    min_clustered_tickers: int = 2


@dataclass(frozen=True)
class ComovementInputs:
    """Everything one evaluation reads, supplied by the caller.

    Attributes:
        close_rows: daily close rows (see returns.returns_matrix for the row shape).
        proposed_buys_usd_by_ticker: the day's proposed dollars per ticker, escalation
            included.
        day_budget_usd: the day's budget (signal 1's cap is a share of it).
        day_returns_by_ticker: simple return on the day per ticker, -0.02 = down 2%
            (signal 2 only).
        day_returns_session: the session those day returns belong to; must equal the
            as-of date (signal 2 only).
        escalation_usd_by_ticker: the part of each proposed buy that is dip escalation
            above the ticker's base tier (signal 2 only). Must not exceed the proposed buy.
    """

    close_rows: Sequence[Mapping[str, object]]
    proposed_buys_usd_by_ticker: Mapping[str, float]
    day_budget_usd: float
    day_returns_by_ticker: Mapping[str, float] = field(default_factory=dict)
    day_returns_session: date | None = None
    escalation_usd_by_ticker: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ComovementOutcome:
    """The result of one evaluation. ``ok`` False means publish nothing."""

    signal: str
    as_of: date
    ok: bool
    reason: str
    suggestions: tuple[SizingSuggestion, ...] = ()
    clusters: tuple[Cluster, ...] = ()
    detail: Mapping[str, object] = field(default_factory=dict)


class _Failed(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _Failed("invalid_input", f"{name} must be a finite number, got {value!r}")
    return float(value)


class _ComovementSuggester:
    """Shared fail-closed pipeline. Subclasses add only their signal's own step."""

    signal: str = ""
    default_model_version: str = ""

    def __init__(
        self,
        load_inputs: Callable[[date], ComovementInputs],
        config: ComovementConfig | None = None,
        model_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._load_inputs = load_inputs
        self._config = config or ComovementConfig()
        self._model_version = model_version or self.default_model_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # suggest() is called once per ticker; clustering once per as-of date is enough.
        self._cache: tuple[date, ComovementOutcome] | None = None

    @property
    def source(self) -> str:
        return SOURCE_PREFIX + self.signal

    # ------------------------------------------------------------------ public API

    def suggest(self, ticker: str, asof: date) -> SizingSuggestion:
        """One ticker's suggestion for ``asof``.

        Raises:
            ComovementUnavailable: when the evaluation failed closed (with its reason),
                or with reason ``ticker_not_proposed`` for a ticker with no proposed buy.
        """
        outcome = self.evaluate(asof)
        if not outcome.ok:
            raise ComovementUnavailable(outcome.reason, str(outcome.detail.get("message", "")))
        for s in outcome.suggestions:
            if s.ticker == ticker:
                return s
        raise ComovementUnavailable("ticker_not_proposed", ticker)

    def evaluate(self, asof: date) -> ComovementOutcome:
        """Run the whole pipeline for ``asof``. Never raises for bad inputs."""
        if self._cache is not None and self._cache[0] == asof:
            return self._cache[1]
        try:
            outcome = self._evaluate(asof)
        except _Failed as failed:
            outcome = ComovementOutcome(self.signal, asof, False, failed.reason,
                                        detail={"message": str(failed)})
        self._cache = (asof, outcome)
        return outcome

    # ------------------------------------------------------------------ pipeline

    def _evaluate(self, asof: date) -> ComovementOutcome:
        cfg = self._config
        try:
            inputs = self._load_inputs(asof)
        except Exception as exc:  # noqa: BLE001 -- any loader failure must fail closed, not crash
            raise _Failed("input_load_failed", f"{type(exc).__name__}: {exc}") from exc

        buys = {t: _number(f"proposed buy for {t}", v) for t, v in inputs.proposed_buys_usd_by_ticker.items()}
        if not buys:
            raise _Failed("missing_proposed_buys", "no proposed buys for the day")
        if any(v < 0.0 for v in buys.values()):
            raise _Failed("invalid_input", "a proposed buy is negative")
        budget = _number("day_budget_usd", inputs.day_budget_usd)
        if budget <= 0.0:
            raise _Failed("invalid_input", "day_budget_usd must be positive")
        if not inputs.close_rows:
            raise _Failed("missing_prices", "no close rows supplied")

        try:
            matrix = returns_matrix(inputs.close_rows, asof, cfg.window_sessions, cfg.min_sessions)
        except ComovementInputError as exc:
            raise _Failed("invalid_input", str(exc)) from exc
        self._check_freshness(matrix, asof)
        missing = sorted(t for t in buys if not matrix.returns.get(t) and t not in matrix.singletons)
        if missing:
            raise _Failed("missing_ticker_history", f"no usable closes for {missing}")
        clusters = self._cluster(matrix)

        try:
            multipliers, detail = self._signal(clusters, buys, budget, inputs, asof)
        except ValueError as exc:
            raise _Failed("invalid_input", str(exc)) from exc

        now = self._clock()
        suggestions = []
        for t in sorted(buys):
            m = multipliers.get(t, 1.0)
            if not (math.isfinite(m) and cfg.floor <= m <= 1.0):
                raise _Failed("failed_check", f"multiplier {m!r} for {t} outside [{cfg.floor}, 1.0]")
            suggestions.append(SizingSuggestion(
                ticker=t,
                asof=asof,
                multiplier_adjustment_pct=0.0 if m == 1.0 else (m - 1.0) * 100.0,
                eval_metric_mae=UNSCORED_MAE,
                source=self.source,
                model_version=self._model_version,
                generated_at=now,
            ))
        detail = {
            "clusters": [list(c) for c in clusters],
            "latest_price_date": matrix.latest_price_date.isoformat() if matrix.latest_price_date else None,
            "return_sessions_used": len(matrix.sessions),
            "short_history_singletons": list(matrix.singletons),
            "config": asdict(cfg),
            **detail,
        }
        return ComovementOutcome(self.signal, asof, True, "ok", tuple(suggestions), clusters, detail)

    def _check_freshness(self, matrix: ReturnsMatrix, asof: date) -> None:
        if matrix.latest_price_date is None:
            raise _Failed("missing_prices", f"no non-interpolated close on or before {asof.isoformat()}")
        age = (asof - matrix.latest_price_date).days
        if age > self._config.max_price_staleness_days:
            raise _Failed("stale_prices", f"newest close {matrix.latest_price_date.isoformat()} is {age} days old")

    def _cluster(self, matrix: ReturnsMatrix) -> tuple[Cluster, ...]:
        cfg = self._config
        if len(matrix.eligible) < cfg.min_clustered_tickers:
            raise _Failed("failed_check", f"only {len(matrix.eligible)} tickers have {cfg.min_sessions}+ returns")
        clusters = build_clusters(matrix, cut=cfg.cut, min_overlap=cfg.min_overlap_sessions)
        # Self-check: the clusters must be an exact partition of every ticker with history.
        flat = [t for c in clusters for t in c]
        if len(flat) != len(set(flat)) or set(flat) != set(matrix.eligible) | set(matrix.singletons):
            raise _Failed("failed_check", "clusters are not a partition of the priced tickers")
        return clusters

    def _signal(self, clusters, buys, budget, inputs, asof):  # pragma: no cover - abstract
        raise NotImplementedError


class ClusterCapSuggester(_ComovementSuggester):
    """Signal 1: cluster concentration cap. Reads prices, proposed buys and the budget only."""

    signal = SIGNAL_CLUSTER_CAP
    default_model_version = "comovement-cluster-cap-v1"

    def _signal(self, clusters, buys, budget, inputs, asof):
        cfg = self._config
        res = cluster_cap_suggestions(clusters, buys, budget, cap_share=cfg.cap_share, floor=cfg.floor,
                                      min_cluster_size=cfg.cap_min_cluster_size)
        detail = {
            "day_budget_usd": budget,
            "cap_usd": res.cap_usd,
            "capped_clusters": [
                {"members": list(c.members), "proposed_usd": c.proposed_usd, "capped_usd": c.capped_usd,
                 "multiplier": c.multiplier, "floor_bound": c.floor_bound}
                for c in res.clusters
            ],
        }
        return dict(res.multipliers), detail


class CorrelatedSelloffSuggester(_ComovementSuggester):
    """Signal 2: correlated selloff detector. Also needs the day's returns and escalations.

    The per-ticker multiplier turns the escalation cap back into a scale on the whole
    proposed buy: (proposed - escalation + escalation_cap) / proposed, never below the
    floor. A ticker outside a marked cluster stays at 1.0.
    """

    signal = SIGNAL_SELLOFF
    default_model_version = "comovement-correlated-selloff-v1"

    def _signal(self, clusters, buys, budget, inputs, asof):
        cfg = self._config
        if not inputs.day_returns_by_ticker:
            raise _Failed("missing_day_returns", "no day returns supplied")
        if inputs.day_returns_session != asof:
            raise _Failed("stale_day_returns",
                          f"day returns are for {inputs.day_returns_session}, not {asof.isoformat()}")
        escalations = {t: _number(f"escalation for {t}", v) for t, v in inputs.escalation_usd_by_ticker.items()}
        for t, e in escalations.items():
            if t not in buys:
                raise _Failed("invalid_input", f"escalation for {t}, which has no proposed buy")
            if e > buys[t]:
                raise _Failed("invalid_input", f"escalation {e} for {t} exceeds its proposed buy {buys[t]}")
        res = correlated_selloff(clusters, inputs.day_returns_by_ticker, escalations,
                                 min_cluster_size=cfg.selloff_min_cluster_size, share_down=cfg.share_down,
                                 drop_threshold=cfg.drop_threshold)
        multipliers: dict[str, float] = {}
        floor_bound: list[str] = []
        for t, cap in res.escalation_caps_usd.items():
            proposed = buys[t]
            if proposed <= 0.0:
                continue
            raw = (proposed - escalations[t] + cap) / proposed
            if raw < cfg.floor:
                floor_bound.append(t)
            multipliers[t] = min(1.0, max(cfg.floor, raw))
        detail = {
            "day_returns_session": asof.isoformat(),
            "universe_flag": res.universe_flag,
            "universe_scored": res.universe_scored,
            "universe_down": res.universe_down,
            "marked_clusters": [
                {"members": list(c.members), "scored": c.scored, "down": c.down, "share_down": c.share_down,
                 "escalation_total_usd": c.escalation_total_usd, "escalation_budget_usd": c.escalation_budget_usd}
                for c in res.marked_clusters
            ],
            "escalation_caps_usd": dict(sorted(res.escalation_caps_usd.items())),
            "floor_bound_tickers": sorted(floor_bound),
        }
        return multipliers, detail
