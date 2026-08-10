"""Regression tests for SheetsWriter's SSRF hardening in strategy_monitor.py.

Pins the fix from the "🛡️ Sentinel: Fix SSRF in strategy_monitor.py" PR:
`write_rows` must reject any Sheets API URL that isn't `https://api.pipedream.com/...`
before ever reaching `urlopen`, and the request it does send must use an opener
that refuses to follow redirects (so a compromised/misconfigured upstream can't
redirect the request -- and its `Authorization` header -- to an internal host).

Skips (rather than fails) when the trading-strategies research extras
(pandas/numpy/scipy/jinja2/yfinance/vectorbt/alpaca-py) aren't installed, since
those aren't part of the core suite's dependencies -- see AGENTS.md.
"""

import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

strategy_monitor = pytest.importorskip("strategy_monitor")

CONFIG = {
    "GOOGLE_SHEET_ID":   "sheet123",
    "GOOGLE_SHEET_NAME": "Weekly_Report",
    "PIPEDREAM_TOKEN":   "super-secret-pipedream-token",
}


def _mock_success_response(status=201):
    resp = MagicMock()
    resp.status = status
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_write_rows_rejects_non_https_scheme(monkeypatch):
    monkeypatch.setattr(
        strategy_monitor.SheetsWriter,
        "_API_BASE",
        "http://api.pipedream.com/v1/accounts/google_sheets__pipedream/actions",
    )
    writer = strategy_monitor.SheetsWriter(CONFIG)

    with patch("urllib.request.build_opener") as mock_build_opener:
        result = writer.write_rows({}, {}, "2026-08-10")

    assert result is False
    mock_build_opener.assert_not_called()


def test_write_rows_rejects_untrusted_host(monkeypatch):
    monkeypatch.setattr(
        strategy_monitor.SheetsWriter,
        "_API_BASE",
        "https://evil.example.com/v1/accounts/google_sheets__pipedream/actions",
    )
    writer = strategy_monitor.SheetsWriter(CONFIG)

    with patch("urllib.request.build_opener") as mock_build_opener:
        result = writer.write_rows({}, {}, "2026-08-10")

    assert result is False
    mock_build_opener.assert_not_called()


def test_write_rows_rejects_file_scheme(monkeypatch):
    monkeypatch.setattr(strategy_monitor.SheetsWriter, "_API_BASE", "file:///etc/passwd")
    writer = strategy_monitor.SheetsWriter(CONFIG)

    with patch("urllib.request.build_opener") as mock_build_opener:
        result = writer.write_rows({}, {}, "2026-08-10")

    assert result is False
    mock_build_opener.assert_not_called()


def test_write_rows_uses_redirect_blocking_opener_on_trusted_url():
    writer = strategy_monitor.SheetsWriter(CONFIG)
    mock_opener = MagicMock()
    mock_opener.open.return_value = _mock_success_response()

    with patch("urllib.request.build_opener", return_value=mock_opener) as mock_build_opener:
        result = writer.write_rows({"StratA": {"sharpe": 1.0}}, {}, "2026-08-10")

    assert result is True
    mock_build_opener.assert_called_once()
    handler_cls = mock_build_opener.call_args[0][0]
    assert issubclass(handler_cls, urllib.request.HTTPRedirectHandler)


def test_no_redirect_handler_blocks_redirects_instead_of_following_them():
    writer = strategy_monitor.SheetsWriter(CONFIG)
    mock_opener = MagicMock()
    mock_opener.open.return_value = _mock_success_response()

    with patch("urllib.request.build_opener", return_value=mock_opener) as mock_build_opener:
        writer.write_rows({}, {}, "2026-08-10")

    handler_cls = mock_build_opener.call_args[0][0]
    handler = handler_cls()

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            req=MagicMock(),
            fp=MagicMock(),
            code=302,
            msg="Found",
            headers={},
            newurl="http://169.254.169.254/latest/meta-data/",
        )
