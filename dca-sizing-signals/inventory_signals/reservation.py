"""Pure inventory-reservation math for a long-only, buy-only graded DCA.

Spec: docs/inventory_reservation_suggester.md. Adapted from the Avellaneda-Stoikov (2008)
reservation price r = s - q * gamma * sigma^2 * (T - t), with three changes that matter for
a program that only ever buys:

  * One-sided. Only the bid side exists, so only an OVERWEIGHT name is skewed (a deeper
    discount, a smaller buy). An underweight name gets q = 0, never a negative q: this rule
    may shrink a buy but can never enlarge one beyond the existing tier.
  * q is measured on COST BASIS, not market value. A market-value weight falls when a name
    falls, which would lower q and make the rule average down into a chronic loser; cost
    basis caps the capital a persistent loser can absorb.
  * (T - t) is measured in VARIANCE time: the share of the day's forecast variance still
    ahead of the slot, from a caller-supplied intraday seasonal profile (Andersen and
    Bollerslev 1997, the U shape). At the 15:30 slot only the last interval's share is left,
    so the inventory term almost vanishes -- the "trade like TWAP as time runs out" limit of
    Cartea and Jaimungal -- which keeps the rule from stranding budget at the close.

The formula's own assumptions (Brownian mid, CARA utility, exponential fills) do not hold
for gapping small caps, so this is a shaped, capped penalty, not an optimum. Every function
is stdlib-only and side-effect free; every bad argument raises ValueError so the suggester
above it can fail closed with a reason.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

# The seven live scheduled slots (ET), in order. The 3:30 PM slot is the last one.
SLOTS: tuple[str, ...] = ("09:35", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30")

# Starting priors from the research note, not calibrated results. The validation plan
# allows a 3 x 3 grid over z and kappa and nothing more (overfitting control).
DEFAULT_Z = 1.0
DEFAULT_Q_MAX = 2.0
DEFAULT_GAMMA_M = 1.0
DEFAULT_M_MIN = 0.25
DEFAULT_B0_BPS = 0.0
DEFAULT_KAPPA = 0.5
DEFAULT_CAP_SIGMAS = 2.0


def finite(name: str, value: object) -> float:
    """``value`` as a float, or ValueError. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def inventory_excess(committed_usd: float, target_usd: float, q_max: float = DEFAULT_Q_MAX) -> float:
    """q = clip((C - c*) / c*, 0, q_max): excess cost-basis dollars over target, as a
    fraction of target. 0 at or under target; q_max caps the penalty for a very heavy name.
    """
    committed = finite("committed_usd", committed_usd)
    target = finite("target_usd", target_usd)
    cap = finite("q_max", q_max)
    if committed < 0.0:
        raise ValueError("committed_usd must not be negative")
    if target <= 0.0:
        raise ValueError("target_usd must be positive")
    if cap < 0.0:
        raise ValueError("q_max must not be negative")
    return min(cap, max(0.0, (committed - target) / target))


def normalized_profile(weights: Mapping[str, float]) -> dict[str, float]:
    """Validate a per-slot intraday variance profile and scale it to sum to 1.

    ``weights[s]`` is the share of the day's variance in the interval from slot s to the
    next slot (the last slot's interval runs to the close). Every slot in SLOTS must be
    present, non-negative and finite, and the total must be positive. Extra keys are an
    error: a typo'd slot would otherwise silently drop out of the remaining-variance sum.
    """
    if not isinstance(weights, Mapping):
        raise ValueError("seasonal profile must be a mapping of slot -> weight")
    extra = sorted(set(weights) - set(SLOTS))
    missing = [s for s in SLOTS if s not in weights]
    if extra or missing:
        raise ValueError(f"seasonal profile slots wrong: missing {missing}, unknown {extra}")
    values = {s: finite(f"seasonal weight {s}", weights[s]) for s in SLOTS}
    if any(v < 0.0 for v in values.values()):
        raise ValueError("seasonal weights must not be negative")
    total = sum(values.values())
    if total <= 0.0:
        raise ValueError("seasonal weights must sum to a positive number")
    return {s: v / total for s, v in values.items()}


def remaining_weight(profile: Mapping[str, float], slot: str) -> float:
    """Share of the day's variance from ``slot`` to the close: the sum of the weights of
    ``slot`` and every later slot. ``profile`` must already be normalized."""
    if slot not in SLOTS:
        raise ValueError(f"unknown slot {slot!r}; expected one of {SLOTS}")
    return sum(profile[s] for s in SLOTS[SLOTS.index(slot):])


def median(values: Sequence[float]) -> float:
    """Plain median (no statistics import needed for one call site)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("median of an empty sequence")
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def reservation_discount(
    q: float,
    sigma_daily: float,
    sigma_daily_median: float,
    weight_remaining: float,
    sigma_slot_realized: float,
    *,
    z: float = DEFAULT_Z,
    b0_bps: float = DEFAULT_B0_BPS,
    kappa: float = DEFAULT_KAPPA,
    cap_sigmas: float = DEFAULT_CAP_SIGMAS,
) -> tuple[float, float]:
    """delta = min(cap_sigmas * sigma_d, b0 + kappa * sigma_slot + gamma * q * V), in
    log-return units (0.01 = about 1% below the reference), with gamma = z / median sigma_d
    and V = sigma_d^2 * weight_remaining.

    Calibration of gamma: at q = 1 (twice target) with the whole day ahead (weight 1), the
    median-volatility name needs a discount of exactly z daily standard deviations.

    Returns:
        (delta, V).
    """
    q = finite("q", q)
    sig = finite("sigma_daily", sigma_daily)
    sig_med = finite("sigma_daily_median", sigma_daily_median)
    w_left = finite("weight_remaining", weight_remaining)
    sig_slot = finite("sigma_slot_realized", sigma_slot_realized)
    if sig <= 0.0 or sig_med <= 0.0:
        raise ValueError("daily volatility forecasts must be positive")
    if sig_slot < 0.0:
        raise ValueError("sigma_slot_realized must not be negative")
    if q < 0.0:
        raise ValueError("q must not be negative (underweight names are neutral, not negative)")
    if not 0.0 <= w_left <= 1.0 + 1e-9:
        raise ValueError(f"weight_remaining must be in [0, 1], got {w_left}")
    for name, value in (("z", z), ("b0_bps", b0_bps), ("kappa", kappa), ("cap_sigmas", cap_sigmas)):
        if finite(name, value) < 0.0:
            raise ValueError(f"{name} must not be negative")
    gamma = z / sig_med
    variance_left = sig * sig * min(1.0, w_left)
    raw = b0_bps / 10_000.0 + kappa * sig_slot + gamma * q * variance_left
    return min(cap_sigmas * sig, raw), variance_left


def size_multiplier(q: float, *, gamma_m: float = DEFAULT_GAMMA_M, m_min: float = DEFAULT_M_MIN) -> float:
    """m = max(m_min, 1 / (1 + gamma_m * q)), never above 1.0: a name at twice target
    (q = 1) gets half size with the defaults, and nothing is ever enlarged."""
    q = finite("q", q)
    g = finite("gamma_m", gamma_m)
    floor = finite("m_min", m_min)
    if q < 0.0 or g < 0.0:
        raise ValueError("q and gamma_m must not be negative")
    if not 0.0 < floor <= 1.0:
        raise ValueError("m_min must be in (0, 1]")
    return min(1.0, max(floor, 1.0 / (1.0 + g * q)))


def max_buy_price(reference_price: float, delta: float) -> float:
    """p_max = p_ref * exp(-delta): a same-day top-up is suggested only below this."""
    ref = finite("reference_price", reference_price)
    d = finite("delta", delta)
    if ref <= 0.0:
        raise ValueError("reference_price must be positive")
    if d < 0.0:
        raise ValueError("delta must not be negative")
    return ref * math.exp(-d)
