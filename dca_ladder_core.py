"""Ladder and first-allocation base arithmetic, extracted from ``near_close_dca``.

Pure functions over plain data: no repo imports, no I/O, no environment reads, so this one file runs
in a bare interpreter (the Perplexity sandbox build of the plan-first backup executor, see
``docs/adr-plan-first-backup-executor.md``). ``near_close_dca`` keeps its config-aware wrappers and
delegates here, so there is one implementation of the arithmetic.

A non-finite drop (NaN, +/-inf: a NaN or missing price upstream) is fail-closed to 0.0 on both ladders
(``55e0340``): every comparison with NaN is False, so it would otherwise fall through to the top tier,
and +inf would clamp to it.
"""

from __future__ import annotations

import math
from typing import Any, Optional

# base_selection_reason values recorded per signal for the first-allocation base.
BASE_BELOW_CLOSE_AND_LOW = "below_prior_close_and_low"  # full-base scale
BASE_BELOW_CLOSE_ONLY = "below_prior_close_only"  # reduced-base scale
BASE_NOT_BELOW_CLOSE = "not_below_prior_close_no_buy"  # $0, no buy

# Ceiling raised 8.0 -> 10.0 so per-ticker calibration can reach its 96th-100th percentile tiers
# (8.4x-10x) past the global ladder's 8x top tier; the kill-switch cap stays the final word.
MULTIPLIER_CEILING = 10.0
MIN_BASE_USD = 1.0  # a model must never produce a sub-dollar notional


def interpolate_tier_multiplier(drop: float, tiers: Any) -> float:
    """Linear blend between neighbouring ``(drop_fraction, multiplier)`` tier points.

    Reproduces every tier point exactly, clamps at the last tier's value for any drop beyond it, and
    returns 0.0 (no buy) below the first threshold. Rounded to 4 dp so a candidate row's value is
    exactly reproducible by the validators in ``dca_runner._configured_calibration_bounds``. A non-finite
    drop returns 0.0 (fail closed).
    """
    if not math.isfinite(drop):
        return 0.0
    ordered = sorted((float(threshold), float(multiplier)) for threshold, multiplier in tiers)
    if not ordered or drop < ordered[0][0]:
        return 0.0
    if drop >= ordered[-1][0]:
        return ordered[-1][1]
    for (low, low_multiplier), (high, high_multiplier) in zip(ordered, ordered[1:], strict=False):
        if low <= drop < high:
            fraction = (drop - low) / (high - low)
            return round(low_multiplier + (high_multiplier - low_multiplier) * fraction, 4)
    return ordered[-1][1]  # pragma: no cover - unreachable: the bounds above cover every drop


def ladder_multiplier(drop: float, tiers: Any, *, interpolate: bool = False, suppressed: bool = False) -> float:
    """The ladder's own tier match, BEFORE the ML nudge, the ceiling, or the kill-switch cap.

    ``drop`` is a fraction (0.02 == 2.0% down). Stepped by default: the largest threshold less than
    or equal to the drop wins and a drop below the first threshold is 0.0 (no buy). ``interpolate``
    blends between points instead. ``suppressed`` (an inverse-Martingale enrolled ticker) is always 0.0.
    """
    if suppressed:
        return 0.0
    if interpolate:
        return interpolate_tier_multiplier(drop, tiers)
    if not math.isfinite(drop):
        return 0.0  # +inf would otherwise satisfy every threshold and match the top tier
    multiplier = 0.0
    for threshold, tier_multiplier in sorted(tiers):
        if drop >= threshold:
            multiplier = tier_multiplier
    return multiplier


def graded_multiplier(
    drop: float,
    tiers: Any,
    *,
    interpolate: bool = False,
    ml_factor: float = 1.0,
    kill_switch: bool = False,
    suppressed: bool = False,
    ceiling: float = MULTIPLIER_CEILING,
) -> float:
    """Final buy multiplier: tier match, then ML nudge, then ceiling, then kill-switch cap.

    The ML factor applies only to a positive tier match, so a 0x (no-buy) drop stays 0x. The
    kill-switch caps any positive multiplier to 1x and is applied last so it has the final word.
    """
    multiplier = ladder_multiplier(drop, tiers, interpolate=interpolate, suppressed=suppressed)
    if multiplier > 0:
        multiplier = multiplier * ml_factor
    multiplier = min(multiplier, ceiling)
    if kill_switch:
        multiplier = min(multiplier, 1.0)
    return multiplier


def select_base_usd(
    decision_price: float,
    previous_close: float,
    previous_low: Optional[float],
    *,
    full_base: float,
    reduced_base: float,
) -> tuple[float, str]:
    """First-allocation base dollars before the multiplier, and the reason recorded for it.

    Not below the prior close -> $0 (no buy). Below the close but not the prior low -> the reduced
    base. Below both -> the full base. A missing or unusable low is treated as "not below the low"
    so an unavailable low can never upgrade the base. Both bases are floored at $1 first.
    """
    if not (decision_price < previous_close):
        return 0.0, BASE_NOT_BELOW_CLOSE
    full = max(MIN_BASE_USD, float(full_base))
    reduced = max(MIN_BASE_USD, float(reduced_base))
    if (
        previous_low is not None
        and math.isfinite(previous_low)
        and 0 < previous_low <= previous_close
        and decision_price < previous_low
    ):
        return full, BASE_BELOW_CLOSE_AND_LOW
    return reduced, BASE_BELOW_CLOSE_ONLY
