"""Reservoir replay buffer of (ticker, origin_date) REFERENCES.

The anti-catastrophic-forgetting half of the octopus's online updates: each
daily fine-tune batch is 50% recent days and 50% draws from this buffer,
which reservoir-samples (classic Vitter Algorithm R) over every window the
model has ever trained on -- so old regimes stay represented in proportion
to their share of history no matter how long the run gets.

The buffer stores REFERENCES only, never tensors: (ticker, origin ISO date)
pairs are re-materialized into feature windows from the current panel at
sample time. That keeps the checkpoint a small JSON list, and it makes the
buffer survive live-mode panel rebuilds (row indices shift; dates don't).

Determinism: the reservoir's RNG state round-trips through to_state() /
from_state(), so a resumed run admits and evicts exactly as the
uninterrupted run would have.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class ReservoirBuffer:
    def __init__(self, capacity: int, seed: int) -> None:
        self.capacity = capacity
        self.items: list[list[str]] = []  # [ticker, origin_date]
        self.n_seen = 0
        self._rng = np.random.default_rng(seed)

    def add(self, ticker: str, origin_date: str) -> None:
        """Vitter Algorithm R: uniform over everything ever offered."""
        self.n_seen += 1
        if len(self.items) < self.capacity:
            self.items.append([ticker, origin_date])
            return
        slot = int(self._rng.integers(0, self.n_seen))
        if slot < self.capacity:
            self.items[slot] = [ticker, origin_date]

    def sample(self, count: int) -> list[tuple[str, str]]:
        if not self.items:
            return []
        count = min(count, len(self.items))
        chosen = self._rng.choice(len(self.items), size=count, replace=False)
        return [(self.items[index][0], self.items[index][1]) for index in sorted(chosen)]

    def __len__(self) -> int:
        return len(self.items)

    def to_state(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "items": self.items,
            "n_seen": self.n_seen,
            "rng_state": self._rng.bit_generator.state,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "ReservoirBuffer":
        buffer = cls(capacity=int(state["capacity"]), seed=0)
        buffer.items = [list(item) for item in state["items"]]
        buffer.n_seen = int(state["n_seen"])
        buffer._rng.bit_generator.state = state["rng_state"]
        return buffer
