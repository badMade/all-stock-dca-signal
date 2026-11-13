"""Per-endpoint request budgets over a sliding 10-second window (ADR §4 limits).

Budgets come from config as ``{host: {"*": n, "/endpoint": m}}``. A request to
``host`` + ``path`` must fit both the host-wide ``*`` budget and, when the path matches a
configured endpoint, that endpoint's tighter budget (Gamma: 4,000 overall but 300 on
``/markets``). Over budget means sleeping on the injected clock until the oldest request in
the window ages out -- never sending anyway, never dropping the request.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime


def _endpoint_matches(endpoint: str, path: str) -> bool:
    return path == endpoint or path.startswith(endpoint.rstrip("/") + "/")


class RequestBudget:
    def __init__(
        self,
        limits: dict[str, int],
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        window_s: float = 10.0,
    ):
        self._limits = dict(limits)
        self._clock = clock
        self._sleep = sleep
        self._window = window_s
        self._sent: dict[str, deque[float]] = {}

    @classmethod
    def from_config(cls, config, venue: str, *, clock, sleep, window_s: float = 10.0) -> "RequestBudget":
        source = config.sources[venue]
        limits = {
            f"{host}:{endpoint}": n for host, per_endpoint in source.budgets_per_10s.items()
            for endpoint, n in per_endpoint.items()
        }
        return cls(limits, clock=clock, sleep=sleep, window_s=window_s)

    def _keys_for(self, host: str, path: str) -> list[str]:
        keys = [f"{host}:*"] if f"{host}:*" in self._limits else []
        for key in self._limits:
            h, _, endpoint = key.partition(":")
            if h == host and endpoint != "*" and _endpoint_matches(endpoint, path):
                keys.append(key)
        return keys

    def limit_for(self, host: str, path: str) -> int | None:
        limits = [self._limits[k] for k in self._keys_for(host, path)]
        return min(limits) if limits else None

    def acquire(self, key: str) -> None:
        """Block (on the injected clock) until one more request under ``key`` fits its budget."""
        self._acquire_keys([key] if key in self._limits else [])

    def acquire_request(self, host: str, path: str) -> None:
        self._acquire_keys(self._keys_for(host, path))

    def _acquire_keys(self, keys: list[str]) -> None:
        while True:
            now = self._clock().timestamp()
            wait = 0.0
            for key in keys:
                sent = self._sent.setdefault(key, deque())
                while sent and now - sent[0] >= self._window:
                    sent.popleft()
                if len(sent) >= self._limits[key]:
                    wait = max(wait, sent[0] + self._window - now)
            if wait <= 0:
                for key in keys:
                    self._sent[key].append(now)
                return
            self._sleep(wait)
