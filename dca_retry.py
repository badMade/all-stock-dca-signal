"""Opt-in retry/backoff helpers for broker and market-data calls (not wired into any live path).

``PRODUCTION_READINESS.md`` gap 6: there is no retry or backoff anywhere in the repo, yet Robinhood 429
throttling is an observed live failure mode (2026-07-17: 7 of 27 manual override orders failed). The roadmap
asks for retry with backoff/jitter that **honors do-not-retry semantics and reuses the idempotency ``ref_id``**.
This module is that building block, with the safety properties made explicit and tested. It is deliberately
NOT imported by ``dca_live_adapters`` or any runner: putting retries on the order-placement path changes live
behaviour and needs its own review and acceptance run (CLAUDE.md "Consolidating is a decision, not a cleanup").
A caller opts in by wrapping a callable it already owns.

Safety properties (each pinned by ``tests/test_dca_retry.py``):

* **Fail closed on the unknown.** An exception the classifier does not recognise is DO_NOT_RETRY. Only
  errors that are positively known to be transient are retried.
* **Reads and writes are different.** A read may be repeated freely. A placement may have *landed* even though
  the caller saw a timeout or a 5xx, so for an AMBIGUOUS failure :func:`place_with_reconciliation` first looks
  the order up by its ``ref_id`` and resends only when the lookup proves nothing was placed. If the lookup
  itself fails it raises :class:`PlacementUncertain` and does NOT resend, because a blind resend is how a
  duplicate buy is created.
* **One ``ref_id`` per logical order.** The same ``ref_id`` is passed on every attempt and is never regenerated,
  so a broker that honours idempotency keys collapses an accidental double-submit into one order.
* **Bounded.** Attempts, per-delay ceiling and an optional wall-clock deadline (for example the slot's trading
  window) are all enforced; nothing here can retry forever or past the window.
* **Deterministic in tests.** ``sleep``, ``clock`` and ``rng`` are injected, so no test waits or flakes.

Standard library only; no network, no credentials.
"""

from __future__ import annotations

import enum
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")


class Verdict(enum.Enum):
    """How a failure should be treated by a retry loop."""

    RETRY = "retry"  # the request was definitely not processed (e.g. 429): safe to repeat
    AMBIGUOUS = "ambiguous"  # the request MAY have been processed (timeout, 5xx): never resend blindly
    DO_NOT_RETRY = "do_not_retry"  # permanent or unknown: stop and surface the error


class RetryExhausted(Exception):
    """Every permitted attempt failed with a retryable verdict."""

    def __init__(self, last_exception: BaseException, attempts: int) -> None:
        super().__init__(f"gave up after {attempts} attempt(s): {last_exception!r}")
        self.last_exception = last_exception
        self.attempts = attempts


class PlacementUncertain(Exception):
    """A placement failed ambiguously and its outcome could not be established. Do NOT resend."""

    def __init__(self, ref_id: str, cause: BaseException) -> None:
        super().__init__(f"placement outcome for ref_id={ref_id!r} is unknown ({cause!r}); not resending")
        self.ref_id = ref_id
        self.cause = cause


@dataclass(frozen=True)
class RetryPolicy:
    """Bounds for a retry loop. Validated on construction so a NaN or boolean can never silently disable a bound."""

    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    deadline_s: Optional[float] = None  # total wall-clock budget across all attempts and sleeps

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError(f"max_attempts must be an integer >= 1, got {self.max_attempts!r}")
        for label, value in (("base_delay_s", self.base_delay_s), ("max_delay_s", self.max_delay_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be a finite number >= 0, got {value!r}")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s must be >= base_delay_s")
        if self.deadline_s is not None and (
            isinstance(self.deadline_s, bool)
            or not isinstance(self.deadline_s, (int, float))
            or not math.isfinite(self.deadline_s)
            or self.deadline_s <= 0
        ):
            raise ValueError(f"deadline_s must be a finite number > 0 or None, got {self.deadline_s!r}")


_RETRY_STATUS = frozenset({429})
_AMBIGUOUS_STATUS = frozenset({500, 502, 503, 504})
_RETRY_WORDS = ("429", "too many requests", "rate limit", "rate-limit", "throttl")
_AMBIGUOUS_WORDS = ("timed out", "timeout", "connection reset", "connection aborted", "temporarily unavailable")


def default_classifier(exc: BaseException) -> Verdict:
    """Conservative classification by ``status_code``/``status`` attribute, then by message text.

    Anything not clearly transient is DO_NOT_RETRY. 429 is the only status treated as "definitely not processed";
    timeouts and 5xx are AMBIGUOUS because the broker may have accepted the request before failing."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status in _RETRY_STATUS:
            return Verdict.RETRY
        if status in _AMBIGUOUS_STATUS:
            return Verdict.AMBIGUOUS
        return Verdict.DO_NOT_RETRY
    if isinstance(exc, TimeoutError):
        return Verdict.AMBIGUOUS
    text = str(exc).lower()
    if any(word in text for word in _RETRY_WORDS):
        return Verdict.RETRY
    if any(word in text for word in _AMBIGUOUS_WORDS):
        return Verdict.AMBIGUOUS
    return Verdict.DO_NOT_RETRY


def compute_delay(attempt: int, policy: RetryPolicy, rng: random.Random) -> float:
    """Full-jitter exponential backoff: uniform in ``[0, min(max_delay, base * 2**(attempt-1))]``.

    ``attempt`` is the 1-based number of the attempt that just failed."""
    ceiling = min(policy.max_delay_s, policy.base_delay_s * (2 ** (attempt - 1)))
    return rng.uniform(0.0, ceiling)


def _out_of_time(started: float, policy: RetryPolicy, clock: Callable[[], float], next_delay: float) -> bool:
    return policy.deadline_s is not None and (clock() - started) + next_delay > policy.deadline_s


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: Optional[RetryPolicy] = None,
    classify: Callable[[BaseException], Verdict] = default_classifier,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: Optional[random.Random] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Call an IDEMPOTENT ``fn`` (a read) with backoff. RETRY and AMBIGUOUS are both repeated; DO_NOT_RETRY raises.

    Never use this for an order placement; use :func:`place_with_reconciliation`."""
    policy = policy or RetryPolicy()
    generator = rng or random.Random()
    started = clock()
    last: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classification below decides; unknown stays fatal
            if classify(exc) is Verdict.DO_NOT_RETRY:
                raise
            last = exc
            if attempt == policy.max_attempts:
                break
            delay = compute_delay(attempt, policy, generator)
            if _out_of_time(started, policy, clock, delay):
                break
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    assert last is not None
    raise RetryExhausted(last, attempt)


@dataclass(frozen=True)
class PlacementResult:
    order: Any
    attempts: int
    recovered_via_lookup: bool  # True when an earlier ambiguous attempt had in fact landed and was found by ref_id


def place_with_reconciliation(
    place: Callable[[str], Any],
    lookup: Callable[[str], Optional[Any]],
    *,
    ref_id: str,
    policy: Optional[RetryPolicy] = None,
    classify: Callable[[BaseException], Verdict] = default_classifier,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: Optional[random.Random] = None,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> PlacementResult:
    """Place one order with bounded retry, reusing ``ref_id`` and never resending a possibly-landed order.

    ``place(ref_id)`` submits the order and returns the broker's order. ``lookup(ref_id)`` returns the existing
    order for that ``ref_id`` or ``None`` when the broker reports that none exists; it may raise.

    * RETRY (e.g. 429): the request was rejected before processing, so resend after backoff, same ``ref_id``.
    * AMBIGUOUS (timeout/5xx): call ``lookup`` first. Found -> return it without resending. ``None`` -> safe to
      resend. ``lookup`` raising -> :class:`PlacementUncertain`, no resend.
    * DO_NOT_RETRY or unknown: re-raise immediately.
    """
    if not isinstance(ref_id, str) or not ref_id.strip():
        raise ValueError("ref_id must be a non-empty string; retrying without an idempotency key can double-buy")
    policy = policy or RetryPolicy()
    generator = rng or random.Random()
    started = clock()
    last: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return PlacementResult(order=place(ref_id), attempts=attempt, recovered_via_lookup=False)
        except Exception as exc:  # noqa: BLE001 - classification below decides; unknown stays fatal
            verdict = classify(exc)
            if verdict is Verdict.DO_NOT_RETRY:
                raise
            last = exc
            if verdict is Verdict.AMBIGUOUS:
                try:
                    existing = lookup(ref_id)
                except Exception as lookup_exc:  # noqa: BLE001 - any lookup failure means the outcome is unknown
                    raise PlacementUncertain(ref_id, lookup_exc) from exc
                if existing is not None:
                    return PlacementResult(order=existing, attempts=attempt, recovered_via_lookup=True)
            if attempt == policy.max_attempts:
                break
            delay = compute_delay(attempt, policy, generator)
            if _out_of_time(started, policy, clock, delay):
                break
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    assert last is not None
    raise RetryExhausted(last, attempt)


__all__ = [
    "PlacementResult",
    "PlacementUncertain",
    "RetryExhausted",
    "RetryPolicy",
    "Verdict",
    "call_with_retry",
    "compute_delay",
    "default_classifier",
    "place_with_reconciliation",
]
