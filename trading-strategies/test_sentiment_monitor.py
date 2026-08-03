"""Regression tests for NewsCollector's header-based NewsAPI authentication.

Pins the fix for the API-key-in-URL leak (PR #311): fetch_headlines must send
the NewsAPI key via the X-Api-Key header, never as a query parameter or
embedded in the request URL, so it can't reappear in access logs, proxy
logs, or exception messages.

Skips (rather than fails) when the trading-strategies research extras
(numpy/torch/transformers) aren't installed, since those aren't part of the
core suite's dependencies -- see AGENTS.md.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

sentiment_monitor = pytest.importorskip("sentiment_monitor")

API_KEY = "super-secret-newsapi-key"


def _mock_response(payload=None):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload if payload is not None else {"status": "ok", "articles": []}
    return resp


def test_fetch_headlines_sends_key_only_in_header():
    collector = sentiment_monitor.NewsCollector(api_key=API_KEY, days=3)

    with patch("sentiment_monitor.requests.get", return_value=_mock_response()) as mock_get:
        collector.fetch_headlines("AAPL")

    assert mock_get.called
    url, kwargs = mock_get.call_args[0][0], mock_get.call_args.kwargs
    assert kwargs["headers"]["X-Api-Key"] == API_KEY
    assert API_KEY not in str(kwargs["params"])
    assert API_KEY not in url


def test_fetch_headlines_sends_user_agent():
    collector = sentiment_monitor.NewsCollector(api_key=API_KEY, days=3)

    with patch("sentiment_monitor.requests.get", return_value=_mock_response()) as mock_get:
        collector.fetch_headlines("AAPL")

    assert mock_get.call_args.kwargs["headers"]["User-Agent"]


def test_fetch_headlines_handles_ok_status_with_null_articles():
    collector = sentiment_monitor.NewsCollector(api_key=API_KEY, days=3)

    with patch("sentiment_monitor.requests.get", return_value=_mock_response({"status": "ok", "articles": None})):
        result = collector.fetch_headlines("AAPL")

    assert result == []


def test_fetch_headlines_handles_error_status_payload():
    collector = sentiment_monitor.NewsCollector(api_key=API_KEY, days=3)
    error_payload = {"status": "error", "code": "apiKeyInvalid", "message": "Your API key is invalid"}

    with patch("sentiment_monitor.requests.get", return_value=_mock_response(error_payload)):
        with patch.object(sentiment_monitor, "logger") as mock_logger:
            result = collector.fetch_headlines("AAPL")

    assert result == []
    assert mock_logger.warning.called


def test_fetch_headlines_error_message_redacts_key():
    collector = sentiment_monitor.NewsCollector(api_key=API_KEY, days=3)

    with patch("sentiment_monitor.requests.get", side_effect=RuntimeError(f"boom {API_KEY}")):
        with patch.object(sentiment_monitor, "logger") as mock_logger:
            result = collector.fetch_headlines("AAPL")

    assert result == []
    logged_message = mock_logger.error.call_args[0][2]
    assert API_KEY not in logged_message
    assert "***" in logged_message
