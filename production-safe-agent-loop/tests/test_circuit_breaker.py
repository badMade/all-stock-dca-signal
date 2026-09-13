"""Tests for circuit_breaker.py.

CircuitBreaker enforces two hard ceilings -- turn count and cumulative
tokens -- via an exception rather than a return code, so a breach can't be
silently ignored. This is the financial safety component: it's small and
fully deterministic, and these tests aim for 100% coverage with no
untested paths.
"""

import pytest
from circuit_breaker import CircuitBreaker, CircuitBreakerError


def test_defaults_are_turn_limit_5_token_limit_15000():
    breaker = CircuitBreaker()

    assert breaker.turn_limit == 5
    assert breaker.token_limit == 15000


def test_custom_limits_stick():
    breaker = CircuitBreaker(turn_limit=3, token_limit=100)

    assert breaker.turn_limit == 3
    assert breaker.token_limit == 100


def test_under_both_limits_does_not_raise():
    breaker = CircuitBreaker(turn_limit=5, token_limit=15000)

    breaker.check(turn_count=1, accumulated_tokens=100)  # must not raise


def test_exactly_at_turn_limit_does_not_raise():
    breaker = CircuitBreaker(turn_limit=5, token_limit=15000)

    breaker.check(turn_count=5, accumulated_tokens=0)  # must not raise


def test_exactly_at_token_limit_does_not_raise():
    breaker = CircuitBreaker(turn_limit=5, token_limit=15000)

    breaker.check(turn_count=1, accumulated_tokens=15000)  # must not raise


def test_turn_limit_plus_one_raises_turn_ceiling_with_exact_values():
    breaker = CircuitBreaker(turn_limit=5, token_limit=15000)

    with pytest.raises(CircuitBreakerError) as excinfo:
        breaker.check(turn_count=6, accumulated_tokens=42)

    err = excinfo.value
    assert err.reason == "turn_ceiling"
    assert err.turn_count == 6
    assert err.accumulated_tokens == 42


def test_token_ceiling_breach_independent_of_turn_count():
    breaker = CircuitBreaker(turn_limit=5, token_limit=15000)

    with pytest.raises(CircuitBreakerError) as excinfo:
        # Small turn_count, well under turn_limit, to prove the token
        # breach fires on its own merits.
        breaker.check(turn_count=1, accumulated_tokens=15001)

    err = excinfo.value
    assert err.reason == "token_ceiling"
    assert err.turn_count == 1
    assert err.accumulated_tokens == 15001


def test_both_ceilings_breached_at_once_turn_wins():
    breaker = CircuitBreaker(turn_limit=5, token_limit=15000)

    with pytest.raises(CircuitBreakerError) as excinfo:
        breaker.check(turn_count=6, accumulated_tokens=15001)

    assert excinfo.value.reason == "turn_ceiling"


def test_exception_string_form_contains_reason_turn_and_tokens():
    breaker = CircuitBreaker(turn_limit=5, token_limit=15000)

    with pytest.raises(CircuitBreakerError) as excinfo:
        breaker.check(turn_count=6, accumulated_tokens=42)

    message = str(excinfo.value)
    assert "turn_ceiling" in message
    assert "turn=6" in message
    assert "tokens=42" in message


def test_circuit_breaker_error_is_exception_subclass():
    assert issubclass(CircuitBreakerError, Exception)


def test_checkpoint_banner_on_turn_ceiling_breach(capsys):
    # Non-default limits so a passing assertion can't be accidentally
    # matching the class defaults instead of the configured values.
    breaker = CircuitBreaker(turn_limit=2, token_limit=999)

    with pytest.raises(CircuitBreakerError):
        breaker.check(turn_count=3, accumulated_tokens=0)

    out = capsys.readouterr().out
    assert "CIRCUIT BREAKER CHECKPOINT" in out
    assert "turn_ceiling" in out
    assert "limit 2" in out
    assert "limit 999" in out


def test_checkpoint_banner_on_token_ceiling_breach(capsys):
    breaker = CircuitBreaker(turn_limit=7, token_limit=250)

    with pytest.raises(CircuitBreakerError):
        breaker.check(turn_count=1, accumulated_tokens=251)

    out = capsys.readouterr().out
    assert "CIRCUIT BREAKER CHECKPOINT" in out
    assert "token_ceiling" in out
    assert "limit 7" in out
    assert "limit 250" in out


def test_banner_prints_even_if_caller_swallows_the_exception(capsys):
    """The checkpoint banner is printed before the exception is raised, so
    a caller that catches and swallows CircuitBreakerError still can't
    hide the breach from stdout."""
    breaker = CircuitBreaker(turn_limit=1, token_limit=10)

    try:
        breaker.check(turn_count=2, accumulated_tokens=0)
    except CircuitBreakerError:
        pass  # simulate a caller that swallows the exception

    assert "CIRCUIT BREAKER CHECKPOINT" in capsys.readouterr().out
