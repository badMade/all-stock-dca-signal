"""Credential-safe diagnostics at real strategy and adapter logging boundaries."""

from __future__ import annotations

import io
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import pytest

import dca_live_adapters
import near_close_dca
from dca_live_adapters import FinanceDataProvider, RobinhoodDataProvider
from near_close_dca import EASTERN, Action, AlpacaBroker, StrategyConfig, _evaluate_all_signals


@contextmanager
def _logged_output(logger: logging.Logger) -> Iterator[io.StringIO]:
    """Capture ordinary formatted output without replacing production filters."""
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield output
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        handler.close()


def test_alpaca_fill_failure_redacts_the_entire_exception_chain():
    bearer = "synthetic-fill-bearer-value"
    account = "73" + "951" + "824"

    def failed_fill_lookup(_payload):
        try:
            raise ValueError(f"Authorization: Bearer {bearer}")
        except ValueError as cause:
            raise RuntimeError(f"fill transport failed account_number={account}") from cause

    broker = AlpacaBroker(order_fill_fn=failed_fill_lookup)
    with _logged_output(near_close_dca.logger) as output:
        assert broker.get_order_fill("synthetic-order") is None

    text = output.getvalue()
    assert "order-fill lookup failed" in text
    assert "ValueError" in text and "RuntimeError" in text
    assert "[REDACTED]" in text
    assert bearer not in text
    assert account not in text


def test_robinhood_provider_failure_is_redacted_when_signal_evaluation_degrades():
    secret = "synthetic-historicals-api-value"
    calls = []

    def failed_historicals(**kwargs):
        calls.append(kwargs)
        raise RuntimeError(f"historicals transport failed api_key={secret}")

    empty_provider = FinanceDataProvider(bars_fn=lambda *_args: [], previous_close_fn=lambda *_args: None)
    failing_provider = RobinhoodDataProvider(historicals_fn=failed_historicals)
    with _logged_output(near_close_dca.logger) as output:
        decisions, errors = _evaluate_all_signals(
            datetime(2026, 9, 4, 15, 45, tzinfo=EASTERN),
            StrategyConfig(),
            ["AAPL"],
            empty_provider,
            failing_provider,
        )

    assert len(calls) == 1
    assert calls[0]["symbols"] == ["AAPL"]
    assert errors == 1
    assert len(decisions) == 1 and decisions[0].action != Action.BUY
    text = output.getvalue()
    assert "AAPL" in text and "signal evaluation failed" in text
    assert "historicals transport failed" in text
    assert "[REDACTED]" in text
    assert secret not in text


def test_adapter_quote_warning_redacts_untrusted_state_field():
    secret = "synthetic-quote-password-value"
    provider = RobinhoodDataProvider(
        historicals_fn=lambda **_kwargs: {"results": []},
        quotes_fn=lambda **_kwargs: {
            "results": [{"quote": {"symbol": "AAPL", "state": f"password={secret}", "has_traded": False}}]
        },
    )
    with _logged_output(dca_live_adapters.logger) as output:
        assert provider.get_quote("AAPL", datetime(2026, 9, 4, 15, 45, tzinfo=EASTERN)) is None

    text = output.getvalue()
    assert "skipping quote for AAPL" in text
    assert "[REDACTED]" in text
    assert secret not in text


@pytest.mark.parametrize("logger", [near_close_dca.logger, dca_live_adapters.logger], ids=["core", "adapter"])
def test_benign_numeric_log_arguments_keep_their_formatting(logger):
    with _logged_output(logger) as output:
        logger.info("AAPL: count=%d price=%.2f", 3, 12.5)

    assert output.getvalue() == "INFO AAPL: count=3 price=12.50\n"


@pytest.mark.parametrize("logger", [near_close_dca.logger, dca_live_adapters.logger], ids=["core", "adapter"])
def test_cached_traceback_stack_and_message_are_sanitized(logger):
    exception_secret = "synthetic-cached-api-value"
    stack_secret = "synthetic-stack-bearer-value"
    message_secret = "synthetic-cached-password-value"
    record = logging.LogRecord(logger.name, logging.ERROR, __file__, 1, "cached diagnostic", (), None)
    record.exc_text = f"RuntimeError: api_key={exception_secret}"
    record.stack_info = f"Stack detail: Authorization: Bearer {stack_secret}"
    record.message = f"password={message_secret}"

    with _logged_output(logger) as output:
        logger.handle(record)

    text = output.getvalue()
    assert "cached diagnostic" in text
    assert "RuntimeError" in text and "Stack detail" in text
    assert "[REDACTED]" in text
    for secret in (exception_secret, stack_secret, message_secret):
        assert secret not in text
        assert secret not in str(record.__dict__)
    assert record.exc_info is None


@pytest.mark.parametrize("logger", [near_close_dca.logger, dca_live_adapters.logger], ids=["core", "adapter"])
@pytest.mark.parametrize("broken_object", [False, True], ids=["bad-format", "bad-str"])
def test_unformattable_diagnostics_fail_closed(logger, broken_object, capsys):
    secret = "synthetic-unformattable-password-value"

    class BrokenMessage:
        def __str__(self):
            raise ValueError(f"password={secret}")

    with _logged_output(logger) as output:
        if broken_object:
            logger.error(BrokenMessage())
        else:
            logger.error("count=%d password=%s", object(), secret)

    text = output.getvalue()
    assert text.strip(), "Unformattable diagnostics should retain a safe failure notice."
    assert secret not in text
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert "Logging error" not in captured.err
