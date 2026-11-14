"""Tests for dca_retry: bounded backoff, fail-closed classification, and never-resend-a-landed-order."""

from __future__ import annotations

import random

import pytest

from dca_retry import (
    PlacementUncertain,
    RetryExhausted,
    RetryPolicy,
    Verdict,
    call_with_retry,
    compute_delay,
    default_classifier,
    place_with_reconciliation,
)


class HttpError(Exception):
    def __init__(self, status_code, message="http error"):
        super().__init__(message)
        self.status_code = status_code


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "verdict"),
    [
        (HttpError(429), Verdict.RETRY),
        (HttpError(503), Verdict.AMBIGUOUS),
        (HttpError(500), Verdict.AMBIGUOUS),
        (HttpError(400), Verdict.DO_NOT_RETRY),
        (HttpError(401), Verdict.DO_NOT_RETRY),
        (HttpError(403), Verdict.DO_NOT_RETRY),
        (HttpError(422), Verdict.DO_NOT_RETRY),
        (TimeoutError("read timed out"), Verdict.AMBIGUOUS),
        (RuntimeError("Too Many Requests"), Verdict.RETRY),
        (RuntimeError("rate limit exceeded"), Verdict.RETRY),
        (RuntimeError("insufficient buying power"), Verdict.DO_NOT_RETRY),
        (ValueError("something nobody anticipated"), Verdict.DO_NOT_RETRY),
    ],
)
def test_default_classifier_is_conservative(exc, verdict):
    assert default_classifier(exc) is verdict


def test_boolean_status_is_not_treated_as_an_http_status():
    exc = RuntimeError("weird")
    exc.status_code = True  # type: ignore[attr-defined]
    assert default_classifier(exc) is Verdict.DO_NOT_RETRY


# --- policy and delay ------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"max_attempts": 2.5},
        {"base_delay_s": float("nan")},
        {"base_delay_s": -1},
        {"max_delay_s": float("inf")},
        {"base_delay_s": 5, "max_delay_s": 1},
        {"deadline_s": 0},
        {"deadline_s": float("nan")},
        {"deadline_s": True},
    ],
)
def test_policy_rejects_values_that_would_unbound_the_loop(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_delay_never_exceeds_the_cap_or_goes_negative():
    policy = RetryPolicy(max_attempts=10, base_delay_s=1.0, max_delay_s=8.0)
    rng = random.Random(7)
    for attempt in range(1, 11):
        delay = compute_delay(attempt, policy, rng)
        assert 0.0 <= delay <= min(8.0, 2 ** (attempt - 1))


# --- reads -----------------------------------------------------------------


def test_read_retries_throttling_then_succeeds():
    fake = FakeClock()
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise HttpError(429)
        return "ok"

    result = call_with_retry(fn, sleep=fake.sleep, clock=fake.clock, rng=random.Random(1))
    assert result == "ok"
    assert len(calls) == 3
    assert len(fake.sleeps) == 2


def test_read_does_not_retry_a_permanent_error():
    fake = FakeClock()
    calls = []

    def fn():
        calls.append(1)
        raise HttpError(403)

    with pytest.raises(HttpError):
        call_with_retry(fn, sleep=fake.sleep, clock=fake.clock)
    assert calls == [1]
    assert fake.sleeps == []


def test_read_gives_up_after_max_attempts():
    fake = FakeClock()
    with pytest.raises(RetryExhausted) as info:
        call_with_retry(
            lambda: (_ for _ in ()).throw(HttpError(429)),
            policy=RetryPolicy(max_attempts=3),
            sleep=fake.sleep,
            clock=fake.clock,
            rng=random.Random(2),
        )
    assert info.value.attempts == 3
    assert len(fake.sleeps) == 2


def test_read_stops_retrying_when_the_deadline_would_be_passed():
    fake = FakeClock()
    calls = []

    def fn():
        calls.append(1)
        raise HttpError(429)

    # base delay 10s with a 5s budget: even the first backoff cannot fit, so it must stop after one attempt.
    policy = RetryPolicy(max_attempts=5, base_delay_s=10.0, max_delay_s=10.0, deadline_s=5.0)

    class MaxRng(random.Random):
        def uniform(self, a, b):  # always the full ceiling so the test is deterministic
            return b

    with pytest.raises(RetryExhausted):
        call_with_retry(fn, policy=policy, sleep=fake.sleep, clock=fake.clock, rng=MaxRng())
    assert calls == [1]
    assert fake.sleeps == []


# --- placement -------------------------------------------------------------


def test_placement_success_first_try_calls_place_once_and_never_looks_up():
    placed, looked = [], []
    result = place_with_reconciliation(
        lambda ref: placed.append(ref) or {"id": "o1"},
        lambda ref: looked.append(ref),
        ref_id="slot-1030-AAPL",
    )
    assert result.order == {"id": "o1"}
    assert result.attempts == 1
    assert result.recovered_via_lookup is False
    assert placed == ["slot-1030-AAPL"] and looked == []


def test_throttled_placement_retries_with_the_same_ref_id_and_skips_lookup():
    fake = FakeClock()
    seen = []

    def place(ref):
        seen.append(ref)
        if len(seen) < 3:
            raise HttpError(429)
        return {"id": "o2"}

    looked = []
    result = place_with_reconciliation(
        place,
        lambda ref: looked.append(ref),
        ref_id="slot-AAPL",
        sleep=fake.sleep,
        clock=fake.clock,
        rng=random.Random(3),
    )
    assert result.attempts == 3
    assert seen == ["slot-AAPL"] * 3, "the idempotency key must never change between attempts"
    assert looked == [], "a 429 means not processed; there is nothing to reconcile"


def test_ambiguous_failure_that_actually_landed_is_recovered_without_resending():
    placed = []

    def place(ref):
        placed.append(ref)
        raise TimeoutError("read timed out")

    result = place_with_reconciliation(
        place,
        lambda ref: {"id": "landed", "ref_id": ref},
        ref_id="slot-MSFT",
        sleep=lambda _s: None,
    )
    assert placed == ["slot-MSFT"], "an order that may have landed must not be submitted a second time"
    assert result.recovered_via_lookup is True
    assert result.order["id"] == "landed"


def test_ambiguous_failure_with_no_order_found_resends_with_the_same_ref_id():
    fake = FakeClock()
    placed = []

    def place(ref):
        placed.append(ref)
        if len(placed) == 1:
            raise HttpError(503)
        return {"id": "o3"}

    result = place_with_reconciliation(
        place,
        lambda ref: None,
        ref_id="slot-NVDA",
        sleep=fake.sleep,
        clock=fake.clock,
        rng=random.Random(4),
    )
    assert placed == ["slot-NVDA", "slot-NVDA"]
    assert result.attempts == 2 and result.recovered_via_lookup is False


def test_ambiguous_failure_with_a_failing_lookup_raises_and_does_not_resend():
    placed = []

    def place(ref):
        placed.append(ref)
        raise TimeoutError("timed out")

    def lookup(ref):
        raise RuntimeError("order history unavailable")

    with pytest.raises(PlacementUncertain) as info:
        place_with_reconciliation(place, lookup, ref_id="slot-TSLA", sleep=lambda _s: None)
    assert placed == ["slot-TSLA"]
    assert info.value.ref_id == "slot-TSLA"


def test_permanent_rejection_is_raised_immediately_with_no_lookup_or_retry():
    placed, looked = [], []

    def place(ref):
        placed.append(ref)
        raise HttpError(422, "symbol not tradable")

    with pytest.raises(HttpError):
        place_with_reconciliation(place, lambda ref: looked.append(ref), ref_id="slot-X")
    assert placed == ["slot-X"] and looked == []


def test_unknown_exception_is_fatal_for_a_placement():
    with pytest.raises(ValueError):
        place_with_reconciliation(
            lambda ref: (_ for _ in ()).throw(ValueError("unanticipated")),
            lambda ref: None,
            ref_id="slot-Y",
        )


def test_exhausted_throttling_raises_retry_exhausted():
    fake = FakeClock()
    with pytest.raises(RetryExhausted) as info:
        place_with_reconciliation(
            lambda ref: (_ for _ in ()).throw(HttpError(429)),
            lambda ref: None,
            ref_id="slot-Z",
            policy=RetryPolicy(max_attempts=2),
            sleep=fake.sleep,
            clock=fake.clock,
            rng=random.Random(5),
        )
    assert info.value.attempts == 2


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_placement_requires_a_real_idempotency_key(bad):
    with pytest.raises(ValueError):
        place_with_reconciliation(lambda ref: None, lambda ref: None, ref_id=bad)  # type: ignore[arg-type]
