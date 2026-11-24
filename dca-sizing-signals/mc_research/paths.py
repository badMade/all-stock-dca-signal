"""Path-first simulation: generate every path once, then run every strategy over the same set.

Strategies never touch a random generator. They are pure functions of an already-generated
``PathSet``, so two ladder variants cannot drift onto different paths however differently they
branch -- common random numbers hold by construction, not by care.

Each path draws from its own named stream (``("market", "path", i)``, or ``("market", "pair", j)``
for antithetic pairs), so path i is the same whether the study asks for 100 paths or 10,000.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mc_research.rng import RandomStreams


@dataclass(frozen=True)
class PathSet:
    """Immutable simulated price paths. ``prices[i]`` has ``n_steps + 1`` points, ``prices[i][0] == s0``.

    ``antithetic`` means paths ``2j`` and ``2j + 1`` are mirror images (shock z and -z) and must be
    estimated with ``estimators.antithetic``. ``terminal_mean`` is the exact model mean of the final
    price, available as a control-variate mean.
    """

    prices: tuple[tuple[float, ...], ...]
    model: str
    root_seed: int
    dt: float
    antithetic: bool
    terminal_mean: float

    @property
    def n_paths(self) -> int:
        return len(self.prices)

    @property
    def n_steps(self) -> int:
        return len(self.prices[0]) - 1 if self.prices else 0

    def terminal_prices(self) -> tuple[float, ...]:
        return tuple(path[-1] for path in self.prices)


def _gbm_path(s0: float, drift: float, vol: float, shocks: list[float]) -> tuple[float, ...]:
    log_price = math.log(s0)
    out = [s0]
    for z in shocks:
        log_price += drift + vol * z
        out.append(math.exp(log_price))
    return tuple(out)


def gbm_paths(
    *,
    s0: float,
    mu: float,
    sigma: float,
    n_steps: int,
    n_paths: int,
    dt: float,
    root_seed: int,
    antithetic: bool = False,
) -> PathSet:
    """Geometric Brownian motion, exact log-normal steps. ``mu`` and ``sigma`` are annualised.

    The analytic sanity generator only: GBM ignores heavy tails and volatility clustering (Cont
    2001), and under it a sizing rule keyed on today's move has no expected-return effect except
    through cash timing. Any ladder "edge" GBM shows is an artefact; real studies also need a
    regime-switching and a block-bootstrap generator.
    """
    for name, value in (("s0", s0), ("sigma", sigma), ("dt", dt)):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be positive and finite, got {value!r}")
    if not math.isfinite(mu):
        raise ValueError(f"mu must be finite, got {mu!r}")
    if n_steps < 1 or n_paths < 1:
        raise ValueError(f"need n_steps >= 1 and n_paths >= 1, got {n_steps}, {n_paths}")
    if antithetic and n_paths % 2:
        raise ValueError(f"antithetic paths come in pairs; n_paths must be even, got {n_paths}")

    streams = RandomStreams(root_seed).child("market")
    drift = (mu - 0.5 * sigma * sigma) * dt
    vol = sigma * math.sqrt(dt)
    prices: list[tuple[float, ...]] = []
    if antithetic:
        for j in range(n_paths // 2):
            rng = streams.stream("pair", j)
            shocks = [rng.gauss(0.0, 1.0) for _ in range(n_steps)]
            prices.append(_gbm_path(s0, drift, vol, shocks))
            prices.append(_gbm_path(s0, drift, vol, [-z for z in shocks]))
    else:
        for i in range(n_paths):
            rng = streams.stream("path", i)
            prices.append(_gbm_path(s0, drift, vol, [rng.gauss(0.0, 1.0) for _ in range(n_steps)]))
    return PathSet(
        prices=tuple(prices),
        model="gbm",
        root_seed=root_seed,
        dt=dt,
        antithetic=antithetic,
        terminal_mean=s0 * math.exp(mu * dt * n_steps),
    )
