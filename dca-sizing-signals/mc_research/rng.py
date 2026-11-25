"""Deterministic, named random streams: the stdlib stand-in for ``numpy.random.SeedSequence.spawn``.

Common random numbers (CRN) only survive when every random *source* (daily return, intraday path,
regime switch, fill slippage) draws from its own stream. If one stream feeds everything, a variant
whose tier branch consumes one extra draw shifts every later draw, and the two variants silently
end up on different paths -- the "CRN desynchronization" pitfall in docs/mc_variance_reduction.md.

So a stream is addressed by a *key path* (``("market", "path", 17)``), and its seed is a hash of
the root seed plus that path. Adding path 18, or a new ``("slippage", ...)`` source, never moves the
draws of ``("market", "path", 17)``. SHA-256 truncated to 128 bits gives the same collision story
as SeedSequence's 128-bit pool (about n^2 * 2^-128 for n streams).

Stdlib only (hashlib + random); no numpy, so this package adds no dependency to dca-sizing-signals.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

KeyPart = str | int


def _encode(part: KeyPart) -> str:
    # bool is an int subclass; reject it so True and 1 can never alias the same stream.
    if isinstance(part, bool) or not isinstance(part, (str, int)):
        raise TypeError(f"stream key parts must be str or int, got {type(part).__name__}")
    # Type-tagged so the string "1" and the int 1 are different streams.
    return f"{'s' if isinstance(part, str) else 'i'}:{part}"


def derive_seed(root_seed: int, *key: KeyPart) -> int:
    """128-bit seed for the stream at ``key`` under ``root_seed``. Pure and stable across runs."""
    if isinstance(root_seed, bool) or not isinstance(root_seed, int) or root_seed < 0:
        raise ValueError(f"root_seed must be a non-negative int, got {root_seed!r}")
    material = "\x1f".join([str(root_seed), *(_encode(part) for part in key)])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:16], "big")


@dataclass(frozen=True)
class RandomStreams:
    """A root seed plus a key prefix. ``stream(*key)`` hands out an independent ``random.Random``.

    Log ``root_seed`` with every study result: it is the whole of the reproducibility contract.
    """

    root_seed: int
    prefix: tuple[KeyPart, ...] = ()

    def __post_init__(self) -> None:
        derive_seed(self.root_seed, *self.prefix)  # validate eagerly, fail closed on a bad seed/key

    def child(self, *key: KeyPart) -> RandomStreams:
        """A sub-tree, e.g. one per worker or per random source."""
        return RandomStreams(self.root_seed, self.prefix + tuple(key))

    def stream(self, *key: KeyPart) -> random.Random:
        """A fresh generator for this key. Same key, same draws -- every time."""
        return random.Random(derive_seed(self.root_seed, *self.prefix, *key))
