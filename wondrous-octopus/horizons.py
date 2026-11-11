"""Multi-horizon (t+1 .. t+H) target access and the horizon-aware embargo.

The single-horizon system has one leakage rule: a row is trainable once the
NEXT row's date has passed (``label_end_date <= cursor``). Forecasting day
t+h generalizes it to an **embargo of h trading rows**: the horizon-h target
of origin row ``i`` is the bar of row ``i+h``, so it is usable -- for a fit,
an online update, or a score -- only once row ``i+h``'s date is at or before
the revealed-through date (``asof``). Nothing here may ever look at "is the
target finite": the replay panel holds the WHOLE archive, so a future bar
always exists in memory and finiteness proves nothing about whether the
walk-forward cursor has reached it. Everything is therefore expressed against
an explicit ``asof`` date, and :func:`usable_horizon_count` is the single
place that turns an ``asof`` into "how many horizons of this row are knowable".

"t+h" means the ticker's own h-th next trading row, the same convention the
horizon-1 targets use (ragged histories are handled by eligibility, never by
forward-filling), so a ticker's t+5 is five of ITS rows ahead, which on a
ticker with gaps is further than five calendar-market days.

Targets are derived from the stored horizon-1 targets, not stored again for
every h (90x the memory of a full-universe panel)::

    log(X[i+h] / close[i]) = targets[i+h-1, comp] + log(close[i+h-1] / close[i])

where ``targets[j, comp] = log(X[j+1, comp] / close[j])``. For h = 1 the
offset is zero and this is exactly ``targets[i]``.

This module is torch-free so the embargo can be tested and reasoned about on
its own.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
from data import COMPONENTS, TickerSeries

#: Horizon-1 is the legacy single-step system (its own score records,
#: calibrator and heads). A run with ``max_horizon == 1`` is byte-identical to
#: the pre-multi-horizon behavior.
LEGACY_HORIZON = 1
DEFAULT_MAX_HORIZON = 1
#: Evaluation checkpoints: the horizons whose forecasts are matured, scored,
#: ledgered and used to calibrate. Every horizon up to ``max_horizon`` is
#: EMITTED; only these are measured (widen the set as the model earns it).
DEFAULT_EVAL_HORIZONS = (5, 10, 15, 20)
MAX_SUPPORTED_HORIZON = 252

#: The calendar key under which a pending forecast whose first unmatured
#: horizon lies beyond the end of the known calendar waits (live mode: the
#: calendar grows by a day at a time).
FAR_FUTURE_KEY = "9999-12-31"


def default_eval_horizons(max_horizon: int) -> tuple[int, ...]:
    """The default evaluation checkpoints for ``max_horizon`` (empty when 1)."""
    if max_horizon <= LEGACY_HORIZON:
        return ()
    chosen = tuple(h for h in DEFAULT_EVAL_HORIZONS if h <= max_horizon)
    return chosen or (max_horizon,)


def validate_horizons(max_horizon: int, eval_horizons: Sequence[int]) -> tuple[int, tuple[int, ...]]:
    """Normalize and validate the horizon config; raises ``ValueError`` (a refusal).

    ``eval_horizons`` must be sorted-unique integers in ``[2, max_horizon]``
    (horizon 1 is scored by the legacy path, never twice). They must be empty
    exactly when ``max_horizon == 1``.
    """
    if isinstance(max_horizon, bool) or not isinstance(max_horizon, int):
        raise ValueError(f"max_horizon must be an integer, got {max_horizon!r}")
    if not LEGACY_HORIZON <= max_horizon <= MAX_SUPPORTED_HORIZON:
        raise ValueError(f"max_horizon must be in [1, {MAX_SUPPORTED_HORIZON}], got {max_horizon}")
    normalized = tuple(sorted(set(int(h) for h in eval_horizons)))
    if max_horizon == LEGACY_HORIZON:
        if normalized:
            raise ValueError("eval_horizons require max_horizon >= 2")
        return max_horizon, ()
    if not normalized:
        normalized = default_eval_horizons(max_horizon)
    bad = [h for h in normalized if not 2 <= h <= max_horizon]
    if bad:
        raise ValueError(
            f"eval_horizons {bad} must lie in [2, max_horizon={max_horizon}] (horizon 1 is scored natively)"
        )
    return max_horizon, normalized


def _revealed_through_row(series: TickerSeries, asof: Any) -> int:
    """Index of the last row of ``series`` dated on or before ``asof`` (-1 if none)."""
    day_dates = series.dates.astype("datetime64[D]")
    return int(np.searchsorted(day_dates, np.datetime64(asof, "D"), side="right")) - 1


def usable_horizon_count(series: TickerSeries, row: int, asof: Any, max_horizon: int) -> int:
    """How many horizons (1..K) of origin ``row`` are knowable as of ``asof``.

    Horizon h is knowable iff row ``row + h`` exists and its date is at or
    before ``asof`` -- the h-day embargo. Because dates ascend the answer is a
    prefix: horizons 1..K are usable, K+1.. are not. 0 means not even the
    next day has been revealed.
    """
    if asof is None:
        raise ValueError("usable_horizon_count requires an explicit asof date; 'unbounded' is not a safe default")
    last_revealed = _revealed_through_row(series, asof)
    return int(np.clip(last_revealed - row, 0, max_horizon))


def horizon_targets(series: TickerSeries, row: int, max_horizon: int, asof: Any) -> np.ndarray:
    """``(max_horizon, 4)`` log-returns of bar ``row+h`` vs ``row``'s close.

    Entries for embargoed or nonexistent horizons are NaN. ``asof`` is the
    revealed-through date (see module docstring); passing the current
    walk-forward cursor makes it impossible to read a target the cursor has
    not reached.
    """
    out = np.full((max_horizon, len(COMPONENTS)), np.nan, dtype=np.float64)
    usable = usable_horizon_count(series, row, asof, max_horizon)
    if usable <= 0:
        return out
    # Rows row .. row+usable-1 are the "row + h - 1" of h = 1..usable: a
    # contiguous slice, so only that span's closes are ever logged.
    log_close = np.log(series.ref_close[row : row + usable])
    out[:usable] = series.targets[row : row + usable].astype(np.float64) + (log_close - log_close[0])[:, None]
    return out


def horizon_label_date(series: TickerSeries, row: int, horizon: int) -> np.datetime64:
    """The date horizon ``horizon`` of origin ``row`` becomes knowable (NaT if past the end)."""
    position = row + horizon
    if position >= len(series.dates):
        return np.datetime64("NaT", "ns")
    return series.dates[position]


def horizon_embargo_mask(series: TickerSeries, rows: Sequence[int], asof: Any, max_horizon: int) -> np.ndarray:
    """``(len(rows), max_horizon)`` bool: True where the horizon is knowable as of ``asof``."""
    mask = np.zeros((len(rows), max_horizon), dtype=bool)
    for position, row in enumerate(rows):
        mask[position, : usable_horizon_count(series, int(row), asof, max_horizon)] = True
    return mask
