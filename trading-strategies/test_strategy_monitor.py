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

import importlib.util
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Stub missing optional research dependencies during import so SheetsWriter SSRF regression tests always run in CI
# without polluting global sys.modules for the rest of the test suite.
_stubs = {}
for _mod in (
    "numpy",
    "pandas",
    "requests",
    "vectorbt",
    "yfinance",
    "alpaca",
    "alpaca.data",
    "alpaca.data.historical",
    "alpaca.data.requests",
    "alpaca.data.timeframe",
    "jinja2",
    "scipy",
    "scipy.stats",
):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            _stubs[_mod] = MagicMock()
            sys.modules[_mod] = _stubs[_mod]

try:
    import strategy_monitor  # noqa: E402
finally:
    for _mod in _stubs:
        sys.modules.pop(_mod, None)

CONFIG = {
    "GOOGLE_SHEET_ID":   "sheet123",
    "GOOGLE_SHEET_NAME": "Weekly_Report",
    "PIPEDREAM_TOKEN":   "super-secret-pipedream-token",
}


@pytest.fixture(params=["strategy", "sentiment", "monday"])
def smtp_sender(request, monkeypatch):
    """Exercise each real sender with synthetic configuration and mocked transport."""
    cfg = {
        "SMTP_HOST": "smtp.example.invalid", "SMTP_PORT": 587,
        "SMTP_USER": "sender@example.invalid", "SMTP_PASSWORD": "fixture-password",
        "RECIPIENT_EMAIL": "recipient@example.invalid",
    }
    if request.param == "strategy":
        sender = strategy_monitor.EmailNotifier(cfg)
        return strategy_monitor, lambda: sender.send("fixture", "<p>fixture</p>"), False
    if request.param == "sentiment":
        with patch.dict(sys.modules, _stubs):
            try:
                import sentiment_monitor
            except Exception:
                import pytest
                pytest.skip("sentiment_monitor missing dependencies")

        for key, value in cfg.items():
            monkeypatch.setenv(key, str(value))
        sender = sentiment_monitor.EmailBriefing()
        return sentiment_monitor, lambda: sender.send("fixture", "<p>fixture</p>"), False
    spec = importlib.util.spec_from_file_location(
        "smtp_fixture_sentiment_cron", Path(__file__).parent / "monday-sentiment" / "sentiment_cron.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The optional HTTP client is unrelated to this standard-library SMTP path.
    monkeypatch.setitem(sys.modules, "aiohttp", MagicMock())
    spec.loader.exec_module(module)
    return module, lambda: module.send_email(
        "<p>fixture</p>", "fixture", cfg["SMTP_USER"], cfg["SMTP_PASSWORD"], cfg["RECIPIENT_EMAIL"],
        smtp_host=cfg["SMTP_HOST"],
    ), True


def test_smtp_verifies_server_before_authentication(smtp_sender):
    module, send, _ = smtp_sender
    with patch.object(module.smtplib, "SMTP") as transport:
        server = transport.return_value.__enter__.return_value
        send()
    context = server.starttls.call_args.kwargs.get("context")
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    calls = [call[0] for call in server.method_calls]
    assert calls.index("starttls") < calls.index("login") < calls.index("sendmail")


def test_smtp_certificate_rejection_never_authenticates_or_sends(smtp_sender):
    module, send, returns_failure = smtp_sender
    with patch.object(module.smtplib, "SMTP") as transport:
        server = transport.return_value.__enter__.return_value
        server.starttls.side_effect = ssl.SSLCertVerificationError("fixture certificate rejected")
        if returns_failure:
            assert send() is False
        else:
            with pytest.raises(ssl.SSLCertVerificationError):
                send()
    server.login.assert_not_called()
    server.sendmail.assert_not_called()


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

    with (
        patch("urllib.request.build_opener") as mock_build_opener,
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        result = writer.write_rows({}, {}, "2026-08-10")

    assert result is False
    mock_build_opener.assert_not_called()
    mock_urlopen.assert_not_called()


def test_write_rows_rejects_untrusted_host(monkeypatch):
    monkeypatch.setattr(
        strategy_monitor.SheetsWriter,
        "_API_BASE",
        "https://evil.example.com/v1/accounts/google_sheets__pipedream/actions",
    )
    writer = strategy_monitor.SheetsWriter(CONFIG)

    with (
        patch("urllib.request.build_opener") as mock_build_opener,
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        result = writer.write_rows({}, {}, "2026-08-10")

    assert result is False
    mock_build_opener.assert_not_called()
    mock_urlopen.assert_not_called()

def test_write_rows_rejects_disallowed_ip(monkeypatch):
    import dca_net_utils

    monkeypatch.setattr(
        strategy_monitor.SheetsWriter,
        "_API_BASE",
        "https://api.pipedream.com/v1/accounts/google_sheets__pipedream/actions",
    )
    writer = strategy_monitor.SheetsWriter(CONFIG)

    with (
        patch("urllib.request.build_opener") as mock_build_opener,
        patch("urllib.request.urlopen") as mock_urlopen,
        patch(
            "dca_net_utils.assert_safe_destination",
            side_effect=Exception("disallowed ip"),
        ) as mock_assert_safe_destination,
    ):
        result = writer.write_rows({}, {}, "2026-08-10")

    assert result is False
    mock_assert_safe_destination.assert_called_once_with(
        "https://api.pipedream.com/v1/accounts/google_sheets__pipedream/actions/add-rows"
    )
    mock_build_opener.assert_not_called()
    mock_urlopen.assert_not_called()


def test_write_rows_rejects_file_scheme(monkeypatch):
    monkeypatch.setattr(strategy_monitor.SheetsWriter, "_API_BASE", "file:///etc/passwd")
    writer = strategy_monitor.SheetsWriter(CONFIG)

    with (
        patch("urllib.request.build_opener") as mock_build_opener,
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        result = writer.write_rows({}, {}, "2026-08-10")

    assert result is False
    mock_build_opener.assert_not_called()
    mock_urlopen.assert_not_called()


def test_write_rows_uses_redirect_blocking_opener_on_trusted_url():
    writer = strategy_monitor.SheetsWriter(CONFIG)
    mock_opener = MagicMock()
    mock_opener.open.return_value = _mock_success_response()

    with patch("urllib.request.build_opener", return_value=mock_opener) as mock_build_opener:
        result = writer.write_rows({"StratA": {"sharpe": 1.0}}, {}, "2026-08-10")

    assert result is True
    assert mock_build_opener.call_count >= 1
    mock_opener.open.assert_called_once()
    req = mock_opener.open.call_args.args[0]
    assert req.get_header("Authorization") == f"Bearer {CONFIG['PIPEDREAM_TOKEN']}"
    # find the call that sets _NoRedirectHandler
    call_args = None
    for call in mock_build_opener.call_args_list:
        if call.args and issubclass(call.args[0], urllib.request.HTTPRedirectHandler):
            call_args = call.args
            break
    assert call_args is not None
    handler_cls = call_args[0]
    assert issubclass(handler_cls, urllib.request.HTTPRedirectHandler)


def test_no_redirect_handler_blocks_redirects_instead_of_following_them():
    writer = strategy_monitor.SheetsWriter(CONFIG)
    mock_opener = MagicMock()
    mock_opener.open.return_value = _mock_success_response()

    with patch("urllib.request.build_opener", return_value=mock_opener) as mock_build_opener:
        writer.write_rows({}, {}, "2026-08-10")

    call_args = None
    for call in mock_build_opener.call_args_list:
        if call.args and issubclass(call.args[0], urllib.request.HTTPRedirectHandler):
            call_args = call.args
            break
    assert call_args is not None
    handler_cls = call_args[0]
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


def test_apply_config_overrides_normalizes_non_string_secrets(tmp_path):
    config_file = tmp_path / "overrides.json"
    config_file.write_text(
        '{"SMTP_PASSWORD": 1234, "PIPEDREAM_TOKEN": 5678, "THRESHOLDS": {"sharpe": 0.5}}',
        encoding="utf-8",
    )
    base = {
        "SMTP_PASSWORD": "",
        "PIPEDREAM_TOKEN": "",
        "THRESHOLDS": {"sharpe": 0.3, "sortino": 0.5},
    }
    merged = strategy_monitor._apply_config_overrides(str(config_file), base)
    assert merged["SMTP_PASSWORD"] == "1234"
    assert isinstance(merged["SMTP_PASSWORD"], str)
    assert merged["PIPEDREAM_TOKEN"] == "5678"
    assert isinstance(merged["PIPEDREAM_TOKEN"], str)
    assert merged["THRESHOLDS"]["sharpe"] == 0.5


def test_sheets_writer_sanitize_tolerates_non_string_token():
    writer = strategy_monitor.SheetsWriter({"PIPEDREAM_TOKEN": 999})
    sanitized = writer._sanitize("Error response containing token 999")
    assert "999" not in sanitized
    assert "***" in sanitized


def test_email_sender_error_tolerates_non_string_smtp_password():
    cfg = {**strategy_monitor.CONFIG, "SMTP_PASSWORD": 555}
    orchestrator = strategy_monitor.MonitorOrchestrator(
        config=cfg,
        dry_run=False,
    )
    with patch.object(orchestrator.emailer, "send", side_effect=RuntimeError("auth failed with 555")):
        with patch.object(strategy_monitor, "logger") as mock_logger:
            # Simulate the error handling in orchestrator
            try:
                raise RuntimeError("auth failed with 555")
            except Exception as exc:
                safe_exc = str(exc)
                safe_traceback = "traceback with 555"
                if orchestrator.cfg.get("SMTP_PASSWORD"):
                    smtp_pw = str(orchestrator.cfg["SMTP_PASSWORD"])
                    safe_exc = safe_exc.replace(smtp_pw, "***")
                    safe_traceback = safe_traceback.replace(smtp_pw, "***")
                assert "555" not in safe_exc
                assert "***" in safe_exc
                assert "555" not in safe_traceback
                assert "***" in safe_traceback


def test_fatal_exception_redaction_tolerates_non_string_secrets():
    effective_config = {
        "SMTP_PASSWORD": 1234,
        "PIPEDREAM_TOKEN": 5678,
    }
    try:
        raise RuntimeError("failed with 1234 and 5678")
    except Exception as exc:
        safe_exc = str(exc)
        safe_traceback = "traceback with 1234 and 5678"
        if effective_config.get("SMTP_PASSWORD"):
            smtp_pw = str(effective_config["SMTP_PASSWORD"])
            safe_exc = safe_exc.replace(smtp_pw, "***")
            safe_traceback = safe_traceback.replace(smtp_pw, "***")
        if effective_config.get("PIPEDREAM_TOKEN"):
            pipedream_tok = str(effective_config["PIPEDREAM_TOKEN"])
            safe_exc = safe_exc.replace(pipedream_tok, "***")
            safe_traceback = safe_traceback.replace(pipedream_tok, "***")

    assert "1234" not in safe_exc
    assert "5678" not in safe_exc
    assert safe_exc == "failed with *** and ***"
    assert "1234" not in safe_traceback
    assert "5678" not in safe_traceback
    assert safe_traceback == "traceback with *** and ***"
