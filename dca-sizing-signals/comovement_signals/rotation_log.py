"""Signal 5, staples vs semis rotation -- LOG ONLY.

docs/adr-comovement-signals.md: a weak regime indicator logged for research. The
2026-11-02 research saw staples names run at about -0.33 correlation against semis over
one AI/semis-led regime (about 11% of variance, untested in a broad selloff), so this is
context, not a signal anything acts on (report: 25% confidence). Nothing reads the log
and this module is not a SizingSuggester.

    regime(t)        = mean daily log return of the staples group on t
                       - mean daily log return of the semis group on t
    rolling_mean(t)  = mean of regime over the last ``rolling_sessions`` sessions (20)

Positive means staples beat semis that day (risk-off tilt); 20 x rolling_mean is the
20-session cumulative log-return gap.

The groups are always supplied by the caller. The module names no ticker, so the
research's group choice cannot quietly become a hard-coded universe here.

Fail closed: overlapping or empty groups, malformed or stale prices, fewer sessions than
the rolling window, or any session in the window where a group falls short of
``min_group_coverage`` gives no record and a reason. Log returns come from
returns_matrix(), so no row after ``as_of`` is ever read.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from comovement_signals.shadow import (
    DEFAULT_MAX_PRICE_STALENESS_DAYS,
    SIGNAL_ROTATION_LOG,
    ShadowRecord,
    ShadowResult,
    ShadowUnavailable,
    finite,
    fresh_matrix,
)


@dataclass(frozen=True)
class RotationConfig:
    """``min_group_coverage`` is the share of each group that must have a return on a
    session; the default 1.0 means every member, so a missing name never silently turns
    the group mean into a different basket."""

    rolling_sessions: int = 20
    min_group_coverage: float = 1.0
    max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS


def _group_mean(day_returns: Mapping[str, float], group: Sequence[str], min_coverage: float) -> float | None:
    present = [day_returns[t] for t in group if t in day_returns and finite(day_returns[t])]
    if not present or len(present) < math.ceil(min_coverage * len(group)):
        return None
    return math.fsum(present) / len(present)


def regime_value(
    day_returns: Mapping[str, float],
    staples: Sequence[str],
    semis: Sequence[str],
    min_coverage: float = 1.0,
) -> float | None:
    """Staples mean return minus semis mean return for one session, or None when either
    group is short of ``min_coverage``."""
    s = _group_mean(day_returns, staples, min_coverage)
    m = _group_mean(day_returns, semis, min_coverage)
    if s is None or m is None:
        return None
    return s - m


def rotation_log_shadow(
    close_rows: Iterable[Mapping[str, object]],
    as_of: date,
    staples: Iterable[str],
    semis: Iterable[str],
    config: RotationConfig | None = None,
) -> ShadowResult:
    """The rotation log record for the newest usable session (before ``as_of``, or on it
    when its bars are marked ``final``)."""
    config = config if config is not None else RotationConfig()
    staples_t = tuple(sorted(set(staples)))
    semis_t = tuple(sorted(set(semis)))
    if not staples_t or not semis_t or set(staples_t) & set(semis_t):
        return ShadowResult(SIGNAL_ROTATION_LOG, as_of, False, "invalid_groups")
    try:
        matrix = fresh_matrix(close_rows, as_of, config.rolling_sessions, 1, config.max_price_staleness_days)
    except ShadowUnavailable as failed:
        return ShadowResult(SIGNAL_ROTATION_LOG, as_of, False, failed.reason)
    if len(matrix.sessions) < config.rolling_sessions:
        return ShadowResult(SIGNAL_ROTATION_LOG, as_of, False, "insufficient_history")

    values: list[float] = []
    for session in matrix.sessions:
        day_returns = {t: series[session] for t, series in matrix.returns.items() if session in series}
        v = regime_value(day_returns, staples_t, semis_t, config.min_group_coverage)
        if v is None:
            return ShadowResult(SIGNAL_ROTATION_LOG, as_of, False, "incomplete_group")
        values.append(v)

    newest = matrix.sessions[-1]
    newest_returns = {t: series[newest] for t, series in matrix.returns.items() if newest in series}
    payload = {
        "session": newest.isoformat(),
        "staples": list(staples_t),
        "semis": list(semis_t),
        "staples_mean": _group_mean(newest_returns, staples_t, config.min_group_coverage),
        "semis_mean": _group_mean(newest_returns, semis_t, config.min_group_coverage),
        "regime": values[-1],
        "rolling_mean": math.fsum(values) / len(values),
        "rolling_sessions": config.rolling_sessions,
        "return_kind": "log",
        "live_effect": "none",
    }
    record = ShadowRecord(SIGNAL_ROTATION_LOG, as_of, newest, payload)
    return ShadowResult(SIGNAL_ROTATION_LOG, as_of, True, "ok", (record,))
